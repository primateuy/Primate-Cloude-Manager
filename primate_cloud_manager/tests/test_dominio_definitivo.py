# -*- coding: utf-8 -*-
"""Dominio definitivo e IP fija (decisión 2026-10-27).

Qué protege esta suite:
- IP fija: el montaje reserva una Elastic IP etiquetada y la asocia; si la
  asociación falla la libera; al terminar la máquina se libera la EIP de PCM
  (nunca una ajena);
- camino PCM («Crear DNS ahora»): registro A en Route 53 → INSYNC →
  propagación contra los NS de la zona → vhost en paralelo → certificado →
  recién ahí se retira lo viejo (cert sslip.io borrado, web.base.url);
- camino manual («Asociar dominio existente»): PCM no toca Route 53;
  verifica que el dominio apunte a la IP y hace lo mismo desde nginx;
- el dominio todavía no apunta a la IP: se dice a qué apunta y NO se toca
  nada (ni en el wizard ni en el job);
- si el certificado falla, se desarma lo nuevo y la instancia sigue en su
  dominio con su HTTPS.

Todo mockeado (SSM/Route 53/EC2/requests): nada toca infra real.
"""
from contextlib import ExitStack
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.primate_cloud_manager.models import (
    primate_cloud_environment as env_mod,
)
from odoo.addons.primate_cloud_manager.services import (
    aws_ec2, aws_route53, aws_ssm,
)
from odoo.addons.primate_cloud_manager.wizard import (
    primate_cloud_domain_wizard as wiz_mod,
)

IP = "1.2.3.4"
SSLIP = "ensayo.1-2-3-4.sslip.io"
NUEVO = "erp.cliente.com"


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
               % (IP, IP, IP), certbot=True):
    return {
        "pcm ssl propagation": {"status": "Success", "stdout": dig},
        "pcm domain switch vhost": {"status": "Success",
                                    "stdout": "PCM_VHOST_OK"},
        "pcm ssl certbot": (
            {"status": "Success", "stdout": "PCM_SSL_OK"} if certbot else
            {"status": "Failed", "stdout": "Challenge failed for domain"}),
        "pcm domain switch swap": {"status": "Success",
                                   "stdout": "PCM_SWAP_OK"},
        "pcm domain switch rollback": {"status": "Success",
                                       "stdout": "PCM_ROLLBACK_OK"},
        "pcm ssl expiry": {"status": "Success",
                           "stdout": "notAfter=Jun  1 12:00:00 2027 GMT"},
        "pcm ssl web.base.url": {"status": "Success",
                                 "stdout": "PCM_BASEURL_OK"},
    }


@tagged("post_install", "-at_install", "primate_cloud")
class TestDominioDefinitivo(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C-dom", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        partner = self.env["res.partner"].create({"name": "Cliente Dom"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Proyecto Dom", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor Dom", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-dom", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-dom1",
            "instance_state": "running", "region": "us-east-1",
            "public_ip": IP, "management_state": "provisioned",
        })
        self.server.ec2_instance_id = self.machine
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "ensayo", "http_port": 8069,
                         "gevent_port": 8072, "main_url": SSLIP})
        self.inst.write({"state": "active", "ssl_state": "active"})

    # ------------------------------------------------------------ helpers
    def _con_servicios(self, fake, route53=None, verify=(True, "")):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            type(self.account), "_get_aws_service", return_value=mock.Mock()))
        stack.enter_context(mock.patch.object(
            aws_ssm, "AwsSsmService", return_value=fake))
        stack.enter_context(mock.patch.object(
            type(self.server), "_ssl_verify_https", return_value=verify))
        stack.enter_context(mock.patch.object(
            env_mod.time, "sleep", return_value=None))
        stack.enter_context(mock.patch.object(
            aws_route53, "AwsRoute53Service",
            return_value=route53 or mock.Mock()))
        return stack

    def _ec2_falso(self, **kw):
        fake = mock.Mock()
        fake.get_instance_address.return_value = kw.get("address")
        fake.allocate_address.return_value = {
            "public_ip": "9.9.9.9", "allocation_id": "eipalloc-1"}
        return fake

    # ============================================================ IP fija
    def test_ip_fija_reserva_etiquetada_y_asocia(self):
        fake = self._ec2_falso()
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake):
            ip = self.machine._pcm_ensure_elastic_ip()
        self.assertEqual(ip, "9.9.9.9")
        tags = {t["Key"]: t["Value"]
                for t in fake.allocate_address.call_args[0][0]}
        self.assertEqual(tags.get("primate:managed_by"), "pcm")
        fake.associate_address.assert_called_once_with(
            "i-dom1", "eipalloc-1", region="us-east-1")
        self.assertEqual(self.machine.public_ip, "9.9.9.9")
        self.assertEqual(self.machine.eip_allocation_id, "eipalloc-1")

    def test_ip_fija_idempotente_con_eip_propia(self):
        fake = self._ec2_falso(address={
            "public_ip": "8.8.4.4", "allocation_id": "eipalloc-ya",
            "tags": {"primate:managed_by": "pcm"}})
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake):
            self.machine._pcm_ensure_elastic_ip()
        fake.allocate_address.assert_not_called()
        self.assertEqual(self.machine.eip_allocation_id, "eipalloc-ya")

    def test_ip_fija_asociacion_falla_libera(self):
        fake = self._ec2_falso()
        fake.associate_address.side_effect = Exception("AccessDenied")
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake), \
                self.assertRaises(Exception):
            self.machine._pcm_ensure_elastic_ip()
        fake.release_address.assert_called_once_with(
            "eipalloc-1", region="us-east-1")

    def test_ip_fija_bloqueada_en_importados(self):
        self.machine.with_context(pcm_import_sync=True).management_state = \
            "adopted_readonly"
        fake = self._ec2_falso()
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake), \
                self.assertRaises(UserError):
            self.machine._pcm_ensure_elastic_ip()
        fake.allocate_address.assert_not_called()

    def test_montaje_asigna_ip_fija_antes_de_la_base(self):
        orden = []
        with mock.patch.object(
                type(self.machine), "_pcm_ensure_elastic_ip",
                side_effect=lambda: orden.append("eip")), \
                mock.patch.object(
                    type(self.server), "_provision_database",
                    side_effect=lambda *a, **k: orden.append("db") or "x"), \
                mock.patch.object(
                    type(self.account), "_get_aws_service",
                    return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_ensure_server_bootstrap",
                                  side_effect=RuntimeError("corte")):
            self.server.job_install_instance({"db_mode": "local_pg"})
        self.assertEqual(orden[:2], ["eip", "db"])

    def test_terminar_libera_la_ip_fija_de_pcm(self):
        self.machine.eip_allocation_id = "eipalloc-1"
        fake = self._ec2_falso(address={
            "public_ip": IP, "allocation_id": "eipalloc-1",
            "tags": {"primate:managed_by": "pcm"}})
        fake.terminate_instance.return_value = "req-1"
        fake.get_instance.return_value = {"instance_state": "shutting-down"}
        # 1ª consulta: todavía asociada; 2ª: ya suelta.
        fake.get_address.side_effect = [
            {"allocation_id": "eipalloc-1", "association_id": "eipassoc-1",
             "tags": {"primate:managed_by": "pcm"}},
            {"allocation_id": "eipalloc-1", "association_id": None,
             "tags": {"primate:managed_by": "pcm"}},
        ]
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake), \
                mock.patch.object(type(self.machine), "_notify_user_done"), \
                mock.patch("time.sleep", return_value=None):
            self.machine.job_lifecycle("ec2_terminate")
        fake.release_address.assert_called_once_with(
            "eipalloc-1", region="us-east-1")
        self.assertFalse(self.machine.eip_allocation_id)

    def test_terminar_no_libera_una_eip_ajena(self):
        fake = self._ec2_falso(address={
            "public_ip": IP, "allocation_id": "eipalloc-ajena", "tags": {}})
        fake.terminate_instance.return_value = "req-1"
        fake.get_instance.return_value = {"instance_state": "shutting-down"}
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake), \
                mock.patch.object(type(self.machine), "_notify_user_done"):
            self.machine.job_lifecycle("ec2_terminate")
        fake.release_address.assert_not_called()

    def test_instancia_recien_creada_no_visible_no_es_error(self):
        """Corrida 2026-10-27: DescribeInstances respondió NotFound segundos
        después de RunInstances y el montaje se cortó con la EC2 creada."""
        from botocore.exceptions import ClientError
        no_existe = ClientError(
            {"Error": {"Code": "InvalidInstanceID.NotFound",
                       "Message": "does not exist"}}, "DescribeInstances")
        base = mock.Mock()
        client = base.get_client.return_value
        client.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-nueva"}]}
        corriendo = {"Reservations": [{"Instances": [{
            "InstanceId": "i-nueva", "State": {"Name": "running"},
            "InstanceType": "t3.micro"}]}]}
        client.describe_instances.side_effect = [no_existe, corriendo]
        data = aws_ec2.AwsEc2Service(base).create_instance(
            image_id="ami-1", instance_type="t3.micro", tags=[],
            region="us-east-1", _sleep=lambda s: None)
        self.assertEqual(data["aws_instance_id"], "i-nueva")
        self.assertEqual(client.run_instances.call_count, 1)

    def test_gemelo_inline_corre_con_bash(self):
        """Corrida 2026-10-27: con el export antes del shebang, SSM lo corría
        con sh (dash) y `set -o pipefail` cortaba el gemelo."""
        fake = mock.Mock()
        fake.run_script.return_value = {"status": "Failed", "stderr": "x"}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            with self.assertRaises(UserError):
                self.machine._twin_fetch()
        script = fake.run_script.call_args[0][1]
        self.assertTrue(script.startswith("#!/bin/bash\n"), script[:40])

    # ==================================================== camino manual
    def test_manual_cambia_sin_cortar_y_borra_el_cert_viejo(self):
        fake = FakeSsm(respuestas())
        with self._con_servicios(fake):
            result = self.inst.job_attach_domain(NUEVO)
        self.assertEqual(result, "active")
        self.assertEqual(self.inst.main_url, NUEVO)
        self.assertEqual(self.inst.ssl_state, "active")
        self.assertFalse(self.inst.domain_switch_state)
        comentarios = fake.comments()
        # Orden NO destructivo: vhost paralelo → certbot → swap → borrar viejo.
        indices = [next(i for i, c in enumerate(comentarios) if f in c)
                   for f in ("pcm ssl propagation", "pcm domain switch vhost",
                             "pcm ssl certbot", "pcm domain switch swap",
                             "pcm ssl delete", "pcm ssl web.base.url")]
        self.assertEqual(indices, sorted(indices))
        self.assertIn(SSLIP, fake.script_de("pcm ssl delete"))
        self.assertIn("https://%s" % NUEVO,
                      fake.script_de("pcm ssl web.base.url"))
        self.assertEqual(self.inst._public_url(), "https://%s" % NUEVO)
        # Camino manual: Route 53 ni se crea.
        self.assertFalse(self.env["primate.cloud.dns.record"].search(
            [("name", "=", NUEVO)]))

    def test_manual_dominio_que_no_apunta_no_toca_nada(self):
        fake = FakeSsm(respuestas(
            dig="A[system]=5.6.7.8,\nA[1.1.1.1]=5.6.7.8,\nA[8.8.8.8]=,\n"
                "AAAA=\n"))
        with self._con_servicios(fake):
            result = self.inst.job_attach_domain(NUEVO)
        self.assertEqual(result, "not_resolving")
        self.assertEqual(fake.comments(), ["pcm ssl propagation: %s" % NUEVO])
        self.assertEqual(self.inst.main_url, SSLIP)
        self.assertEqual(self.inst.ssl_state, "active")
        self.assertEqual(self.inst.domain_switch_state, "failed")
        self.assertIn("5.6.7.8", self.inst.domain_switch_error)
        self.assertIn("no resuelve", self.inst.domain_switch_error)

    def test_certbot_falla_desarma_lo_nuevo_y_sigue_en_sslip(self):
        fake = FakeSsm(respuestas(certbot=False))
        with self._con_servicios(fake):
            result = self.inst.job_attach_domain(NUEVO)
        self.assertEqual(result, "certbot_failed")
        comentarios = fake.comments()
        self.assertTrue(any("pcm domain switch rollback" in c
                            for c in comentarios))
        self.assertFalse(any("pcm domain switch swap" in c
                             for c in comentarios))
        self.assertFalse(any("pcm ssl delete" in c for c in comentarios))
        rollback = fake.script_de("pcm domain switch rollback")
        self.assertIn("certbot delete --cert-name %s" % NUEVO, rollback)
        self.assertNotIn(SSLIP, rollback)
        self.assertEqual(self.inst.main_url, SSLIP)
        self.assertEqual(self.inst.ssl_state, "active")
        self.assertEqual(self.inst._public_url(), "https://%s" % SSLIP)
        self.assertEqual(self.inst.domain_switch_state, "failed")

    def test_wizard_manual_no_encola_si_no_apunta(self):
        wiz = self.env["primate.cloud.domain.wizard"].create({
            "instance_id": self.inst.id, "mode": "manual", "domain": NUEVO})
        with mock.patch.object(wiz_mod.PrimateCloudDomainWizard,
                               "_resolve_ipv4", return_value=["5.6.7.8"]), \
                mock.patch.object(type(self.inst), "with_delay") as delay, \
                self.assertRaises(UserError) as ctx:
            wiz.action_confirm()
        delay.assert_not_called()
        self.assertIn("5.6.7.8", str(ctx.exception))
        self.assertFalse(self.inst.domain_switch_state)

    def test_wizard_manual_encola_si_apunta(self):
        wiz = self.env["primate.cloud.domain.wizard"].create({
            "instance_id": self.inst.id, "mode": "manual",
            "domain": "HTTPS://%s/" % NUEVO.upper()})
        with mock.patch.object(wiz_mod.PrimateCloudDomainWizard,
                               "_resolve_ipv4", return_value=[IP]), \
                mock.patch.object(type(self.inst), "with_delay") as delay:
            wiz.action_confirm()
        delay.return_value.job_attach_domain.assert_called_once_with(NUEVO)
        self.assertEqual(self.inst.domain_switch_state, "running")

    def test_wizard_rechaza_reservados_y_el_mismo_dominio(self):
        for dominio in ("algo.invalid", SSLIP, "no es dominio"):
            wiz = self.env["primate.cloud.domain.wizard"].create({
                "instance_id": self.inst.id, "mode": "manual",
                "domain": dominio})
            with self.assertRaises(UserError):
                wiz.action_confirm()

    # ======================================================= camino PCM
    def test_pcm_crea_registro_espera_insync_y_cambia(self):
        fake = FakeSsm(respuestas(
            dig="A[ns-1.awsdns-01.org]=%s,\nAAAA=\n" % IP))
        route53 = mock.Mock()
        route53.create_record.return_value = "C123"
        route53.get_change_status.return_value = "INSYNC"
        route53.get_zone_nameservers.return_value = ["ns-1.awsdns-01.org"]
        with self._con_servicios(fake, route53=route53):
            result = self.inst.job_create_dns_and_switch(NUEVO, "Z0TEST", 300)
        self.assertEqual(result, "active")
        kwargs = route53.create_record.call_args.kwargs
        self.assertEqual((kwargs["hosted_zone_id"], kwargs["name"],
                          kwargs["record_type"], kwargs["value"]),
                         ("Z0TEST", NUEVO, "A", IP))
        route53.get_change_status.assert_called_with("C123")
        # La propagación se mira contra los NS de la zona, no un resolver.
        self.assertIn("@ns-1.awsdns-01.org",
                      fake.script_de("pcm ssl propagation"))
        record = self.env["primate.cloud.dns.record"].search(
            [("name", "=", NUEVO)])
        self.assertEqual(record.instance_id, self.inst)
        self.assertEqual(self.inst.main_url, NUEVO)

    def test_pcm_zona_sin_delegar_certbot_falla_y_sigue_en_sslip(self):
        """El ensayo de la sandbox: los NS de la zona responden (INSYNC y
        propagación OK) pero Let's Encrypt no resuelve → certbot falla → la
        instancia sigue en sslip.io con HTTPS y el aviso del dominio."""
        fake = FakeSsm(respuestas(
            dig="A[ns-1.awsdns-01.org]=%s,\nAAAA=\n" % IP, certbot=False))
        route53 = mock.Mock()
        route53.create_record.return_value = "C123"
        route53.get_change_status.return_value = "INSYNC"
        route53.get_zone_nameservers.return_value = ["ns-1.awsdns-01.org"]
        with self._con_servicios(fake, route53=route53):
            result = self.inst.job_create_dns_and_switch(NUEVO, "Z0TEST", 300)
        self.assertEqual(result, "certbot_failed")
        self.assertEqual(self.inst.main_url, SSLIP)
        self.assertEqual(self.inst.ssl_state, "active")
        self.assertEqual(self.inst.domain_switch_target, NUEVO)
        self.assertEqual(self.inst.domain_switch_state, "failed")

    def test_pcm_route53_falla_no_toca_el_servidor(self):
        fake = FakeSsm(respuestas())
        route53 = mock.Mock()
        route53.create_record.side_effect = Exception("AccessDenied")
        with self._con_servicios(fake, route53=route53):
            result = self.inst.job_create_dns_and_switch(NUEVO, "Z0TEST", 300)
        self.assertEqual(result, "dns_failed")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.inst.main_url, SSLIP)
        self.assertIn("AccessDenied", self.inst.domain_switch_error)

    def test_importado_no_cambia_dominio(self):
        self.machine.with_context(pcm_import_sync=True).management_state = \
            "adopted_readonly"
        with self.assertRaises(UserError):
            self.inst.action_open_attach_domain_wizard()
        with self.assertRaises(UserError):
            self.inst.job_attach_domain(NUEVO)

    # ============================================ sslip.io automático (28-oct)
    def _montar_con_sslip(self, params, ssl_result="active"):
        """Corre la instalación con todo AWS/SSM mockeado y devuelve qué
        dominio recibió el install y con cuál se pidió el HTTPS."""
        visto = {}

        def eip():
            self.machine.public_ip = "100.57.183.141"
            return "100.57.183.141"

        def install(ssm, machine, instance, p, region, domain, is_default):
            visto["install"] = domain

        def ssl(instance, change_id=None):
            visto["ssl"] = instance.main_url
            return ssl_result

        Env = type(self.server)
        with mock.patch.object(type(self.machine), "_pcm_ensure_elastic_ip",
                               side_effect=eip), \
                mock.patch.object(type(self.account), "_get_aws_service",
                                  return_value=mock.Mock()), \
                mock.patch.object(Env, "_provision_database",
                                  return_value="localhost"), \
                mock.patch.object(Env, "_ensure_server_bootstrap"), \
                mock.patch.object(Env, "_materialize_multiodoo_layout"), \
                mock.patch.object(Env, "_provision_run_instance_install",
                                  side_effect=install), \
                mock.patch.object(Env, "_provision_ssl", side_effect=ssl):
            ok = self.server.job_install_instance(params)
        return ok, visto

    def test_sslip_automatico_arma_el_dominio_y_emite_https_al_terminar(self):
        ok, visto = self._montar_con_sslip({
            "db_mode": "local_pg", "elastic_ip": True, "sslip_domain": True,
            "ssl_auto": True, "domain": False})
        dominio = "ensayo.100-57-183-141.sslip.io"
        self.assertTrue(ok)
        self.assertEqual(visto["install"], dominio)
        self.assertEqual(visto["ssl"], dominio)
        self.assertEqual(self.inst.main_url, dominio)
        self.assertEqual(self.server.state, "active")

    def test_sin_sslip_automatico_usa_el_dominio_dado(self):
        ok, visto = self._montar_con_sslip({
            "db_mode": "local_pg", "elastic_ip": True,
            "domain": "erp.cliente.com", "ssl_auto": True})
        self.assertEqual(visto["install"], "erp.cliente.com")

    def _wizard(self, **vals):
        self.server.state = "draft"
        base = {
            "environment_id": self.server.id, "region": "us-east-1",
            "image_id": "ami-1", "instance_type": "t3.xlarge",
            "db_mode": "local_pg", "db_name": "ensayo",
            "security_group_ids": "sg-1", "subnet_id": "subnet-1",
            "create_dns": False, "elastic_ip": True,
        }
        base.update(vals)
        return self.env["primate.cloud.provision.wizard"].create(base)

    def test_wizard_sslip_exige_ip_fija_y_sin_route53(self):
        with self.assertRaises(UserError):
            self._wizard(sslip_domain=True, elastic_ip=False).action_provision()
        with self.assertRaises(UserError):
            self._wizard(sslip_domain=True, create_dns=True,
                         hosted_zone_id="Z1").action_provision()
        with self.assertRaises(UserError):
            self._wizard(domain=False).action_provision()

    def test_wizard_sslip_encola_sin_dominio(self):
        wiz = self._wizard(sslip_domain=True, ssl_auto=True)
        with mock.patch.object(type(self.server), "_enqueue_provision") as enq:
            wiz.action_provision()
        params = enq.call_args[0][0]
        self.assertTrue(params["sslip_domain"])
        self.assertTrue(params["ssl_auto"] is not False)
        self.assertTrue(self.server._load_provision_config()["sslip_domain"])
