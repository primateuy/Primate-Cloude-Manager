# -*- coding: utf-8 -*-
"""Dominio adicional (alias) de una instancia — decisión 2026-11-24.

Qué protege esta suite:
- el wizard NO encola si el dominio no apunta a la IP del servidor, si ya es
  el dominio principal o si ya es alias de otra instancia;
- montar un alias es vhost PARALELO + certificado propio: el vhost y el
  certificado del dominio principal no se tocan, y ``web.base.url`` tampoco;
- si certbot o la verificación fallan, lo nuevo se desarma (vhost + cert del
  alias, jamás el principal) y el alias queda en ``failed`` reintentable;
- adopción: si el servidor ya sirve ese server_name (montado a mano, caso
  altanapipa.com), no se escribe otro vhost y el alias queda activo;
- quitar un alias borra SU vhost y SU certificado (nunca el del principal)
  y recién entonces el registro;
- los importados quedan bloqueados.

Todo mockeado (SSM/requests): nada toca infra real.
"""
from contextlib import ExitStack
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.primate_cloud_manager.models import (
    primate_cloud_environment as env_mod,
)
from odoo.addons.primate_cloud_manager.services import aws_ssm
from odoo.addons.primate_cloud_manager.wizard import (
    primate_cloud_domain_wizard as wiz_mod,
)

IP = "1.2.3.4"
PRINCIPAL = "ensayo.1-2-3-4.sslip.io"
ALIAS = "altanapipa.com"


class FakeSsm:
    """SSM falso: registra cada run_script y responde por fragmento del comment."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run_script(self, instance_id, script, **kwargs):
        comment = kwargs.get("comment") or ""
        self.calls.append({"script": script, "comment": comment})
        for fragment, response in self.responses.items():
            if fragment in comment:
                return dict(response)
        return {"status": "Success", "stdout": ""}

    def comments(self):
        return [c["comment"] for c in self.calls]

    def script_de(self, fragment):
        return next(c["script"] for c in self.calls if fragment in c["comment"])


def respuestas(dig="A[system]=%s,\nA[1.1.1.1]=%s,\nA[8.8.8.8]=%s,\nAAAA=\n"
               % (IP, IP, IP), certbot=True, vhost="PCM_VHOST_OK"):
    return {
        "pcm ssl propagation": {"status": "Success", "stdout": dig},
        "pcm alias vhost": {"status": "Success", "stdout": vhost},
        "pcm ssl certbot": (
            {"status": "Success", "stdout": "PCM_SSL_OK"} if certbot else
            {"status": "Failed", "stdout": "Challenge failed for domain"}),
        "pcm domain switch rollback": {"status": "Success",
                                       "stdout": "PCM_ROLLBACK_OK"},
        "pcm ssl expiry": {"status": "Success",
                           "stdout": "notAfter=Jun  1 12:00:00 2027 GMT"},
        "pcm alias remove": {"status": "Success",
                             "stdout": "PCM_ALIAS_RM_OK"},
        "pcm ssl delete": {"status": "Success", "stdout": ""},
    }


@tagged("post_install", "-at_install", "primate_cloud")
class TestAliasDominio(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C-alias", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        partner = self.env["res.partner"].create({"name": "Cliente Alias"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Proyecto Alias", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor Alias", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-alias", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-ali1",
            "instance_state": "running", "region": "us-east-1",
            "public_ip": IP, "management_state": "provisioned",
        })
        self.server.ec2_instance_id = self.machine
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "ensayo", "http_port": 8069,
                         "gevent_port": 8072, "main_url": PRINCIPAL})
        self.inst.write({"state": "active", "ssl_state": "active"})
        self.Alias = self.env["primate.cloud.instance.alias"]

    # ------------------------------------------------------------ helpers
    def _con_servicios(self, fake, verify=(True, "")):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            type(self.account), "_get_aws_service", return_value=mock.Mock()))
        stack.enter_context(mock.patch.object(
            aws_ssm, "AwsSsmService", return_value=fake))
        stack.enter_context(mock.patch.object(
            type(self.server), "_ssl_verify_https", return_value=verify))
        stack.enter_context(mock.patch.object(
            env_mod.time, "sleep", return_value=None))
        return stack

    def _alias(self, domain=ALIAS):
        return self.Alias.create({"name": domain,
                                  "instance_id": self.inst.id})

    def _wizard(self, domain=ALIAS):
        return self.env["primate.cloud.alias.wizard"].create({
            "instance_id": self.inst.id, "domain": domain})

    # ============================================================= wizard
    def test_wizard_no_encola_si_no_apunta(self):
        wiz = self._wizard()
        with mock.patch.object(wiz_mod.PrimateCloudDomainWizard,
                               "_resolve_ipv4", return_value=["5.6.7.8"]), \
                self.assertRaises(UserError) as ctx:
            wiz.action_confirm()
        self.assertIn("5.6.7.8", str(ctx.exception))
        self.assertFalse(self.Alias.search([("name", "=", ALIAS)]))

    def test_wizard_rechaza_el_dominio_principal_y_los_duplicados(self):
        with self.assertRaises(UserError):
            self._wizard(PRINCIPAL).action_confirm()
        self._alias()      # ya es alias (de esta u otra instancia) → rechazo
        wiz = self._wizard()
        with mock.patch.object(wiz_mod.PrimateCloudDomainWizard,
                               "_resolve_ipv4", return_value=[IP]), \
                self.assertRaises(UserError) as ctx:
            wiz.action_confirm()
        self.assertIn(ALIAS, str(ctx.exception))

    def test_wizard_encola_si_apunta(self):
        wiz = self._wizard("HTTPS://%s/" % ALIAS.upper())
        with mock.patch.object(wiz_mod.PrimateCloudDomainWizard,
                               "_resolve_ipv4", return_value=[IP]), \
                mock.patch.object(type(self.Alias), "with_delay") as delay:
            wiz.action_confirm()
        delay.return_value.job_mount.assert_called_once_with()
        alias = self.Alias.search([("name", "=", ALIAS)])
        self.assertEqual(alias.state, "pending")
        self.assertEqual(alias.instance_id, self.inst)

    # ============================================================ montaje
    def test_montar_no_toca_el_principal_ni_web_base_url(self):
        alias = self._alias()
        fake = FakeSsm(respuestas())
        with self._con_servicios(fake):
            result = alias.job_mount()
        self.assertTrue(result)
        self.assertEqual(alias.state, "active")
        self.assertTrue(alias.ssl_cert_expiry)
        # El principal quedó exactamente como estaba.
        self.assertEqual(self.inst.main_url, PRINCIPAL)
        self.assertEqual(self.inst.ssl_state, "active")
        comentarios = fake.comments()
        # Orden: resolución → vhost paralelo → certbot → expiry.
        indices = [next(i for i, c in enumerate(comentarios) if f in c)
                   for f in ("pcm ssl propagation", "pcm alias vhost",
                             "pcm ssl certbot", "pcm ssl expiry")]
        self.assertEqual(indices, sorted(indices))
        # NUNCA: swap de vhost, borrado del cert principal, web.base.url.
        for prohibido in ("pcm domain switch swap", "pcm ssl delete",
                          "pcm ssl web.base.url"):
            self.assertFalse(any(prohibido in c for c in comentarios),
                             prohibido)
        vhost = fake.script_de("pcm alias vhost")
        self.assertIn("server_name %s;" % ALIAS, vhost)
        self.assertIn("127.0.0.1:8069", vhost)
        self.assertIn("127.0.0.1:8072", vhost)
        self.assertIn("pcm-ensayo-alias-altanapipa-com.conf", vhost)
        log = self.env["primate.cloud.operation.log"].search([
            ("action_type", "=", "alias_add"), ("result", "=", "success"),
            ("resource_id", "=", self.inst.id)])
        self.assertTrue(log)

    def test_certbot_falla_desarma_el_alias_y_el_principal_sigue(self):
        alias = self._alias()
        fake = FakeSsm(respuestas(certbot=False))
        with self._con_servicios(fake):
            result = alias.job_mount()
        self.assertFalse(result)
        self.assertEqual(alias.state, "failed")
        self.assertIn("Challenge failed", alias.error_message)
        rollback = fake.script_de("pcm domain switch rollback")
        self.assertIn("pcm-ensayo-alias-altanapipa-com.conf", rollback)
        self.assertIn("certbot delete --cert-name %s" % ALIAS, rollback)
        self.assertNotIn(PRINCIPAL, rollback)
        self.assertEqual(self.inst.main_url, PRINCIPAL)
        self.assertEqual(self.inst.ssl_state, "active")

    def test_no_resuelve_no_toca_nada(self):
        alias = self._alias()
        fake = FakeSsm(respuestas(
            dig="A[system]=5.6.7.8,\nA[1.1.1.1]=5.6.7.8,\nA[8.8.8.8]=,\n"
                "AAAA=\n"))
        with self._con_servicios(fake):
            result = alias.job_mount()
        self.assertFalse(result)
        self.assertEqual(alias.state, "failed")
        self.assertEqual(fake.comments(),
                         ["pcm ssl propagation: %s" % ALIAS])

    def test_adopcion_de_un_vhost_ya_montado_a_mano(self):
        """Caso real altanapipa.com: el vhost ya estaba en el servidor; el
        job no escribe otro, emite/verifica y el alias queda activo."""
        alias = self._alias()
        fake = FakeSsm(respuestas(vhost="PCM_ALIAS_ADOPTADO"))
        with self._con_servicios(fake):
            result = alias.job_mount()
        self.assertTrue(result)
        self.assertEqual(alias.state, "active")
        self.assertIn("adoptado",
                      self.inst.message_ids.sorted("id")[-1].body)

    def test_adopcion_certbot_falla_no_desarma_lo_ajeno(self):
        """Si el vhost no lo escribimos nosotros, el fallo no lo borra."""
        alias = self._alias()
        fake = FakeSsm(respuestas(vhost="PCM_ALIAS_ADOPTADO", certbot=False))
        with self._con_servicios(fake):
            result = alias.job_mount()
        self.assertFalse(result)
        self.assertEqual(alias.state, "failed")
        self.assertFalse(any("pcm domain switch rollback" in c
                             for c in fake.comments()))

    def test_reintentar_un_fallado_reencola(self):
        alias = self._alias()
        alias.write({"state": "failed", "error_message": "x"})
        with mock.patch.object(type(self.Alias), "with_delay") as delay:
            alias.action_retry()
        delay.return_value.job_mount.assert_called_once_with()
        self.assertEqual(alias.state, "pending")
        self.assertFalse(alias.error_message)

    # ============================================================= quitar
    def test_quitar_borra_su_vhost_y_su_cert_nunca_el_principal(self):
        alias = self._alias()
        alias.write({"state": "active"})
        fake = FakeSsm(respuestas())
        with self._con_servicios(fake):
            result = alias.job_remove()
        self.assertTrue(result)
        self.assertFalse(self.Alias.search([("name", "=", ALIAS)]))
        script = fake.script_de("pcm alias remove")
        # El vhost del dominio principal está explícitamente excluido.
        self.assertIn("pcm-ensayo.conf", script)
        self.assertIn('[ "$f" = "$MAIN" ] && continue', script)
        borrado = fake.script_de("pcm ssl delete")
        self.assertIn(ALIAS, borrado)
        self.assertNotIn(PRINCIPAL, borrado)
        self.assertEqual(self.inst.main_url, PRINCIPAL)
        log = self.env["primate.cloud.operation.log"].search([
            ("action_type", "=", "alias_remove"), ("result", "=", "success"),
            ("resource_id", "=", self.inst.id)])
        self.assertTrue(log)

    def test_quitar_sin_maquina_borra_solo_el_registro(self):
        alias = self._alias()
        self.machine.instance_state = "terminated"
        result = alias.job_remove()
        self.assertTrue(result)
        self.assertFalse(self.Alias.search([("name", "=", ALIAS)]))

    # ========================================================== bloqueos
    def test_importado_bloqueado(self):
        self.machine.with_context(pcm_import_sync=True).management_state = \
            "adopted_readonly"
        with self.assertRaises(UserError):
            self._wizard().action_confirm()
        with self.assertRaises(UserError):
            self.inst.action_open_alias_wizard()
