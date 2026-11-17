# -*- coding: utf-8 -*-
"""Montaje a la carta (Alta na Pipa 2026-11, opciones reutilizables del entorno).

Cubre los 5 gaps + extras del runbook:
a. wkhtmltopdf desde el release de GitHub (versión configurable, verificada).
b. Módulos iniciales + idioma (-i … --load-language=… antes de arrancar).
c. Paquetes pip extra por entorno (el perfil PCM pasa por el mismo bloque).
d. addons_path por repo con orden explícito (sequence) — chau symlinks.
e. Verificación post-instalación contra archivo de referencia del repo.
+ http_interface = 127.0.0.1, limit_time_* como tokens, nginx del runbook
  (client_max_body_size, caché de estáticos, proxy_redirect off) y el
  guardado de config con restart DIFERIDO cuando la instancia es self.

SSM siempre mockeado; los tests no tocan infra real.
"""
import re
import subprocess
import tempfile
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


class _BaseMontaje(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Alta na Pipa"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Alta", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "altanapipa", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "altanapipa", "pg_user": "odoo_altanapipa",
                         "http_port": 8069, "gevent_port": 8072})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
        })

    def _params(self, **overrides):
        params = {
            "db_mode": "local_pg", "db_name": "altanapipa",
            "db_password": "PgPass_token-urlsafe-XYZ", "db_host": "localhost",
            "domain": "altanapipa.primatecloudmanager.com",
            "admin_password": "admin-x",
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

    def _fake_ssm(self, output=None):
        fake = mock.Mock()
        fake.run_script.return_value = output or {
            "stdout": "", "status": "Success"}
        return fake


# ----------------------------------------------------------------------------
# Conf + instalación inicial + pip (gaps b, c y extras del runbook)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestMontajeALaCarta(_BaseMontaje):

    def test_conf_endurecido_por_defecto(self):
        """http_interface + limit_time_* entran al conf de TODA instancia."""
        conf = self._conf_base(self._script())
        self.assertIn("http_interface = 127.0.0.1", conf)
        self.assertIn("limit_time_cpu = 600", conf)
        self.assertIn("limit_time_real = 1200", conf)

    def test_conf_limit_time_configurable(self):
        conf = self._conf_base(self._script(limit_time_cpu=300,
                                            limit_time_real=900))
        self.assertIn("limit_time_cpu = 300", conf)
        self.assertIn("limit_time_real = 900", conf)
        with self.assertRaises(UserError):
            self._script(limit_time_real=999999)

    def test_conf_sin_claves_duplicadas_con_perfil(self):
        """El perfil eleva limit_time_real VIA token: la clave aparece UNA sola
        vez en el conf final (una duplicada rompe el configparser de Odoo)."""
        script = self._script(pcm_profile=True)
        self.assertIn("limit_time_real = 14400", self._conf_base(script))
        self.assertEqual(script.count("limit_time_real ="), 1)

    def test_init_modules_e_idioma_renderizan(self):
        script = self._script(init_modules="alta_na_pipa_website,web_responsive",
                              load_language="pt_BR")
        self.assertIn('INIT_MODULES="alta_na_pipa_website,web_responsive"',
                      script)
        self.assertIn('LOAD_LANGUAGE="pt_BR"', script)
        self.assertIn("--stop-after-init", script)
        self.assertIn("PCM_ERR_INIT_MODULES", script)
        # El -i corre ANTES de arrancar la unit (la base nace completa) y
        # después del conf (usa ese conf).
        self.assertLess(script.index("PCM_ERR_INIT_MODULES"),
                        script.index("systemctl enable --now"))

    def test_init_modules_validaciones(self):
        with self.assertRaises(UserError):   # charset
            self._script(init_modules="a;rm -rf /")
        with self.assertRaises(UserError):   # idioma inválido
            self._script(init_modules="web", load_language="pt BR")
        with self.assertRaises(UserError):   # idioma sin módulos
            self._script(load_language="pt_BR")

    def test_init_modules_acepta_espacios_en_la_lista(self):
        script = self._script(init_modules="web, website")
        self.assertIn('INIT_MODULES="web,website"', script)

    def test_pip_extra_renderiza_y_verifica(self):
        script = self._script(pip_packages="openpyxl, requests==2.31.0")
        self.assertIn('PIP_PACKAGES="openpyxl requests==2.31.0"', script)
        self.assertIn("pip\" show", script)
        self.assertIn("PCM_ERR_PIP", script)

    def test_pip_extra_validacion_cierra_el_shell(self):
        for malo in ("openpyxl; rm -rf /", "-r evil.txt",
                     "git+https://x/y.git", "paquete==1.0 --pre"):
            with self.assertRaises(UserError, msg=malo):
                self._script(pip_packages=malo)

    def test_perfil_pcm_pasa_por_el_mismo_bloque_pip(self):
        """Perfil PCM + extras: UN solo bloque pip con el merge (sin duplicar)."""
        script = self._script(pcm_profile=True, pip_packages="openpyxl,boto3")
        match = re.search(r'PIP_PACKAGES="([^"]*)"', script)
        paquetes = match.group(1).split()
        self.assertEqual(paquetes, ["boto3", "botocore", "paramiko",
                                    "cryptography", "PyGithub", "openpyxl"])

    def test_sin_opciones_el_script_es_el_de_siempre(self):
        script = self._script()
        self.assertIn('PIP_PACKAGES=""', script)
        self.assertIn('INIT_MODULES=""', script)

    def test_nginx_extras_del_runbook(self):
        """client_max_body_size + caché de estáticos + proxy_redirect off, en
        AMBOS vhosts (default y secundario)."""
        for is_default in (True, False):
            script = self.server._build_instance_install_script(
                self.inst, self._params(), is_default=is_default)
            site = re.search(r"cat > \"\$\{SITE\}\" <<'NGINX'\n(.*?)\nNGINX\n",
                             script, re.S)
            # El script trae los dos vhosts; con el if bash solo corre uno —
            # acá validamos el texto completo renderizado.
            self.assertTrue(site)
            self.assertEqual(script.count("client_max_body_size 100M;"), 2)
            self.assertEqual(script.count("location ~ ^/(web/static/|website/image/)"), 2)
            self.assertEqual(script.count("proxy_redirect off;"), 2)
            self.assertEqual(script.count("location ^~ /web/database"), 2)

    def test_script_renderizado_pasa_bash_n(self):
        script = self._script(init_modules="a,b", load_language="pt_BR",
                              pip_packages="openpyxl", pcm_profile=True)
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
            handle.write(script)
            handle.flush()
            proc = subprocess.run(["bash", "-n", handle.name],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_install_timeout_crece_con_init_modules(self):
        """El -i de un sitio entero tarda: el run SSM acompaña (2700 vs 900)."""
        ssm = self._fake_ssm({"stdout": "PCM_INSTALL_OK", "status": "Success"})
        self.server._provision_run_instance_install(
            ssm, self.machine, self.inst, self._params(init_modules="web"),
            "us-east-1", "d.com", is_default=True)
        self.assertEqual(ssm.run_script.call_args.kwargs["timeout"], 2700)
        self.server._provision_run_instance_install(
            ssm, self.machine, self.inst, self._params(),
            "us-east-1", "d.com", is_default=True)
        self.assertEqual(ssm.run_script.call_args.kwargs["timeout"], 900)


# ----------------------------------------------------------------------------
# Montaje CON repos: clone en orden → addons_path → -i sobre los clones
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestMontajeConRepos(_BaseMontaje):

    def _repo(self, name, seq):
        return self.env["primate.cloud.repository"].create({
            "name": name, "environment_id": self.server.id,
            "github_url": "https://github.com/PrimateUy-SAS/%s" % name,
            "local_path": "/opt/pcm/instances/altanapipa/repos/%s" % name,
            "configured_branch": "19.0", "sequence": seq,
        })

    def test_clona_en_orden_activa_path_y_corre_i(self):
        self._repo("alta_na_pipa", 40)
        self._repo("webOCA", 10)
        self.inst.write({"slug": "altanapipa",
                         "service_name": "odoo-altanapipa",
                         "conf_path": "/etc/odoo/altanapipa.conf"})
        clones = []
        fake = self._fake_ssm({"stdout": "PCM_INIT_MODULES_OK",
                               "status": "Success"})
        with mock.patch.object(
                type(self.machine), "clone_addon",
                side_effect=lambda url, path, **kw: clones.append(path) or True), \
             mock.patch.object(type(self.machine), "_ensure_custom_addons_path",
                               return_value="restarted") as ensure, \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.server._provision_repos_e_instalacion_inicial(
                self.machine, self.inst,
                {"init_modules": "alta_na_pipa_website,web_responsive",
                 "load_language": "pt_BR"})
        # Orden EXPLÍCITO: webOCA (10) antes que alta_na_pipa (40).
        self.assertEqual(clones, [
            "/opt/pcm/instances/altanapipa/repos/webOCA",
            "/opt/pcm/instances/altanapipa/repos/alta_na_pipa"])
        ensure.assert_called_once()
        script = fake.run_script.call_args.args[1]
        self.assertIn("systemctl stop odoo-altanapipa", script)
        self.assertIn("-i alta_na_pipa_website,web_responsive", script)
        self.assertIn("--load-language=pt_BR", script)
        self.assertIn("systemctl start odoo-altanapipa", script)
        # El -i corre DETENIDO y el start va después.
        self.assertLess(script.index("stop"), script.index("-i "))
        self.assertLess(script.index("-i "), script.index("start"))

    def test_clone_fallido_corta_el_montaje(self):
        self._repo("webOCA", 10)
        with mock.patch.object(type(self.machine), "clone_addon",
                               return_value=False):
            with self.assertRaises(UserError):
                self.server._provision_repos_e_instalacion_inicial(
                    self.machine, self.inst, {"init_modules": "web"})

    def test_sin_init_modules_solo_clona_y_activa(self):
        self._repo("webOCA", 10)
        fake = self._fake_ssm()
        with mock.patch.object(type(self.machine), "clone_addon",
                               return_value=True), \
             mock.patch.object(type(self.machine), "_ensure_custom_addons_path",
                               return_value="restarted"), \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.server._provision_repos_e_instalacion_inicial(
                self.machine, self.inst, {})
        fake.run_script.assert_not_called()   # sin -i no hay paso extra


# ----------------------------------------------------------------------------
# wkhtmltopdf desde release (gap a)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestWkhtmltopdf(_BaseMontaje):

    def test_builder_valida_version(self):
        Env = self.env["primate.cloud.environment"]
        script = Env._build_wkhtmltopdf_script("0.12.6.1-3")
        self.assertIn('VERSION="0.12.6.1-3"', script)
        self.assertIn(
            "releases/download/${VERSION}/wkhtmltox_${VERSION}.jammy_${ARCH}.deb",
            script)
        self.assertIn("PCM_WKHTML_OK", script)
        self.assertIn("PCM_WKHTML_FAIL", script)
        self.assertIn("wkhtmltopdf --version", script)
        for mala in ("", "0.12; rm -rf /", "v1.0$", "../../etc"):
            with self.assertRaises(UserError, msg=mala):
                Env._build_wkhtmltopdf_script(mala)

    def test_script_pasa_bash_n(self):
        script = self.env["primate.cloud.environment"]\
            ._build_wkhtmltopdf_script("0.12.6.1-3")
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
            handle.write(script)
            handle.flush()
            proc = subprocess.run(["bash", "-n", handle.name],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_provision_ok_y_fallo(self):
        ssm = self._fake_ssm({
            "stdout": "PCM_WKHTML_OK:wkhtmltopdf 0.12.6.1 (with patched qt)",
            "status": "Success"})
        detalle = self.server._provision_wkhtmltopdf(
            ssm, self.machine, "us-east-1", "0.12.6.1-3")
        self.assertIn("patched qt", detalle)
        ssm_mal = self._fake_ssm({
            "stdout": "PCM_WKHTML_FAIL:wkhtmltopdf 0.12.6 (sin patched qt)",
            "status": "Success"})
        with self.assertRaises(UserError):
            self.server._provision_wkhtmltopdf(
                ssm_mal, self.machine, "us-east-1", "0.12.6.1-3")

    def test_accion_sobre_servidor_montado_encola(self):
        """La acción idempotente queda disponible para servidores YA montados."""
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.server), "with_delay",
                               return_value=fake_delay):
            self.server.action_install_wkhtmltopdf()
        fake_delay.job_install_wkhtmltopdf.assert_called_once_with("0.12.6.1-3")
        with self.assertRaises(UserError):
            self.server.action_install_wkhtmltopdf(version="malo;a")

    def test_job_usa_ssm_y_registra(self):
        fake = self._fake_ssm({
            "stdout": "PCM_WKHTML_OK:wkhtmltopdf 0.12.6.1 (with patched qt)",
            "status": "Success"})
        with mock.patch.object(
                type(self.account), "_get_aws_service", return_value=object()), \
             mock.patch(
                "odoo.addons.primate_cloud_manager.models."
                "primate_cloud_environment.aws_ssm.AwsSsmService",
                return_value=fake):
            ok = self.server.job_install_wkhtmltopdf("0.12.6.1-3")
        self.assertTrue(ok)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "ssm_command"),
             ("name", "like", "wkhtmltopdf")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")


# ----------------------------------------------------------------------------
# addons_path por repo con orden explícito (gap d)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestAddonsPathPorRepo(_BaseMontaje):

    def _repo(self, name, path, seq):
        return self.env["primate.cloud.repository"].create({
            "name": name, "environment_id": self.server.id,
            "local_path": path, "configured_branch": "19.0",
            "sequence": seq,
        })

    def test_orden_explicito_por_sequence(self):
        self._repo("alta_na_pipa", "/opt/x/alta_na_pipa", 40)
        self._repo("webOCA", "/opt/x/webOCA", 10)
        self._repo("primate_portal", "/opt/x/primate_portal", 30)
        self._repo("surveyOCA", "/opt/x/surveyOCA", 20)
        paths = self.machine._repo_addons_paths(self.inst)
        self.assertEqual(paths, ["/opt/x/webOCA", "/opt/x/surveyOCA",
                                 "/opt/x/primate_portal",
                                 "/opt/x/alta_na_pipa"])

    def test_ensure_arma_el_path_ordenado(self):
        """runtime preservado + repos en orden + dir suelto al final."""
        self._repo("webOCA", "/opt/x/webOCA", 10)
        self._repo("alta", "/opt/x/alta", 20)
        runtime = "/opt/pcm/runtime/odoo-19/src/addons"
        addons_dir = self.inst.addons_dir or "/opt/odoo/custom-addons"
        cfg = {"readonly": {"addons_path": "%s,%s" % (runtime, addons_dir)},
               "config_hash": "h"}
        saved = {}

        def fake_save(edits, expected_hash, internal_keys=(), instance_id=None):
            saved.update(edits)
            return "saved"

        fake = self._fake_ssm()
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg), \
             mock.patch.object(type(self.machine), "job_save_config",
                               side_effect=fake_save), \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            outcome = self.machine._ensure_custom_addons_path(
                odoo_instance=self.inst)
        self.assertEqual(outcome, "restarted")
        self.assertEqual(
            saved["addons_path"],
            "%s,/opt/x/webOCA,/opt/x/alta,%s" % (runtime, addons_dir))

    def test_ensure_ready_si_ya_esta_todo(self):
        self._repo("webOCA", "/opt/x/webOCA", 10)
        runtime = "/opt/pcm/runtime/odoo-19/src/addons"
        addons_dir = self.inst.addons_dir or "/opt/odoo/custom-addons"
        cfg = {"readonly": {"addons_path": "%s,/opt/x/webOCA,%s"
                            % (runtime, addons_dir)}, "config_hash": "h"}
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg), \
             mock.patch.object(type(self.machine), "job_save_config") as save, \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=self._fake_ssm()):
            outcome = self.machine._ensure_custom_addons_path(
                odoo_instance=self.inst)
        self.assertEqual(outcome, "ready")
        save.assert_not_called()

    def test_migracion_limpia_symlinks_del_esquema_viejo(self):
        """Los symlinks del dir suelto que apuntan ADENTRO de un clon (puente
        de la mudanza PCM) se borran antes del restart: con la entrada propia
        serían un módulo duplicado."""
        self._repo("pcm", "/opt/pcm/instances/pcm/addons/Primate-Cloude-Manager",
                   10)
        runtime = "/opt/pcm/runtime/odoo-19/src/addons"
        addons_dir = self.inst.addons_dir or "/opt/odoo/custom-addons"
        cfg = {"readonly": {"addons_path": "%s,%s" % (runtime, addons_dir)},
               "config_hash": "h"}
        fake = self._fake_ssm()
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg), \
             mock.patch.object(type(self.machine), "job_save_config",
                               return_value="saved"), \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.machine._ensure_custom_addons_path(odoo_instance=self.inst)
        scripts = [c.args[1] for c in fake.run_script.call_args_list]
        limpieza = [s for s in scripts if "PCM_SYMLINKS_OK" in s]
        self.assertTrue(limpieza, "no corrió la limpieza de symlinks")
        self.assertIn(
            "/opt/pcm/instances/pcm/addons/Primate-Cloude-Manager/*",
            limpieza[0])
        self.assertIn('[ -L "${enlace}" ]', limpieza[0])   # SOLO symlinks

    def test_accion_aplicar_addons_path_en_entorno_montado(self):
        """La migración (pcm-self) se dispara con una acción propia."""
        self._repo("pcm", "/opt/x/pcm", 10)
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.server), "with_delay",
                               return_value=fake_delay):
            self.server.action_apply_addons_path()
        fake_delay.job_apply_addons_path.assert_called_once()
        with mock.patch.object(type(self.machine), "_ensure_custom_addons_path",
                               return_value="restarted") as ensure:
            ok = self.server.job_apply_addons_path()
        self.assertTrue(ok)
        ensure.assert_called_once()
        log = self.env["primate.cloud.operation.log"].search(
            [("name", "like", "Aplicar addons_path")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")

    def test_guardado_fallido_no_limpia_symlinks(self):
        """Lección pcm-self 2026-11-17: si el guardado del conf NO aplica
        (stale/error), los symlinks NO se tocan — son el puente de vida del
        módulo. La limpieza corre SOLO tras un guardado exitoso."""
        self._repo("pcm", "/opt/x/pcm", 10)
        runtime = "/opt/pcm/runtime/odoo-19/src/addons"
        addons_dir = self.inst.addons_dir or "/opt/odoo/custom-addons"
        cfg = {"readonly": {"addons_path": "%s,%s" % (runtime, addons_dir)},
               "config_hash": "h"}
        for outcome in ("stale", "rolled_back", "error"):
            with mock.patch.object(type(self.machine), "fetch_config",
                                   return_value=cfg), \
                 mock.patch.object(type(self.machine), "job_save_config",
                                   return_value=outcome), \
                 mock.patch.object(type(self.machine),
                                   "_limpiar_symlinks_de_repos") as limpiar, \
                 mock.patch.object(type(self.machine), "_get_ssm_service",
                                   return_value=self._fake_ssm()):
                self.machine._ensure_custom_addons_path(
                    odoo_instance=self.inst)
            limpiar.assert_not_called()
        # Y con guardado exitoso, SÍ limpia (después del save).
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg), \
             mock.patch.object(type(self.machine), "job_save_config",
                               return_value="saved_deferred"), \
             mock.patch.object(type(self.machine),
                               "_limpiar_symlinks_de_repos") as limpiar, \
             mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=self._fake_ssm()):
            outcome = self.machine._ensure_custom_addons_path(
                odoo_instance=self.inst)
        self.assertEqual(outcome, "restarted")
        limpiar.assert_called_once()

    def test_limpieza_saltea_paths_con_charset_raro(self):
        fake = self._fake_ssm()
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.machine._limpiar_symlinks_de_repos(
                fake, "/opt/odoo/custom-addons",
                ["/opt/x/bueno", "/opt/mal; rm -rf /"])
        script = fake.run_script.call_args.args[1]
        self.assertIn("/opt/x/bueno/*", script)
        self.assertNotIn("rm -rf /", script.replace('rm -f "${enlace}"', ""))


# ----------------------------------------------------------------------------
# Config de la instancia self: restart diferido (http_interface a pcm-self)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestSaveConfigSelfDiferido(_BaseMontaje):

    def test_self_aplica_y_reinicia_diferido_sin_health(self):
        self.machine.provisioned_by_pcm = True
        self.inst.is_self = True
        apply_out = {"stdout": (
            "PCM_OLD:workers=0\nPCM_BAK:/etc/odoo/x.conf.pcm-bak-1\n"
            "PCM_HTTP_PORT:8069\nPCM_RESULT:applied\n"), "status": "Success"}
        fake = mock.Mock()

        def run_script(instance_id, cmd, **kw):
            if "config apply" in kw.get("comment", ""):
                return apply_out
            return {"stdout": "", "status": "Success"}

        fake.run_script.side_effect = run_script
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake), \
             mock.patch.object(type(self.machine), "_config_health_check") as hc:
            outcome = self.machine.job_save_config(
                {"workers": "2"}, "h", instance_id=self.inst.id)
        self.assertEqual(outcome, "saved_deferred")
        hc.assert_not_called()   # nadie queda vivo para el health del self
        restart = [c for c in fake.run_script.call_args_list
                   if "restart" in c.kwargs.get("comment", "")]
        self.assertTrue(restart)
        self.assertIn("systemd-run --collect --on-active=10",
                      restart[0].args[1])
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "config_edit")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")
        self.assertIn("DIFERIDO", log.error_message)

    def test_harden_http_interface_pasa_por_internal_keys(self):
        """La clave es read-only para el editor: la acción la aplica con el
        guardado quirúrgico (y no toca nada si ya está en 127.0.0.1)."""
        self.machine.provisioned_by_pcm = True
        cfg = {"readonly": {"http_interface": ""}, "config_hash": "h"}
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg), \
             mock.patch.object(type(self.machine), "job_save_config",
                               return_value="saved") as save:
            outcome = self.server.job_harden_http_interface()
        self.assertEqual(outcome, "saved")
        args, kwargs = save.call_args
        self.assertEqual(args[0], {"http_interface": "127.0.0.1"})
        self.assertEqual(kwargs["internal_keys"], {"http_interface"})
        # Idempotente: ya blindada → ni edita ni reinicia.
        cfg_ok = {"readonly": {"http_interface": "127.0.0.1"},
                  "config_hash": "h"}
        with mock.patch.object(type(self.machine), "fetch_config",
                               return_value=cfg_ok), \
             mock.patch.object(type(self.machine), "job_save_config") as save2:
            outcome = self.server.job_harden_http_interface()
        self.assertEqual(outcome, "ready")
        save2.assert_not_called()

    def test_no_self_sigue_con_health_y_restart_normal(self):
        self.machine.provisioned_by_pcm = True
        self.inst.is_self = False
        apply_out = {"stdout": (
            "PCM_OLD:workers=0\nPCM_BAK:/etc/odoo/x.conf.pcm-bak-1\n"
            "PCM_HTTP_PORT:8069\nPCM_RESULT:applied\n"), "status": "Success"}
        fake = mock.Mock()
        fake.run_script.side_effect = lambda i, c, **kw: (
            apply_out if "config apply" in kw.get("comment", "")
            else {"stdout": "", "status": "Success"})
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake), \
             mock.patch.object(type(self.machine), "_config_health_check",
                               return_value="http") as hc:
            outcome = self.machine.job_save_config(
                {"workers": "2"}, "h", instance_id=self.inst.id)
        self.assertEqual(outcome, "saved")
        hc.assert_called_once()
        restart = [c for c in fake.run_script.call_args_list
                   if "restart" in c.kwargs.get("comment", "")]
        self.assertNotIn("systemd-run", restart[0].args[1])


# ----------------------------------------------------------------------------
# Cambiar de rama un repo ya clonado (gap d, checkout sin reinstalar)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestCheckoutBranchActualizaRama(_BaseMontaje):

    def test_checkout_branch_exitoso_actualiza_configured_branch(self):
        repo = self.env["primate.cloud.repository"].create({
            "name": "webOCA", "environment_id": self.server.id,
            "local_path": "/opt/x/webOCA", "configured_branch": "19.0",
        })
        deploy = self.env["primate.cloud.deployment"].create({
            "environment_id": self.server.id, "repository_id": repo.id,
            "deployment_type": "checkout_branch", "target_branch": "19.0_fix",
        })
        out = {"stdout": "PCM_ORIGIN:aaa\nPCM_TARGET:bbb\n",
               "status": "Success"}
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=self._fake_ssm(out)):
            ok = deploy.job_deploy()
        self.assertTrue(ok)
        self.assertEqual(repo.configured_branch, "19.0_fix")
        self.assertEqual(repo.current_commit, "bbb")
        # El script hace fetch + checkout + pull (sin reinstalar nada).
        script = deploy._build_deploy_script()
        self.assertIn("fetch --all --prune", script)
        self.assertIn("checkout 19.0_fix", script)

    def test_pull_no_toca_la_rama(self):
        repo = self.env["primate.cloud.repository"].create({
            "name": "alta", "environment_id": self.server.id,
            "local_path": "/opt/x/alta", "configured_branch": "19.0",
        })
        deploy = self.env["primate.cloud.deployment"].create({
            "environment_id": self.server.id, "repository_id": repo.id,
            "deployment_type": "pull",
        })
        out = {"stdout": "PCM_ORIGIN:aaa\nPCM_TARGET:bbb\n",
               "status": "Success"}
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=self._fake_ssm(out)):
            deploy.job_deploy()
        self.assertEqual(repo.configured_branch, "19.0")


# ----------------------------------------------------------------------------
# Deploys git: -u automático cuando cambian los módulos (incidente pcm-self)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestPullConUAutomatico(_BaseMontaje):

    def setUp(self):
        super().setUp()
        self.repo = self.env["primate.cloud.repository"].create({
            "name": "pcm", "environment_id": self.server.id,
            "local_path": "/opt/x/pcm", "configured_branch": "19.0",
        })

    def _deploy(self, **vals):
        base = {"environment_id": self.server.id, "deployment_type": "pull",
                "repository_id": self.repo.id}
        base.update(vals)
        return self.env["primate.cloud.deployment"].create(base)

    def _con_db(self):
        db = self.env["primate.cloud.database"].create({
            "name": "altanapipa", "account_id": self.account.id,
            "environment_id": self.server.id})
        self.inst.database_id = db

    def test_pull_detecta_y_actualiza_modulos_cambiados(self):
        self._con_db()
        script = self._deploy()._build_deploy_script()
        self.assertIn("ir_module_module", script)        # psql de instalados
        self.assertIn("__manifest__.py", script)         # detección por manifest
        self.assertIn('echo "PCM_UPDATE:$CAMBIADOS"', script)
        self.assertIn("--no-http", script)               # el -u no choca el puerto
        self.assertIn("-d altanapipa", script)
        # El -u corre con el servicio DETENIDO (el DDL no compite con el
        # server vivo) y el servicio SIEMPRE vuelve a arrancar.
        self.assertIn("systemctl stop ", script)
        self.assertIn("systemctl start ", script)
        self.assertIn('if [ -n "$CAMBIADOS" ]; then', script)
        self.assertIn("bash /var/tmp/pcm_update_deploy.sh", script)
        # El stop→start ya reinicia: el pull no agrega otro restart.
        self.assertNotIn("systemctl restart", script)

    def test_pull_sin_base_degrada_a_vacio(self):
        script = self._deploy()._build_deploy_script()
        self.assertIn('CAMBIADOS=""', script)
        self.assertNotIn("ir_module_module", script)
        # Sin base no hay -u posible: tampoco se genera el ciclo stop→start.
        self.assertNotIn("pcm_update_deploy.sh", script)

    def test_checkout_sigue_reiniciando_siempre(self):
        self._con_db()
        script = self._deploy(deployment_type="checkout_branch",
                              target_branch="19.0")._build_deploy_script()
        self.assertIn('echo "PCM_UPDATE:$CAMBIADOS"', script)
        # Con cambios de módulos el stop→start del -u ya reinicia; sin
        # cambios, el checkout reinicia como siempre (guardado por -z).
        self.assertIn('if [ -z "$CAMBIADOS" ]; then sudo systemctl restart',
                      script)

    def test_self_difiere_el_ciclo_entero_del_u(self):
        """Para la self, stop→-u→start va DIFERIDO en una unit transitoria:
        el job reporta (con marcador) antes de que PCM se apague."""
        self._con_db()
        self.inst.is_self = True
        script = self._deploy()._build_deploy_script()
        self.assertIn('echo "PCM_UPDATE_DIFERIDO"', script)
        self.assertIn("sudo systemd-run --collect --on-active=5 "
                      "bash /var/tmp/pcm_update_deploy.sh", script)

    def test_module_update_lleva_no_http(self):
        """El -u manual del deploy tampoco choca el puerto del Odoo vivo."""
        self._con_db()
        script = self._deploy(deployment_type="module_update",
                              module_names="primate_cloud_manager",
                              repository_id=False)._build_deploy_script()
        self.assertIn("-u primate_cloud_manager --no-http --stop-after-init",
                      script)
        # También el -u manual corre con el servicio detenido.
        self.assertIn("systemctl stop ", script)
        self.assertIn("systemctl start ", script)

    def test_script_pull_pasa_bash_n(self):
        self._con_db()
        script = self._deploy()._build_deploy_script()
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
            handle.write(script)
            handle.flush()
            proc = subprocess.run(["bash", "-n", handle.name],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ----------------------------------------------------------------------------
# Verificación post-instalación (gap e)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestVerificacionInstalacion(_BaseMontaje):

    def setUp(self):
        super().setUp()
        self.repo = self.env["primate.cloud.repository"].create({
            "name": "alta_na_pipa", "environment_id": self.server.id,
            "local_path": "/opt/x/alta_na_pipa", "configured_branch": "19.0",
        })
        self.db = self.env["primate.cloud.database"].create({
            "name": "altanapipa", "environment_id": self.server.id,
            "account_id": self.account.id,
        })
        self.inst.database_id = self.db

    @staticmethod
    def _stdout(instalados, esperados):
        return ("PCM_MODS_BEGIN\n%s\nPCM_MODS_END\n"
                "PCM_REF_BEGIN\n%s\nPCM_REF_END\n"
                % ("\n".join(instalados), "\n".join(esperados)))

    def _run(self, stdout):
        self.server.write({"modules_ref_repo_id": self.repo.id,
                           "modules_ref_path": "docs/MODULES_PROD.txt"})
        with mock.patch.object(
                type(self.machine), "_get_ssm_service",
                return_value=self._fake_ssm(
                    {"stdout": stdout, "status": "Success"})):
            return self.server.job_verify_installation()

    def _last_log(self):
        return self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "ssm_command"),
             ("name", "like", "Verificar instalaci")],
            order="id desc", limit=1)

    def test_verde_cuando_identicos(self):
        mods = ["alta_na_pipa_website", "base", "web", "website"]
        self.assertTrue(self._run(self._stdout(mods, mods)))
        self.assertEqual(self._last_log().result, "success")

    def test_rojo_con_faltantes_y_sobrantes(self):
        ok = self._run(self._stdout(["base", "web", "intruso"],
                                    ["base", "web", "website"]))
        self.assertFalse(ok)
        log = self._last_log()
        self.assertEqual(log.result, "failed")
        self.assertIn("FALTAN (1): website", log.error_message)
        self.assertIn("SOBRAN (1): intruso", log.error_message)

    def test_rojo_si_no_se_pudo_leer_nada(self):
        """Sin módulos en el stdout (psql mudo) JAMÁS es verde."""
        self.assertFalse(self._run(self._stdout([], [])))

    def test_script_consulta_db_y_archivo_del_repo(self):
        self.server.write({"modules_ref_repo_id": self.repo.id,
                           "modules_ref_path": "docs/MODULES_PROD.txt"})
        fake = self._fake_ssm({"stdout": self._stdout(["base"], ["base"]),
                               "status": "Success"})
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.server.job_verify_installation()
        script = fake.run_script.call_args.args[1]
        self.assertIn("ir_module_module", script)
        self.assertIn("altanapipa", script)
        self.assertIn("/opt/x/alta_na_pipa/docs/MODULES_PROD.txt", script)

    def test_accion_valida_referencia_y_path(self):
        with self.assertRaises(UserError):   # sin referencia configurada
            self.server.action_verify_installation()
        with self.assertRaises(UserError):   # path con escape
            self.server.action_verify_installation(
                {"repo_id": self.repo.id, "path": "../../etc/passwd"})
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.server), "with_delay",
                               return_value=fake_delay):
            self.server.action_verify_installation(
                {"repo_id": self.repo.id, "path": "docs/MODULES_PROD.txt"})
        fake_delay.job_verify_installation.assert_called_once()
        self.assertEqual(self.server.modules_ref_path, "docs/MODULES_PROD.txt")


# ----------------------------------------------------------------------------
# Wizard: validación temprana + params
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestWizardMontaje(_BaseMontaje):

    def _wizard(self, **vals):
        base = {"environment_id": self.server.id,
                "account_id": self.account.id, "region": "us-east-1",
                "domain": "altanapipa.primatecloudmanager.com",
                "db_mode": "local_pg", "db_name": "altanapipa",
                "create_dns": False}
        base.update(vals)
        return self.env["primate.cloud.provision.wizard"].create(base)

    def test_validate_corta_antes_de_crear_la_ec2(self):
        casos = [
            {"pip_packages": "openpyxl; rm -rf /"},
            {"init_modules": "a;b"},
            {"load_language": "pt_BR"},                 # idioma sin módulos
            {"init_modules": "web", "load_language": "no vale"},
            {"wkhtml_version": "0.12; rm"},
        ]
        for vals in casos:
            with self.assertRaises(UserError, msg=vals):
                self._wizard(**vals)._validate()

    def test_prepare_params_lleva_las_opciones_nuevas(self):
        wiz = self._wizard(init_modules="web,website", load_language="pt_BR",
                           pip_packages="openpyxl",
                           wkhtml_version="0.12.6.1-3",
                           limit_time_cpu=600, limit_time_real=1200)
        wiz._validate()
        params = wiz._prepare_params()
        self.assertEqual(params["init_modules"], "web,website")
        self.assertEqual(params["load_language"], "pt_BR")
        self.assertEqual(params["pip_packages"], "openpyxl")
        self.assertEqual(params["wkhtml_version"], "0.12.6.1-3")
        self.assertEqual(params["limit_time_cpu"], 600)
        self.assertEqual(params["limit_time_real"], 1200)

    def test_defaults_600_1200(self):
        wiz = self._wizard()
        self.assertEqual(wiz.limit_time_cpu, 600)
        self.assertEqual(wiz.limit_time_real, 1200)
