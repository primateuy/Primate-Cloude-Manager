# -*- coding: utf-8 -*-
"""Perfil PCM del montaje (mudanza 2026-11, reutilizable).

Goldens del script renderizado de ``install_instance.sh`` con
``pcm_profile``: pip extra en el venv del runtime, conf con el jobrunner de
queue_job y jobs largos (``limit_time_real = 14400``, lección bj-uat), y la
clave Fernet generada EN el servidor (EnvironmentFile): la clave no puede
viajar por stdout ni quedar en el historial de comandos SSM. Sin el perfil,
el conf que aterriza es EL MISMO de antes.
"""
import re

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestPerfilPcm(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "PrimateUY"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "PCM", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "pcm-self", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "pcm-self", "pg_user": "odoo_pcm_self",
                         "http_port": 8069, "gevent_port": 8072})

    def _params(self, **overrides):
        params = {
            "db_mode": "local_pg", "db_name": "pcm_produccion",
            "db_password": "PgPass_token-urlsafe-XYZ", "db_host": "localhost",
            "domain": "pcm.erp.primateuy.com", "admin_password": "admin-x",
        }
        params.update(overrides)
        return params

    def _script(self, **overrides):
        return self.server._build_instance_install_script(
            self.inst, self._params(**overrides), is_default=True)

    @staticmethod
    def _conf_base(script):
        match = re.search(r"cat > \"\$\{CONF\}\" <<'CONF'\n(.*?)\nCONF\n",
                          script, re.S)
        assert match, "no se encontró el heredoc del conf"
        return match.group(1)

    @staticmethod
    def _conf_perfil(script):
        match = re.search(
            r"cat >> \"\$\{CONF\}\" <<'CONFPCM'\n(.*?)\nCONFPCM\n", script, re.S)
        assert match, "no se encontró el heredoc del conf del perfil"
        return match.group(1)

    # ------------------------------------------------------------------
    # Sin perfil: nada cambia respecto del golden histórico
    # ------------------------------------------------------------------
    def test_sin_perfil_el_conf_es_el_de_siempre(self):
        script = self._script(workers=0)
        self.assertIn('PCM_PROFILE="0"', script)
        conf = self._conf_base(script)
        self.assertNotIn("queue_job", conf)
        self.assertNotIn("server_wide_modules", conf)
        self.assertIn("workers = 0", conf)

    def test_unit_lleva_environmentfile_opcional(self):
        # El «-» hace la línea inocua sin perfil; con perfil carga la clave.
        script = self._script()
        self.assertIn("EnvironmentFile=-/etc/odoo/pcm-self.env", script)

    # ------------------------------------------------------------------
    # Con perfil: conf del jobrunner + jobs largos
    # ------------------------------------------------------------------
    def test_perfil_conf_jobrunner_y_jobs_largos(self):
        from odoo.tools import config
        script = self._script(pcm_profile=True)
        self.assertIn('PCM_PROFILE="1"', script)
        extra = self._conf_perfil(script)
        # Las claves de [options] van ANTES del header [queue_job] (siguen en
        # la sección [options] del conf ya escrito).
        opciones, _sep, queue = extra.partition("[queue_job]")
        self.assertTrue(queue, "falta la sección [queue_job]")
        self.assertIn("server_wide_modules = base,web,queue_job", opciones)
        self.assertIn("limit_time_real = 14400", opciones)      # lección bj-uat
        self.assertIn("limit_time_real_cron = -1", opciones)
        self.assertIn("max_cron_threads = 2", opciones)
        self.assertIn("channels = root:2", queue)
        # Golden de claves: todo lo agregado a [options] es una opción REAL de
        # Odoo (una clave inventada se ignora en silencio — bug db_filter).
        claves = [l.split("=", 1)[0].strip() for l in opciones.splitlines()
                  if "=" in l and not l.startswith(("[", "#", ";"))]
        desconocidas = [c for c in claves if c not in config.options]
        self.assertFalse(desconocidas, "claves inventadas: %s" % desconocidas)

    def test_perfil_workers_2_por_defecto_explicito_se_respeta(self):
        # Sin dato → 2 con perfil (Linux + jobrunner), 0 sin perfil.
        self.assertIn("workers = 2",
                      self._conf_base(self._script(pcm_profile=True)))
        self.assertIn("workers = 0", self._conf_base(self._script()))
        # Un valor explícito manda.
        self.assertIn("workers = 3",
                      self._conf_base(self._script(pcm_profile=True, workers=3)))

    def test_perfil_pip_extra_en_el_runtime(self):
        script = self._script(pcm_profile=True)
        pip = re.search(
            r'if \[ "\$\{PCM_PROFILE\}" = "1" \]; then\n(.*?)\nfi', script, re.S)
        self.assertTrue(pip, "falta el bloque gateado del perfil")
        self.assertIn("pip\" install", script)
        for dep in ("boto3", "botocore", "paramiko", "cryptography", "PyGithub"):
            self.assertIn(dep, script)

    def test_perfil_clona_queue_job_antes_del_arranque(self):
        """server_wide_modules = queue_job exige el módulo en el path ANTES
        del primer arranque: sin este clone el health check moriría siempre."""
        script = self._script(pcm_profile=True)
        self.assertIn("https://github.com/OCA/queue", script)
        self.assertIn('ln -s "../repos/oca-queue/queue_job"', script)
        self.assertLess(script.index("github.com/OCA/queue"),
                        script.index("systemctl enable --now"),
                        "el clone tiene que correr antes de arrancar la unit")

    # ------------------------------------------------------------------
    # La clave Fernet: nace en el servidor y NO viaja
    # ------------------------------------------------------------------
    def test_clave_fernet_no_viaja_por_ssm(self):
        script = self._script(pcm_profile=True)
        # Se genera en el server y aterriza DIRECTO en el env file.
        self.assertIn("Fernet.generate_key()", script)
        self.assertIn('> "${ENVF}"', script)
        self.assertIn('printf \'PRIMATE_CLOUD_FERNET_KEY=%s\\n\'', script)
        # Nunca por echo/stdout (el stdout de SSM queda ~30 días en la cuenta).
        for linea in script.splitlines():
            if "Fernet.generate_key" in linea:
                self.assertNotIn("echo", linea)
        # Permisos cerrados y sin pisar una clave existente.
        self.assertIn('chmod 640 "${ENVF}"', script)
        self.assertIn('[ ! -f "${ENVF}" ]', script)

    def test_rollback_borra_el_env_solo_si_nacio_en_esta_corrida(self):
        script = self._script(pcm_profile=True)
        rollback = script[script.index("rollback() {"):script.index("trap rollback ERR")]
        self.assertIn('if [ "${CREATED_ENV}" = "1" ]; then', rollback)
        self.assertIn('rm -f "/etc/odoo/${SLUG}.env"', rollback)

    # ------------------------------------------------------------------
    # Bootstrap: aws-cli con reintentos (snap store flaky — 408 real)
    # ------------------------------------------------------------------
    def test_bootstrap_awscli_reintenta_y_falla_ruidoso(self):
        script = self.server._build_bootstrap_script()
        # snap con pocos intentos (espera creciente) + FALLBACK al zip
        # oficial de AWS: el snap store tumbó 3 montajes reales en 2 días.
        self.assertIn("for intento in 1 2 3", script)
        self.assertIn("espera=$((espera + espera / 2))", script)
        self.assertIn("awscli.amazonaws.com/awscli-exe-linux-x86_64.zip", script)
        self.assertIn("/tmp/awscliv2/aws/install", script)
        self.assertIn("PCM_ERR_BOOTSTRAP_AWSCLI", script)
        # El marker sigue al FINAL (si algo falló, el bootstrap NO quedó aplicado).
        self.assertLess(script.index("PCM_ERR_BOOTSTRAP_AWSCLI"),
                        script.index('touch "${MARKER}"'))

    # ------------------------------------------------------------------
    # Los wizards pasan el perfil
    # ------------------------------------------------------------------
    def test_wizard_provision_pasa_el_perfil(self):
        wiz = self.env["primate.cloud.provision.wizard"].create({
            "environment_id": self.server.id, "region": "us-east-1",
            "db_mode": "local_pg", "db_name": "pcm_produccion",
            "pcm_profile": True,
        })
        self.assertTrue(wiz._prepare_params()["pcm_profile"])
        # Y se recuerda al reintentar (config persistida).
        self.assertIn("pcm_profile", wiz._raw_values())

    def test_wizard_instancia_pasa_el_perfil(self):
        from unittest import mock
        self.server.state = "active"
        wiz = self.env["primate.cloud.instance.create.wizard"].create({
            "environment_id": self.server.id, "project_id": self.project.id,
            "name": "PCM Dos", "db_name": "pcm_dos", "domain": "dos.pcm.test",
            "admin_password": "x", "pcm_profile": True,
        })
        capturado = {}
        with mock.patch.object(
                type(self.server), "_enqueue_add_instance",
                side_effect=lambda inst, params: capturado.update(params)), \
             mock.patch.object(
                type(self.server), "_create_instance_with_ports",
                return_value=self.inst):
            wiz.action_add_instance()
        self.assertTrue(capturado.get("pcm_profile"))
