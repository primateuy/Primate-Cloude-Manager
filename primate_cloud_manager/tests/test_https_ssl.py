# -*- coding: utf-8 -*-
"""HTTPS (D-R3.9 endurecida): el certificado como paso VERIFICADO del modelo.

Qué protege esta suite (decisiones del OK 2026-10-22):
- el orden nuevo (DNS ANTES del install) en la cadena principal;
- certbot SOLO corre con la propagación confirmada (rate limits LE): esperar
  no consume intento, y el cron tampoco llama a certbot sin propagación;
- un AAAA ajeno corta con mensaje claro ANTES de certbot;
- ``active`` existe únicamente tras la verificación HTTPS real;
- warning + auto-retry: ninguna falla SSL rompe el montaje;
- links de la UI según ``ssl_state`` (nunca un https inventado);
- cambio de dominio → vhost reescrito + cert viejo borrado + reemisión;
- teardown → certbot delete;
- ``web.base.url`` → https, idempotente, vía SSM;
- ``pcm.ssl_use_le_staging`` agrega --staging (y baja verify).

Todo mockeado (SSM/Route53/requests): nada toca infra real.
"""
from contextlib import ExitStack
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import file_open

from odoo.addons.primate_cloud_manager.models import (
    primate_cloud_environment as env_mod,
)
from odoo.addons.primate_cloud_manager.services import aws_route53, aws_ssm


class FakeSsm:
    """SSM falso: registra cada run_script y responde por fragmento del comment."""

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
        return {"status": "Success", "command_id": "cmd-1", "stdout": ""}

    def comments(self):
        return [c["comment"] for c in self.calls]


def respuestas_ok(ip="1.2.3.4"):
    """Camino feliz completo del ciclo SSL."""
    return {
        "pcm ssl propagation": {
            "status": "Success",
            "stdout": "A[system]=%s,\nAAAA=\n" % ip},
        "pcm ssl certbot": {"status": "Success", "stdout": "PCM_SSL_OK"},
        "pcm ssl expiry": {
            "status": "Success",
            "stdout": "notAfter=Jun  1 12:00:00 2027 GMT"},
        "pcm ssl web.base.url": {"status": "Success",
                                 "stdout": "PCM_BASEURL_OK"},
    }


@tagged("post_install", "-at_install", "primate_cloud")
class TestHttpsSsl(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C-ssl", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        partner = self.env["res.partner"].create({"name": "Cliente SSL"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Proyecto SSL", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor SSL", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-ssl", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-ssl1",
            "instance_state": "running", "region": "us-east-1",
            "public_ip": "1.2.3.4",
        })
        self.server.ec2_instance_id = self.machine
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "cliente-ssl", "http_port": 8069,
                         "gevent_port": 8072, "main_url": "cliente.primate.cloud"})
        self.inst.write({"state": "active"})

    def _ciclo(self, fake, change_id=None, verify=(True, ""), route53=None):
        """Un _provision_ssl con todos los servicios falsos inyectados."""
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                type(self.account), "_get_aws_service",
                return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(
                aws_ssm, "AwsSsmService", return_value=fake))
            stack.enter_context(mock.patch.object(
                type(self.server), "_ssl_verify_https", return_value=verify))
            stack.enter_context(mock.patch.object(
                env_mod.time, "sleep", return_value=None))
            stack.enter_context(mock.patch.object(
                aws_route53, "AwsRoute53Service",
                **({"return_value": route53} if route53 is not None else {})))
            return self.server._provision_ssl(self.inst, change_id=change_id)

    # ------------------------------------------------------------- ciclo feliz
    def test_ciclo_feliz_activa_y_verifica(self):
        fake = FakeSsm(respuestas_ok())
        result = self._ciclo(fake)
        self.assertEqual(result, "active")
        self.assertEqual(self.inst.ssl_state, "active")
        self.assertTrue(self.inst.ssl_cert_expiry)
        self.assertEqual(self.inst.ssl_cert_expiry.year, 2027)
        self.assertEqual(self.inst.ssl_retry_count, 0)
        # Orden: propagación ANTES de certbot; web.base.url al final.
        comments = fake.comments()
        self.assertLess(comments.index("pcm ssl propagation: cliente.primate.cloud"),
                        comments.index("pcm ssl certbot: cliente.primate.cloud"))
        self.assertIn("pcm ssl web.base.url: cliente.primate.cloud", comments)

    def test_insync_se_espera_antes_de_dig(self):
        """Con ChangeId fresco, GetChange se consulta hasta INSYNC (ajuste D)."""
        r53 = mock.Mock()
        r53.get_change_status.side_effect = ["PENDING", "PENDING", "INSYNC"]
        fake = FakeSsm(respuestas_ok())
        result = self._ciclo(fake, change_id="/change/C1", route53=r53)
        self.assertEqual(result, "active")
        self.assertEqual(r53.get_change_status.call_count, 3)

    # ------------------------------------------------------- sin propagación
    def test_sin_propagacion_no_consume_certbot(self):
        """Ajuste C: dig sin la IP → certbot NO corre, no se consume intento."""
        fake = FakeSsm({"pcm ssl propagation": {
            "status": "Success", "stdout": "A[system]=5.6.7.8,\nAAAA=\n"}})
        result = self._ciclo(fake)
        self.assertEqual(result, "not_propagated")
        self.assertEqual(self.inst.ssl_state, "pending")
        self.assertEqual(self.inst.ssl_retry_count, 0)
        self.assertFalse([c for c in fake.comments() if "certbot" in c])

    def test_aaaa_conflictivo_corta_claro_sin_certbot(self):
        fake = FakeSsm({"pcm ssl propagation": {
            "status": "Success",
            "stdout": "A[system]=1.2.3.4,\nAAAA=2600:db8::1,\n"}})
        result = self._ciclo(fake)
        self.assertEqual(result, "aaaa_conflict")
        self.assertEqual(self.inst.ssl_state, "failed")
        self.assertIn("AAAA", self.inst.ssl_last_error)
        self.assertFalse([c for c in fake.comments() if "certbot" in c])

    def test_propagacion_contra_ns_autoritativos(self):
        """Con registro DNS de PCM, el dig va contra los NS de la zona."""
        self.env["primate.cloud.dns.record"].create({
            "name": self.inst.main_url, "account_id": self.account.id,
            "environment_id": self.server.id, "hosted_zone_id": "Z123",
            "record_type": "A", "record_value": "1.2.3.4", "state": "active"})
        r53 = mock.Mock()
        r53.get_zone_nameservers.return_value = [
            "ns-1.awsdns.org", "ns-2.awsdns.co.uk"]
        fake = FakeSsm({"pcm ssl propagation": {
            "status": "Success",
            "stdout": ("A[ns-1.awsdns.org]=1.2.3.4,\n"
                       "A[ns-2.awsdns.co.uk]=1.2.3.4,\nAAAA=\n")},
            **{k: v for k, v in respuestas_ok().items()
               if "propagation" not in k}})
        result = self._ciclo(fake, route53=r53)
        self.assertEqual(result, "active")
        script = [c["script"] for c in fake.calls
                  if "propagation" in c["comment"]][0]
        self.assertIn("@ns-1.awsdns.org", script)
        self.assertIn("@ns-2.awsdns.co.uk", script)

    # ------------------------------------------------------- certbot y tope
    def test_certbot_falla_consume_intento_hasta_el_tope(self):
        malas = dict(respuestas_ok())
        malas["pcm ssl certbot"] = {
            "status": "Failed", "stdout": "", "stderr": "boom"}
        for attempt in range(1, env_mod.SSL_RETRY_MAX + 1):
            result = self._ciclo(FakeSsm(malas))
            self.assertEqual(result, "certbot_failed")
            self.assertEqual(self.inst.ssl_retry_count, attempt)
        self.assertEqual(self.inst.ssl_state, "failed")
        # El botón re-arma el ciclo desde cero.
        with mock.patch.object(type(self.inst), "with_delay"):
            self.inst.action_enable_ssl()
        self.assertEqual(self.inst.ssl_state, "pending")
        self.assertEqual(self.inst.ssl_retry_count, 0)

    def test_verificacion_real_gatea_active(self):
        """certbot OK pero el GET https real falla → NADA de active."""
        fake = FakeSsm(respuestas_ok())
        result = self._ciclo(fake, verify=(False, "443 filtrado"))
        self.assertEqual(result, "verify_failed")
        self.assertNotEqual(self.inst.ssl_state, "active")
        self.assertEqual(self.inst.ssl_retry_count, 1)

    def test_le_staging_agrega_flag_y_baja_verify(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.ssl_use_le_staging", "True")
        fake = FakeSsm(respuestas_ok())
        with mock.patch.object(type(self.server), "_ssl_verify_https",
                               return_value=(True, "")) as verify:
            with mock.patch.object(type(self.account), "_get_aws_service",
                                   return_value=mock.Mock()), \
                    mock.patch.object(aws_ssm, "AwsSsmService",
                                      return_value=fake), \
                    mock.patch.object(env_mod.time, "sleep"):
                result = self.server._provision_ssl(self.inst)
        self.assertEqual(result, "active")
        script = [c["script"] for c in fake.calls if "certbot" in c["comment"]][0]
        self.assertIn("--staging", script)
        self.assertFalse(verify.call_args.kwargs.get("verify", True))

    # ------------------------------------------------------------------ cron
    def test_cron_sin_propagacion_no_llama_certbot(self):
        self.inst.sudo().write({"ssl_state": "pending",
                                "ssl_pending_since": "2026-10-19 10:00:00"})
        fake = FakeSsm({"pcm ssl propagation": {
            "status": "Success", "stdout": "A[system]=\nAAAA=\n"}})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake), \
                mock.patch.object(env_mod.time, "sleep"):
            self.env["primate.cloud.instance"]._cron_ssl()
        self.assertFalse([c for c in fake.comments() if "certbot" in c])
        # Pendiente desde hace más del tope → failed honesto.
        self.assertEqual(self.inst.ssl_state, "failed")
        self.assertIn("propag", (self.inst.ssl_last_error or "").lower())

    # ------------------------------------------------------------- links UI
    def test_links_ui_segun_ssl_state(self):
        self.assertEqual(self.inst._public_url(), "http://cliente.primate.cloud")
        self.assertEqual(self.inst._impersonate_base_url(),
                         "http://cliente.primate.cloud")
        self.inst.sudo().ssl_state = "active"
        self.assertEqual(self.inst._public_url(), "https://cliente.primate.cloud")
        self.inst.sudo().ssl_state = "failed"
        self.assertEqual(self.inst._public_url(), "http://cliente.primate.cloud")
        self.inst.main_url = False
        self.assertEqual(self.inst._public_url(), "http://1.2.3.4")

    def test_serializer_expone_url_y_ssl(self):
        data = self.env["primate.cloud.dashboard"].get_odoo_instance_detail(
            self.inst.id)
        self.assertEqual(data["url"], "http://cliente.primate.cloud")
        self.assertEqual(data["ssl"]["state"], "none")
        self.inst.sudo().ssl_state = "active"
        data = self.env["primate.cloud.dashboard"].get_odoo_instance_detail(
            self.inst.id)
        self.assertEqual(data["url"], "https://cliente.primate.cloud")
        self.assertEqual(data["ssl"]["state_label"], "HTTPS activo")

    # ----------------------------------------------------- cambio de dominio
    def test_cambio_dominio_reemite_y_borra_el_viejo(self):
        self.inst.sudo().write({"ssl_state": "active"})
        with mock.patch.object(type(self.inst), "with_delay") as delayed:
            self.inst.write({"main_url": "nuevo.primate.cloud"})
        self.assertTrue(delayed.called)
        self.assertEqual(self.inst.ssl_state, "pending")
        self.assertFalse(self.inst.ssl_cert_expiry)
        fake = FakeSsm({**respuestas_ok(),
                        "pcm domain change": {"status": "Success",
                                              "stdout": "PCM_VHOST_OK"},
                        "pcm ssl delete": {"status": "Success", "stdout": ""}})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake), \
                mock.patch.object(type(self.server), "_ssl_verify_https",
                                  return_value=(True, "")), \
                mock.patch.object(env_mod.time, "sleep"):
            ok = self.server._apply_instance_domain_change(
                self.inst, "cliente.primate.cloud")
        self.assertTrue(ok)
        vhost_call = [c for c in fake.calls if "domain change" in c["comment"]][0]
        self.assertIn("server_name nuevo.primate.cloud;", vhost_call["script"])
        self.assertIn("X-Forwarded-Host", vhost_call["script"])
        delete_call = [c for c in fake.calls if "ssl delete" in c["comment"]][0]
        self.assertIn("cliente.primate.cloud", delete_call["script"])
        self.assertEqual(self.inst.ssl_state, "active")

    def test_cambio_dominio_no_dispara_en_provisioning(self):
        """Una instancia que recién se instala (sin main_url previa o no
        activa) no encola el job de cambio de dominio."""
        nueva = self.env["primate.cloud.instance"].create({
            "name": "Odoo nuevo", "project_id": self.project.id,
            "environment_id": self.server.id, "slug": "nuevo",
            "odoo_version": "19", "odoo_edition": "community",
            "http_port": 8079, "gevent_port": 8082})
        with mock.patch.object(type(nueva), "with_delay") as delayed:
            nueva.write({"main_url": "algo.primate.cloud", "state": "active"})
        self.assertFalse(delayed.called)

    # ------------------------------------------------------------- teardown
    def test_teardown_borra_el_certificado(self):
        script = self.server._build_instance_teardown_script(self.inst)
        self.assertIn("certbot delete --cert-name cliente.primate.cloud",
                      script)

    def test_teardown_sin_dominio_sin_certbot(self):
        self.inst.main_url = False
        script = self.server._build_instance_teardown_script(self.inst)
        self.assertNotIn("certbot delete", script)

    # ------------------------------------------------------------ web.base.url
    def test_web_base_url_idempotente_por_ssm(self):
        fake = FakeSsm(respuestas_ok())
        self._ciclo(fake)
        script = [c["script"] for c in fake.calls
                  if "web.base.url" in c["comment"]][0]
        self.assertIn("https://cliente.primate.cloud", script)
        self.assertIn("IS DISTINCT FROM", script)   # idempotencia
        self.assertIn("WHERE NOT", script)          # upsert si no existe
        self.assertIn(self.inst.conf_path or "cliente-ssl", script)

    # ------------------------------------------------------- goldens scripts
    def test_scripts_sin_certbot_y_con_forwarded_host(self):
        """certbot salió de los DOS install scripts; el vhost pasa
        X-Forwarded-Proto y X-Forwarded-Host; bootstrap trae dnsutils y
        asegura certbot.timer."""
        with file_open(env_mod.INSTANCE_INSTALL_SCRIPT_PATH) as handle:
            instance_sh = handle.read()
        self.assertNotIn("certbot --nginx", instance_sh)
        self.assertIn("X-Forwarded-Proto", instance_sh)
        self.assertIn("X-Forwarded-Host", instance_sh)
        self.assertIn("proxy_mode = True", instance_sh)
        with file_open(env_mod.INSTALL_SCRIPT_PATH) as handle:
            odoo_sh = handle.read()
        self.assertNotIn("certbot --nginx", odoo_sh)
        self.assertIn("X-Forwarded-Host", odoo_sh)
        with file_open(env_mod.BOOTSTRAP_SCRIPT_PATH) as handle:
            bootstrap_sh = handle.read()
        self.assertIn("dnsutils", bootstrap_sh)
        self.assertIn("certbot.timer", bootstrap_sh)

    # ------------------------------------------- guardia temprana de la zona
    def test_zone_id_inexistente_corta_claro(self):
        """NoSuchHostedZone del wizard: mensaje con zona y cuenta, nada creado
        (lección de la corrida real 2026-10-22: cortaba con la EC2 ya
        facturando)."""
        fake_r53 = mock.Mock()
        fake_r53.zone_exists.return_value = False
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_route53, "AwsRoute53Service",
                                  return_value=fake_r53):
            with self.assertRaises(UserError) as caught:
                self.server._check_hosted_zone(self.account, "ZNOPE123")
        self.assertIn("ZNOPE123", str(caught.exception))
        self.assertIn("no existe en la cuenta", str(caught.exception))
        self.assertIn(self.account.display_name, str(caught.exception))

    def test_wizard_provision_corta_antes_de_gastar(self):
        """action_provision con zona inexistente: UserError y NO se encola."""
        wizard = self.env["primate.cloud.provision.wizard"].create({
            "environment_id": self.server.id, "account_id": self.account.id,
            "region": "us-east-1", "domain": "x.primate.cloud",
            "instance_type": "t3.small", "create_dns": True,
            "hosted_zone_id": "ZNOPE123", "db_mode": "none",
        })
        fake_r53 = mock.Mock()
        fake_r53.zone_exists.return_value = False
        setup_ok = mock.Mock(status="ok")
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_route53, "AwsRoute53Service",
                                  return_value=fake_r53), \
                mock.patch.object(
                    type(self.env["primate.cloud.region.setup"]),
                    "get_or_discover", return_value=setup_ok), \
                mock.patch.object(type(self.server),
                                  "_enqueue_provision") as enqueue:
            with self.assertRaises(UserError) as caught:
                wizard.action_provision()
        self.assertIn("no existe en la cuenta", str(caught.exception))
        self.assertFalse(enqueue.called, "con zona inexistente NO se encola")
        # Con la zona válida, el mismo wizard sigue de largo.
        fake_r53.zone_exists.return_value = True
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_route53, "AwsRoute53Service",
                                  return_value=fake_r53), \
                mock.patch.object(
                    type(self.env["primate.cloud.region.setup"]),
                    "get_or_discover", return_value=setup_ok), \
                mock.patch.object(type(self.server),
                                  "_enqueue_provision") as enqueue:
            wizard.action_provision()
        self.assertTrue(enqueue.called)

    # ------------------------------------------------- orden en la cadena R2
    def test_cadena_principal_dns_antes_del_install(self):
        """job_install_instance: DNS primero, install después, SSL al final
        — y una falla de SSL NO tumba el montaje (decisión 1)."""
        order = []
        params = {"create_dns": True, "hosted_zone_id": "Z1",
                  "domain": "cliente.primate.cloud", "db_mode": "none"}

        def marca(nombre, result=None):
            def _side_effect(*args, **kwargs):
                order.append(nombre)
                if isinstance(result, Exception):
                    raise result
                return result
            return _side_effect

        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_provision_dns",
                                  side_effect=marca("dns", "/change/C9")), \
                mock.patch.object(type(self.server), "_ensure_server_bootstrap",
                                  side_effect=marca("bootstrap")), \
                mock.patch.object(type(self.server),
                                  "_materialize_multiodoo_layout",
                                  side_effect=marca("layout")), \
                mock.patch.object(type(self.server),
                                  "_provision_run_instance_install",
                                  side_effect=marca("install")), \
                mock.patch.object(type(self.server), "_provision_ssl",
                                  side_effect=marca("ssl", "active")) as ssl_mock, \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=FakeSsm()):
            ok = self.server.job_install_instance(params)
        self.assertTrue(ok)
        self.assertEqual(order[0], "dns")
        self.assertLess(order.index("dns"), order.index("install"))
        self.assertEqual(order[-1], "ssl")
        # El ChangeId del DNS viaja al paso SSL (espera de INSYNC).
        self.assertEqual(ssl_mock.call_args.kwargs.get("change_id")
                         or ssl_mock.call_args.args[-1], "/change/C9")
        self.assertEqual(self.server.state, "active")

    def test_cadena_principal_ssl_roto_no_tumba(self):
        params = {"create_dns": True, "hosted_zone_id": "Z1",
                  "domain": "cliente.primate.cloud", "db_mode": "none"}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_provision_dns",
                                  return_value="/change/C9"), \
                mock.patch.object(type(self.server), "_ensure_server_bootstrap"), \
                mock.patch.object(type(self.server),
                                  "_materialize_multiodoo_layout"), \
                mock.patch.object(type(self.server),
                                  "_provision_run_instance_install"), \
                mock.patch.object(type(self.server), "_provision_ssl",
                                  side_effect=RuntimeError("certbot explotó")), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=FakeSsm()):
            ok = self.server.job_install_instance(params)
        self.assertTrue(ok)   # el montaje NO se cae por SSL
        self.assertEqual(self.server.state, "active")
        self.assertEqual(self.server.primary_instance_id.ssl_state, "pending")
