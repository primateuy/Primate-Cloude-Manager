# -*- coding: utf-8 -*-
"""Goldens de R3-B1: los scripts multi-Odoo renderizados de VERDAD.

El golden más crítico del recableo es el de aislamiento multi-tenant
(``test_conf_real_aisla_multitenant_cada_instancia``): valida que el conf
REAL que aterriza en el servidor —no el template— lleve ``db_filter`` y
``list_db`` correctos para CADA instancia. Es la barrera contra un incidente
de privacidad entre clientes en un servidor compartido: si un cambio futuro
lo afloja, la suite se pone roja.
"""
import re
import shutil
import subprocess
import tempfile

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestR3Scripts(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project_a = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id})
        self.project_b = self.env["primate.cloud.project"].create(
            {"name": "Cliente B", "account_id": self.account.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor compartido", "project_id": self.project_a.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        # Instancia A = la primaria (nace con el entorno); B = de OTRO cliente.
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "pg_user": "odoo_cliente_a",
                           "http_port": 8069, "gevent_port": 8072})
        self.inst_b = self.env["primate.cloud.instance"].create({
            "name": "Odoo Cliente B", "project_id": self.project_b.id,
            "environment_id": self.server.id, "slug": "cliente-b",
            "pg_user": "odoo_cliente_b", "odoo_version": "19",
            "http_port": 8079, "gevent_port": 8082,
        })

    def _params(self, db_name, **overrides):
        params = {
            "db_mode": "local_pg", "db_name": db_name,
            "db_password": "PgPass_token-urlsafe-XYZ", "db_host": "localhost",
            "domain": "%s.pcm.test" % db_name, "admin_password": "admin-x",
            "workers": 0,
        }
        params.update(overrides)
        return params

    @staticmethod
    def _conf_block(script, slug):
        """Extrae el bloque del odoo.conf REAL del script renderizado."""
        match = re.search(r"cat > \"\$\{CONF\}\" <<'CONF'\n(.*?)\nCONF\n",
                          script, re.S)
        assert match, "no se encontró el heredoc del conf"
        return match.group(1)

    # ------------------------------------------------------------------
    # EL golden crítico del recableo: aislamiento multi-tenant por conf.
    # ------------------------------------------------------------------
    def test_conf_real_aisla_multitenant_cada_instancia(self):
        """El conf REAL de CADA instancia lleva el candado db_filter+list_db.

        Barrera de privacidad entre clientes en un servidor compartido: la
        instancia de A no puede ver ni listar la BD de B. Se valida sobre el
        script RENDERIZADO (valores finales), no sobre el template.
        """
        for instance, db_name in ((self.inst_a, "cliente_a_db"),
                                  (self.inst_b, "cliente_b_db")):
            script = self.server._build_instance_install_script(
                instance, self._params(db_name), is_default=False)
            conf = self._conf_block(script, instance.slug)
            self.assertIn("db_filter = ^%s$" % db_name, conf,
                          "db_filter EXACTO ausente para %s" % instance.slug)
            self.assertIn("list_db = False", conf,
                          "list_db = False ausente para %s" % instance.slug)
            self.assertIn("db_name = %s" % db_name, conf)
            # workers = 0 (default multi-Odoo) NO puede colapsar a "" en el
            # conf: 'workers = ' → int('') → Odoo no arranca (hallazgo del
            # E2E real). Debe aterrizar literal '0'.
            self.assertIn("workers = 0", conf)
            self.assertNotIn("workers = \n", conf + "\n")
            # Credencial PROPIA: el conf usa el pg_user de ESTA instancia.
            self.assertIn("db_user = %s" % instance.pg_user, conf)
            # data_dir/addons/log del slug propio (filestore separado).
            self.assertIn("/opt/pcm/instances/%s/data" % instance.slug, conf)
        # Cruzado explícito: el conf de A no menciona nada de B y viceversa.
        script_a = self.server._build_instance_install_script(
            self.inst_a, self._params("cliente_a_db"), is_default=True)
        self.assertNotIn("cliente-b", script_a)
        self.assertNotIn("cliente_b", script_a)

    def test_propiedad_por_slug_no_toca_lo_ajeno(self):
        # Todo artefacto escrito/borrado lleva el slug; el único rm fuera del
        # slug NO existe acá (el default site lo saca el bootstrap).
        script = self.server._build_instance_install_script(
            self.inst_b, self._params("cliente_b_db"), is_default=False)
        self.assertNotIn("sites-enabled/default", script)
        self.assertIn("/etc/odoo/${SLUG}.conf", script)
        self.assertIn("odoo-${SLUG}.service", script)
        self.assertIn("pcm-${SLUG}.conf", script)
        self.assertIn('SLUG="cliente-b"', script)
        # nginx: reload sí, restart jamás.
        self.assertIn("systemctl reload nginx", script)
        self.assertNotIn("systemctl restart nginx", script)

    def test_rollback_clean_slate_orden_y_guardas(self):
        script = self.server._build_instance_install_script(
            self.inst_b, self._params("cliente_b_db"), is_default=False)
        rollback = script[script.index("rollback()"):script.index("trap rollback ERR")]
        # Orden: unit → site → dropdb → dropuser → dirs.
        order = [rollback.index("systemctl stop"), rollback.index("rm -f \"${SITE_LINK}\""),
                 rollback.index("dropdb"), rollback.index("dropuser"),
                 rollback.index("rm -rf \"${INST_DIR}\"")]
        self.assertEqual(order, sorted(order))
        # dropdb SOLO si la BD nació en esta corrida (guardas de banderas).
        self.assertIn('if [ "${DB_WAS_ABSENT}" = "1" ]', rollback)
        self.assertIn("dropdb --if-exists", rollback)
        # Los fallos detectados a mano disparan rollback (fail), no exit pelado.
        self.assertIn('fail() { echo "$1"; rollback; }', script)
        for marker in ("PCM_ERR_NO_BOOTSTRAP", "PCM_ERR_PORT_BUSY",
                       "PCM_ERR_DIRTY_SLUG", "PCM_ERR_DB_EXISTS",
                       "PCM_ERR_INSTANCE_UNHEALTHY", "PCM_ERR_NEIGHBOR_DOWN"):
            self.assertIn('fail "%s' % marker, script)

    def test_guardas_antes_de_crear_y_vecinos(self):
        script = self.server._build_instance_install_script(
            self.inst_b, self._params("cliente_b_db"), is_default=False)
        # Guardas ANTES de cualquier creación.
        first_create = script.index("CREATED_USER=1") if "CREATED_USER=1" in script else len(script)
        first_create = min(first_create, script.index("mkdir -p \"${INST_DIR}"))
        for guard in ("PCM_ERR_NO_BOOTSTRAP", "PCM_ERR_PORT_BUSY",
                      "PCM_ERR_DIRTY_SLUG", "PCM_ERR_DISK", "PCM_ERR_DB_EXISTS"):
            self.assertLess(script.index(guard), first_create)
        # ss -ltn con los puertos REALES de la instancia.
        self.assertIn("for puerto in 8079 8082", script)
        # Vecinos: snapshot antes + re-verificación después.
        self.assertIn("Vecinos corriendo antes", script)
        self.assertIn("PCM_ERR_NEIGHBOR_DOWN", script)
        # Runtime compartido bajo flock (dos installs no compilan a la vez).
        self.assertIn("flock", script)
        self.assertIn("runtime/odoo-19", script)

    def test_is_default_solo_primera_instancia(self):
        con = self.server._build_instance_install_script(
            self.inst_a, self._params("cliente_a_db"), is_default=True)
        sin = self.server._build_instance_install_script(
            self.inst_b, self._params("cliente_b_db"), is_default=False)
        self.assertIn("default_server", con)
        # El branch no-default existe en ambos renders (el template trae los
        # dos); lo que decide es el token IS_DEFAULT renderizado.
        self.assertIn('IS_DEFAULT="1"', con)
        self.assertIn('IS_DEFAULT="0"', sin)

    def test_credenciales_jamas_en_lineas_de_log(self):
        script = self.server._build_instance_install_script(
            self.inst_b, self._params("cliente_b_db"), is_default=False)
        for line in script.splitlines():
            if "log " in line or line.strip().startswith("echo"):
                self.assertNotIn("PgPass_token-urlsafe-XYZ", line,
                                 "la contraseña PG apareció en una línea de log")

    def test_validaciones_de_builder_cortan_input_inseguro(self):
        # Slug raro → corta antes de renderizar (nada viaja al shell).
        self.inst_b.slug = "malo;rm -rf /"
        with self.assertRaises(UserError):
            self.server._build_instance_install_script(
                self.inst_b, self._params("cliente_b_db"), is_default=False)
        self.inst_b.slug = "cliente-b"
        with self.assertRaises(UserError):
            self.server._build_instance_install_script(
                self.inst_b, self._params("db;drop"), is_default=False)
        with self.assertRaises(UserError):
            self.server._build_instance_install_script(
                self.inst_b, self._params("okdb", db_password="con'comilla"),
                is_default=False)

    def test_bootstrap_renderiza_y_marca_al_final(self):
        script = self.env["primate.cloud.environment"]._build_bootstrap_script(
            swap_mb=1024)
        self.assertIn('SWAP_MB="1024"', script)
        self.assertIn("postgresql", script)
        self.assertIn("rm -f /etc/nginx/sites-enabled/default", script)
        # El marker se escribe al FINAL (si algo falló, el bootstrap no quedó
        # marcado como aplicado).
        self.assertGreater(script.index('touch "${MARKER}"'),
                           script.index("systemctl reload nginx"))
        self.assertIn("PCM_BOOTSTRAP_OK", script)

    def test_scripts_renderizados_pasan_bash_n(self):
        # Validación de sintaxis REAL de bash sobre los renders finales.
        if not shutil.which("bash"):
            self.skipTest("bash no disponible")
        renders = [
            self.env["primate.cloud.environment"]._build_bootstrap_script(),
            self.server._build_instance_install_script(
                self.inst_b, self._params("cliente_b_db"), is_default=False),
        ]
        for script in renders:
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
                handle.write(script)
                handle.flush()
                result = subprocess.run(["bash", "-n", handle.name],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
