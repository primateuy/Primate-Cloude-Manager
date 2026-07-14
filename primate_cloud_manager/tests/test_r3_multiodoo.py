# -*- coding: utf-8 -*-
"""R3 multi-Odoo: goldens de B1 (scripts renderizados) + orquestación de B2.

El golden más crítico del recableo es el de aislamiento multi-tenant
(``test_conf_real_aisla_multitenant_cada_instancia``): valida que el conf
REAL que aterriza en el servidor —no el template— lleve ``db_filter`` y
``list_db`` correctos para CADA instancia. Es la barrera contra un incidente
de privacidad entre clientes en un servidor compartido: si un cambio futuro
lo afloja, la suite se pone roja.

B2 agrega la orquestación: allocator de puertos atómico, lock por servidor,
``job_bootstrap_server``/``job_add_instance`` con snapshot de salud
antes/después (el mecanismo de no-romper-lo-ajeno que la prueba real de
§10 ejercita) y la cadena R2 adoptando el layout por slug.
"""
import re
import shutil
import subprocess
import tempfile
import zlib
from unittest import mock

import requests as requests_lib

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.primate_cloud_manager.services import aws_ssm


class FakeSsm:
    """SSM falso: registra cada run_script y responde por comment.

    ``responses`` mapea un fragmento del ``comment`` → dict de salida; sin
    match devuelve Success con TODOS los marcadores (camino feliz simple).
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run_script(self, instance_id, script, **kwargs):
        comment = kwargs.get("comment") or ""
        self.calls.append({"instance_id": instance_id, "script": script,
                           "comment": comment})
        for fragment, response in self.responses.items():
            if fragment in comment:
                return dict(response)
        return {"status": "Success", "command_id": "cmd-1",
                "stdout": "PCM_BOOTSTRAP_OK\nPCM_INSTALL_OK\n"
                          "PCM_TEARDOWN_DONE"}


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


@tagged("post_install", "-at_install", "primate_cloud")
class TestR3B2PuertosYLock(TransactionCase):
    """Allocator de puertos (slots D-R3.8) + claves de lock por servidor."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        # La primaria nace con el slot 0 (defaults legacy 8069/8072).
        self.primary = self.server.primary_instance_id

    def _nueva(self, nombre, **vals):
        base = {"name": nombre, "project_id": self.project.id,
                "environment_id": self.server.id, "odoo_version": "19"}
        base.update(vals)
        return self.env["primate.cloud.instance"].create(base)

    def test_allocate_ports_primer_slot_libre(self):
        self.assertEqual(self.server._allocate_ports(), (8079, 8082))

    def test_allocate_ports_no_recicla_slots_de_archivadas(self):
        # Una instancia archivada RETIENE su slot (D-R3.8: sin reciclaje en
        # v1 — una unit residual en el servidor no puede chocar con un slot
        # re-asignado).
        archivada = self._nueva("Vieja", http_port=8079, gevent_port=8082)
        archivada.write({"state": "archived", "active": False})
        self.assertEqual(self.server._allocate_ports(), (8089, 8092))

    def test_allocate_ports_usa_hueco_nunca_asignado(self):
        # Un hueco que NUNCA se asignó sí es usable (no es un slot reciclado).
        self._nueva("Salteada", http_port=8089, gevent_port=8092)
        self.assertEqual(self.server._allocate_ports(), (8079, 8082))

    def test_allocate_ports_toma_el_xact_lock(self):
        # La atomicidad: la asignación corre bajo pg_advisory_xact_lock (se
        # libera al commit) → dos asignaciones concurrentes se serializan y
        # la 2ª ve lo commiteado por la 1ª. Se verifica el lock REAL en
        # pg_locks (clave = env.id<<32 | crc32('port-alloc')).
        self.server._allocate_ports()
        key = self.server._server_lock_key("port-alloc")
        self.env.cr.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND granted AND classid = %s AND objid = %s",
            (key >> 32, key & 0xFFFFFFFF))
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_create_instance_with_ports_asigna_slot(self):
        inst = self.server._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.project.id,
            "odoo_version": "19"})
        self.assertEqual((inst.http_port, inst.gevent_port), (8079, 8082))
        # La siguiente ve la anterior (misma transacción: search la flushea).
        inst2 = self.server._create_instance_with_ports({
            "name": "Odoo C", "project_id": self.project.id,
            "odoo_version": "19"})
        self.assertEqual((inst2.http_port, inst2.gevent_port), (8089, 8092))

    def test_server_lock_key_determinista_sin_hash(self):
        # Clave estable entre procesos: crc32, no hash() (varía por seed).
        esperado = (self.server.id << 32) | (
            zlib.crc32(b"instance-install") & 0xFFFFFFFF)
        self.assertEqual(self.server._server_lock_key("instance-install"),
                         esperado)
        self.assertEqual(self.server._server_lock_key("instance-install"),
                         self.server._server_lock_key("instance-install"))
        # Alcances y servidores distintos → claves distintas.
        self.assertNotEqual(self.server._server_lock_key("instance-install"),
                            self.server._server_lock_key("port-alloc"))
        otro = self.env["primate.cloud.environment"].create({
            "name": "Otro", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19"})
        self.assertNotEqual(otro._server_lock_key("instance-install"),
                            self.server._server_lock_key("instance-install"))


@tagged("post_install", "-at_install", "primate_cloud")
class TestR3B2Jobs(TransactionCase):
    """job_add_instance / job_bootstrap_server: la orquestación de B2."""

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
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b2", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        # A = primaria viva (el vecino a proteger); B = la nueva, de OTRO cliente.
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "state": "active",
                           "main_url": "a.pcm.test"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "Odoo Cliente B", "project_id": self.project_b.id,
            "slug": "cliente-b", "pg_user": "odoo_cliente_b",
            "odoo_version": "19", "odoo_edition": "community",
        })

    def _params(self, **overrides):
        params = {
            "db_mode": "local_pg", "db_name": "cliente_b_db",
            "db_password": "PgPass_token-urlsafe-XYZ",
            "domain": "b.pcm.test", "admin_password": "admin-x", "workers": 0,
        }
        params.update(overrides)
        return params

    def _run_job(self, fake_ssm, alive_sequence, params=None):
        """Corre job_add_instance con AWS/SSM falsos y salud controlada."""
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
             mock.patch.object(aws_ssm, "AwsSsmService",
                               return_value=fake_ssm), \
             mock.patch.object(type(self.server), "_instance_http_alive",
                               side_effect=alive_sequence) as probe:
            ok = self.server.job_add_instance(
                self.inst_b.id, params or self._params())
        return ok, probe

    def test_enqueue_add_instance_valida_y_genera_password(self):
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.server), "with_delay",
                               return_value=fake_delay):
            self.server._enqueue_add_instance(
                self.inst_b, {"db_mode": "local_pg", "db_name": "x"})
        args = fake_delay.job_add_instance.call_args[0]
        self.assertEqual(args[0], self.inst_b.id)
        # La contraseña PG transitoria se genera al ENCOLAR (requeue = misma).
        self.assertTrue(args[1]["db_password"])
        # v1: solo BD local del servidor.
        with self.assertRaises(UserError):
            self.server._enqueue_add_instance(self.inst_b, {"db_mode": "rds"})
        # Servidor no activo → no se encola.
        self.server.state = "error"
        with self.assertRaises(UserError):
            self.server._enqueue_add_instance(
                self.inst_b, {"db_mode": "local_pg"})

    def test_job_add_instance_feliz(self):
        fake = FakeSsm()
        ok, probe = self._run_job(fake, alive_sequence=[True, True])
        self.assertTrue(ok)
        self.assertEqual(self.inst_b.state, "active")
        self.assertEqual(self.inst_b.main_url, "b.pcm.test")
        # El job materializó el layout por slug en el REGISTRO (D-R3.10:
        # pg_user propio; rutas por instancia para el bookkeeping de R4).
        self.assertEqual(self.inst_b.service_name, "odoo-cliente-b")
        self.assertEqual(self.inst_b.pg_user, "odoo_cliente_b")
        self.assertEqual(self.inst_b.conf_path, "/etc/odoo/cliente-b.conf")
        self.assertEqual(self.inst_b.data_dir,
                         "/opt/pcm/instances/cliente-b/data")
        # Snapshot ANTES + re-verificación DESPUÉS del único vecino vivo.
        self.assertEqual(probe.call_count, 2)
        # Orden de las mutaciones por SSM: bootstrap → install (nada más).
        comments = [c["comment"] for c in fake.calls]
        self.assertIn("pcm bootstrap", comments[0])
        self.assertIn("pcm instance install", comments[1])
        self.assertEqual(len(fake.calls), 2)
        install = fake.calls[1]["script"]
        self.assertIn('SLUG="cliente-b"', install)
        self.assertIn('IS_DEFAULT="0"', install)  # ya hay una primaria
        # La BD quedó registrada y atada a la instancia NUEVA (no a la primaria).
        db = self.env["primate.cloud.database"].search(
            [("environment_id", "=", self.server.id),
             ("name", "=", "cliente_b_db")])
        self.assertEqual(db.instance_id, self.inst_b)
        self.assertEqual(self.inst_b.database_id, db)
        self.assertTrue(self.server.multiodoo_ready)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "instance_install"),
             ("result", "=", "success"),
             ("resource_id", "=", self.inst_b.id)])
        self.assertTrue(log)

    def test_job_add_instance_no_confia_el_cache_de_bootstrap(self):
        # Regla permanente: aunque el caché diga listo, el bootstrap se
        # re-corre por SSM (el script no-opea sobre el marker REAL en disco).
        self.server.multiodoo_ready = True
        fake = FakeSsm()
        ok, _probe = self._run_job(fake, alive_sequence=[True, True])
        self.assertTrue(ok)
        self.assertIn("pcm bootstrap", fake.calls[0]["comment"])

    def test_job_add_instance_vecino_roto_desmonta_la_nueva(self):
        # EL contrato de no-romper-lo-ajeno: A servía ANTES y no sirve
        # DESPUÉS → failed + teardown de B (los vecinos no se tocan).
        fake = FakeSsm()
        ok, probe = self._run_job(fake, alive_sequence=[True, False])
        self.assertFalse(ok)
        self.assertEqual(self.inst_b.state, "error")
        comments = [c["comment"] for c in fake.calls]
        self.assertIn("pcm instance teardown", comments[-1])
        teardown = fake.calls[-1]["script"]
        self.assertIn("cliente-b", teardown)
        self.assertIn("dropdb", teardown)
        # El fallo dice QUÉ vecino se rompió, con nombre y apellido.
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "instance_install"),
             ("result", "=", "failed")], limit=1, order="id desc")
        self.assertIn(self.inst_a.name, log.error_message)

    def test_job_add_instance_vecino_ya_muerto_no_bloquea(self):
        # Lo que YA estaba roto antes no se le achaca al install (patrón
        # PCM_HTTP_WAS): muerto-antes → no se re-verifica ni bloquea.
        fake = FakeSsm()
        ok, probe = self._run_job(fake, alive_sequence=[False])
        self.assertTrue(ok)
        self.assertEqual(self.inst_b.state, "active")
        self.assertEqual(probe.call_count, 1)

    def test_job_add_instance_bootstrap_falla_no_instala(self):
        fake = FakeSsm(responses={"pcm bootstrap": {
            "status": "Failed", "stdout": "", "stderr": "boom"}})
        ok, _probe = self._run_job(fake, alive_sequence=[True, True])
        self.assertFalse(ok)
        self.assertEqual(self.inst_b.state, "error")
        # Nada más se corrió: ni snapshot SSM ni install.
        self.assertEqual(len(fake.calls), 1)

    def test_job_add_instance_serializa_bajo_el_lock_del_servidor(self):
        fake = FakeSsm()
        with mock.patch.object(type(self.server), "_acquire_server_lock",
                               autospec=True,
                               return_value="LLAVE") as acquire, \
             mock.patch.object(type(self.server), "_release_server_lock",
                               autospec=True) as release:
            self._run_job(fake, alive_sequence=[True, True])
        acquire.assert_called_once_with(self.server, "instance-install")
        release.assert_called_once_with(self.server, "LLAVE")

    def test_job_add_instance_libera_el_lock_ante_fallo(self):
        fake = FakeSsm(responses={"pcm bootstrap": {
            "status": "Failed", "stdout": "", "stderr": "boom"}})
        with mock.patch.object(type(self.server), "_acquire_server_lock",
                               autospec=True, return_value="LLAVE"), \
             mock.patch.object(type(self.server), "_release_server_lock",
                               autospec=True) as release:
            self._run_job(fake, alive_sequence=[True, True])
        release.assert_called_once_with(self.server, "LLAVE")

    def test_job_bootstrap_server_marca_cache(self):
        fake = FakeSsm()
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
             mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            ok = self.server.job_bootstrap_server()
        self.assertTrue(ok)
        self.assertTrue(self.server.multiodoo_ready)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "server_bootstrap"),
             ("result", "=", "success")])
        self.assertTrue(log)

    def test_teardown_golden_orden_y_guardas(self):
        script = self.server._build_instance_teardown_script(
            self.inst_b, db_name="cliente_b_db", drop_db=True)
        # Mismo orden que el trap: unit → site → dropdb → dropuser → dirs.
        order = [script.index("systemctl stop"),
                 script.index("sites-enabled/pcm-cliente-b.conf"),
                 script.index("dropdb"), script.index("dropuser"),
                 script.index('rm -rf "/opt/pcm/instances/cliente-b"')]
        self.assertEqual(order, sorted(order))
        # Sin drop_db, la BD no se toca.
        sin_db = self.server._build_instance_teardown_script(
            self.inst_b, drop_db=False)
        self.assertNotIn("dropdb", sin_db)
        # JAMÁS dropea el usuario PG compartido 'odoo' (legacy).
        self.inst_b.pg_user = "odoo"
        legacy = self.server._build_instance_teardown_script(
            self.inst_b, drop_db=False)
        self.assertNotIn("dropuser", legacy)
        # Sintaxis bash real.
        if shutil.which("bash"):
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
                handle.write(script)
                handle.flush()
                result = subprocess.run(["bash", "-n", handle.name],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_instance_http_alive_probe_externo_y_fallback(self):
        ssm = FakeSsm(responses={"pcm health probe": {
            "status": "Success", "stdout": "200"}})
        # Externo (IP + Host): valida también el ruteo nginx.
        self.machine.public_ip = "1.2.3.4"
        with mock.patch.object(requests_lib, "get",
                               return_value=mock.Mock(status_code=200)) as get:
            self.assertTrue(self.server._instance_http_alive(
                ssm, self.machine, "us-east-1", self.inst_a))
        self.assertEqual(get.call_args[1]["headers"]["Host"], "a.pcm.test")
        with mock.patch.object(requests_lib, "get",
                               return_value=mock.Mock(status_code=502)):
            self.assertFalse(self.server._instance_http_alive(
                ssm, self.machine, "us-east-1", self.inst_a))
        with mock.patch.object(
                requests_lib, "get",
                side_effect=requests_lib.ConnectionError("x")):
            self.assertFalse(self.server._instance_http_alive(
                ssm, self.machine, "us-east-1", self.inst_a))
        # Sin dominio → fallback por SSM al puerto local.
        self.assertTrue(self.server._instance_http_alive(
            ssm, self.machine, "us-east-1", self.inst_b))
        # curl imprime 000 cuando no conecta: eso es MUERTO.
        ssm_muerto = FakeSsm(responses={"pcm health probe": {
            "status": "Success", "stdout": "000"}})
        self.assertFalse(self.server._instance_http_alive(
            ssm_muerto, self.machine, "us-east-1", self.inst_b))


@tagged("post_install", "-at_install", "primate_cloud")
class TestR3B2Cadena(TransactionCase):
    """La cadena R2 (crear entorno) adopta el layout multi-Odoo (D-R3.1)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-chain", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "provisioning"

    def test_cadena_instala_primaria_por_slug(self):
        fake = FakeSsm()
        params = {
            "db_mode": "local_pg", "db_name": "forum",
            "db_password": "PgPass_token-urlsafe-XYZ",
            "domain": "forum.pcm.test", "admin_password": "x",
            "create_dns": False, "region": "us-east-1",
        }
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
             mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            ok = self.server.job_install_instance(params)
        self.assertTrue(ok)
        self.assertEqual(self.server.state, "active")
        # Bootstrap primero, install por slug después.
        self.assertIn("pcm bootstrap", fake.calls[0]["comment"])
        self.assertIn("pcm instance install", fake.calls[1]["comment"])
        install = fake.calls[1]["script"]
        primaria = self.server.primary_instance_id
        self.assertIn('SLUG="%s"' % primaria.slug, install)
        self.assertIn("db_filter = ^forum$", install)
        self.assertIn('IS_DEFAULT="1"', install)  # primera instancia
        # La instancia quedó MATERIALIZADA en el layout por slug.
        self.assertEqual(primaria.state, "active")
        self.assertEqual(primaria.service_name, "odoo-%s" % primaria.slug)
        self.assertEqual(primaria.conf_path, "/etc/odoo/%s.conf" % primaria.slug)
        self.assertEqual(primaria.data_dir,
                         "/opt/pcm/instances/%s/data" % primaria.slug)
        self.assertEqual(primaria.pg_user,
                         "odoo_%s" % primaria.slug.replace("-", "_"))
        self.assertTrue(self.server.multiodoo_ready)

    def test_cadena_install_fallido_recuperable(self):
        # PCM_ERR del script → entorno+instancia en error, con el marcador
        # accionable en la bitácora (retry por requeue, R2 intacto).
        fake = FakeSsm(responses={"pcm instance install": {
            "status": "Failed",
            "stdout": "PCM_ERR_PORT_BUSY: el puerto 8069 ya está en uso.\n"
                      "PCM_ROLLBACK_DONE"}})
        params = {"db_mode": "local_pg", "db_name": "forum",
                  "db_password": "PgPass_token-urlsafe-XYZ",
                  "domain": "forum.pcm.test", "create_dns": False}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
             mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            ok = self.server.job_install_instance(params)
        self.assertFalse(ok)
        self.assertEqual(self.server.state, "error")
        self.assertEqual(self.server.primary_instance_id.state, "error")
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "ssm_command"), ("result", "=", "failed")],
            limit=1, order="id desc")
        self.assertIn("PCM_ERR_PORT_BUSY", log.error_message)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR3B3Wizard(TransactionCase):
    """Wizard «Agregar instancia» + gate por layout (D-R3.2)."""

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
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b3", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "instance_type": "t3.micro",
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        # Primaria YA instalada con el layout multi-Odoo (unit por slug).
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "state": "active",
                           "service_name": "odoo-cliente-a",
                           "main_url": "a.pcm.test"})

    def _wizard(self, **overrides):
        vals = {
            "environment_id": self.server.id, "project_id": self.project_b.id,
            "name": "Odoo Cliente B", "odoo_version": "19",
            "odoo_edition": "community", "domain": "b.pcm.test",
            "db_name": "cliente_b_db", "admin_password": "admin-x",
            "workers": 0,
        }
        vals.update(overrides)
        return self.env["primate.cloud.instance.create.wizard"].create(vals)

    # --- Gate por layout -------------------------------------------------
    def test_gate_multiodoo_abre_el_wizard(self):
        action = self.server.action_create_instance()
        self.assertEqual(action["res_model"],
                         "primate.cloud.instance.create.wizard")
        self.assertEqual(action["context"]["default_environment_id"],
                         self.server.id)

    def test_gate_servidor_virgen_abre_el_wizard(self):
        # Sin ningún Odoo instalado (primaria draft con defaults legacy) el
        # servidor NO es legacy: es virgen y puede nacer multi-Odoo.
        self.inst_a.write({"state": "draft", "service_name": "odoo"})
        action = self.server.action_create_instance()
        self.assertEqual(action["res_model"],
                         "primate.cloud.instance.create.wizard")

    def test_gate_legacy_sigue_gated_con_motivo_nuevo(self):
        # Un Odoo ACTIVO con la unit compartida 'odoo' = layout pre-R3
        # (D-R3.2: adopción in-place pendiente, no se le monta un segundo).
        self.inst_a.service_name = "odoo"
        with self.assertRaises(UserError) as ctx:
            self.server.action_create_instance()
        self.assertIn("legacy", str(ctx.exception))

    def test_gate_exige_servidor_activo(self):
        self.server.state = "error"
        with self.assertRaises(UserError):
            self.server.action_create_instance()

    # --- Wizard ----------------------------------------------------------
    def test_preview_de_puertos_muestra_el_slot_probable(self):
        wizard = self._wizard()
        self.assertEqual(wizard.http_port_preview, 8079)
        self.assertEqual(wizard.gevent_port_preview, 8082)

    def test_ram_warning_advisoria(self):
        # t3.micro (1 GB) con 1 Odoo vivo: el 2º dispara la advertencia.
        wizard = self._wizard()
        self.assertTrue(wizard.ram_warning)
        self.assertIn("t3.micro", wizard.ram_warning)
        # Con RAM de sobra (o tipo desconocido), sin advertencia.
        self.machine.instance_type = "t3.large"
        self.assertFalse(self._wizard().ram_warning)
        self.machine.instance_type = "x9.desconocido"
        self.assertFalse(self._wizard().ram_warning)

    def test_valida_nombre_de_base(self):
        with self.assertRaises(UserError):
            self._wizard(db_name="malo;drop").action_add_instance()

    def test_valida_base_duplicada_en_el_servidor(self):
        self.env["primate.cloud.database"].create({
            "name": "cliente_b_db", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
        })
        with self.assertRaises(UserError) as ctx:
            self._wizard().action_add_instance()
        self.assertIn("cliente_b_db", str(ctx.exception))

    def test_valida_slug_duplicado_incluye_archivadas(self):
        # "Cliente A" slugifica a "cliente-a" (la primaria) → choque claro.
        with self.assertRaises(UserError):
            self._wizard(name="Cliente A").action_add_instance()
        # También contra archivadas (los slots/slugs no se reciclan).
        self.inst_a.write({"state": "archived", "active": False})
        with self.assertRaises(UserError):
            self._wizard(name="Cliente A").action_add_instance()

    def test_valida_legacy_tambien_server_side(self):
        # La defensa no vive solo en el gate del botón: el confirm re-valida.
        self.inst_a.service_name = "odoo"
        with self.assertRaises(UserError):
            self._wizard().action_add_instance()

    def test_confirma_crea_instancia_con_slot_y_encola(self):
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.server), "with_delay",
                               return_value=fake_delay):
            result = self._wizard().action_add_instance()
        instance = self.env["primate.cloud.instance"].search(
            [("environment_id", "=", self.server.id),
             ("slug", "=", "odoo-cliente-b")])
        self.assertTrue(instance)
        self.assertEqual(instance.state, "draft")
        self.assertEqual(instance.project_id, self.project_b)
        self.assertEqual((instance.http_port, instance.gevent_port),
                         (8079, 8082))
        self.assertEqual(instance.main_url, "b.pcm.test")
        # Encolado con la contraseña PG transitoria generada al encolar.
        args = fake_delay.job_add_instance.call_args[0]
        self.assertEqual(args[0], instance.id)
        self.assertEqual(args[1]["db_mode"], "local_pg")
        self.assertEqual(args[1]["db_name"], "cliente_b_db")
        self.assertTrue(args[1]["db_password"])
        # Cierra con notificación (en el drawer: toast, no modal).
        self.assertEqual(result["tag"], "display_notification")

    def test_dns_exige_hosted_zone(self):
        with self.assertRaises(UserError):
            self._wizard(create_dns=True).action_add_instance()
