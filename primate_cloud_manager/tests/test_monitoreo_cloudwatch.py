# -*- coding: utf-8 -*-
"""Monitoreo CloudWatch (pedido INCO 2026-11).

Opción por entorno que instala el agente de AWS por SSM con la config que
INCO mantiene en un parámetro SSM. Goldens del script (idempotente, .deb de
Ubuntu, marcadores de veredicto), guardas (importado bloquea, self permite),
parsing del job, health del servicio y wizard con default activado.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.primate_cloud_environment import aws_ssm


@tagged("post_install", "-at_install", "primate_cloud")
class TestMonitoreoCloudWatch(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "PrimateUY"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "P", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "monit-srv", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-monit", "account_id": self.account.id,
            "aws_instance_id": "i-monit", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"

    def _mock_ssm(self, stdout="PCM_CW_OK", stderr=""):
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success", "stdout": stdout, "stderr": stderr}
        return fake

    # ------------------------------------------------------------------
    # El script (goldens)
    # ------------------------------------------------------------------
    def test_script_default_archivo_local_con_la_config_de_inco(self):
        """Modo DEFAULT (respuesta de INCO): config embebida + archivo local,
        sin Parameter Store."""
        self.server.cw_ssm_parameter = False
        script = self.server._build_cw_agent_script(True)
        # Idempotencia: el .deb solo se instala si NO está.
        self.assertIn("dpkg -s amazon-cloudwatch-agent", script)
        # El .deb de Ubuntu (el script de INCO es de Amazon Linux).
        self.assertIn("ubuntu/amd64/latest/amazon-cloudwatch-agent.deb", script)
        # La config aterriza en el servidor y se carga por archivo.
        self.assertIn("/opt/aws/amazon-cloudwatch-agent/etc/pcm-cloudwatch.json",
                      script)
        self.assertIn("SRC=file:/opt/aws/amazon-cloudwatch-agent/etc/"
                      "pcm-cloudwatch.json", script)
        self.assertIn('fetch-config -m ec2 -c "${SRC}" -s', script)
        self.assertNotIn("ssm:", script)
        # El JSON embebido es EXACTAMENTE el de INCO.
        import json as json_mod
        heredoc = script.split("<<'CWJSON'\n", 1)[1].split("\nCWJSON\n", 1)[0]
        self.assertEqual(json_mod.loads(heredoc), {
            "metrics": {
                "append_dimensions": {"InstanceId": "${aws:InstanceId}"},
                "metrics_collected": {
                    "mem": {"measurement": ["mem_used_percent"],
                            "metrics_collection_interval": 60},
                    "disk": {"measurement": ["disk_used_percent"],
                             "metrics_collection_interval": 60,
                             "resources": ["/"]}}}})
        # Veredicto por marcadores y verificación real del servicio.
        self.assertIn("PCM_CW_OK", script)
        self.assertIn("PCM_CW_ERR", script)
        self.assertIn("systemctl is-active amazon-cloudwatch-agent", script)
        # Lo verboso va al log del servidor, no al stdout de SSM (trunca 24KB).
        self.assertIn("/var/log/pcm-cw-agent.log", script)

    def test_script_modo_ssm_opcional(self):
        """Con parámetro seteado, la config se lee de SSM (modo alternativo)."""
        self.server.cw_ssm_parameter = "/alarm/AWS-CWAgentLinConfig"
        script = self.server._build_cw_agent_script(True)
        self.assertIn("SRC=ssm:/alarm/AWS-CWAgentLinConfig", script)
        self.assertIn('fetch-config -m ec2 -c "${SRC}" -s', script)
        # En modo SSM no se escribe la config local.
        self.assertNotIn("CWJSON", script)

    def test_config_editable_por_parametro_de_sistema(self):
        otra = '{"metrics": {"metrics_collected": {"mem": {"measurement": ["mem_used_percent"]}}}}'
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.cw_agent_config", otra)
        self.server.cw_ssm_parameter = False
        self.assertEqual(self.server._cw_agent_config(), otra)
        self.assertIn(otra, self.server._build_cw_agent_script(True))

    def test_config_invalida_corta_antes_de_viajar(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.cw_agent_config", "{esto no es json")
        self.server.cw_ssm_parameter = False
        with self.assertRaises(UserError):
            self.server._build_cw_agent_script(True)

    def test_script_disable_detiene_sin_desinstalar(self):
        script = self.server._build_cw_agent_script(False)
        self.assertIn("-a stop", script)
        self.assertIn("systemctl disable --now amazon-cloudwatch-agent", script)
        self.assertIn("PCM_CW_DISABLED", script)
        # Desactivar NO desinstala.
        self.assertNotIn("remove", script)
        self.assertNotIn("purge", script)

    def test_script_valida_el_parametro_en_el_punto_de_ejecucion(self):
        # El nombre se interpola en el script: charset cerrado o no se arma.
        self.server.cw_ssm_parameter = "/alarm/x; rm -rf /"
        with self.assertRaises(UserError):
            self.server._build_cw_agent_script(True)

    # ------------------------------------------------------------------
    # Guardas de la opción
    # ------------------------------------------------------------------
    def test_importado_bloqueado(self):
        self.server.with_context(pcm_import_sync=True).write(
            {"pcm_origin": "imported_aws"})
        with self.assertRaises(UserError):
            self.server.action_cw_monitoring_enable()

    def test_self_permitido(self):
        # No es destructiva: el servidor que hospeda a ESTE PCM puede
        # monitorearse a sí mismo.
        self.server.primary_instance_id.is_self = True
        self.server.action_cw_monitoring_enable()
        self.assertTrue(self.server.cw_monitoring)
        self.assertEqual(self.server.cw_agent_state, "pending")

    def test_sin_maquina_corriendo_no_hay_accion(self):
        self.machine.instance_state = "stopped"
        with self.assertRaises(UserError):
            self.server.action_cw_monitoring_enable()

    def test_activar_encola_el_job(self):
        antes = self.env["queue.job"].search_count([])
        self.server.action_cw_monitoring_enable()
        jobs = self.env["queue.job"].search([], order="id desc", limit=1)
        self.assertEqual(self.env["queue.job"].search_count([]), antes + 1)
        self.assertIn("job_cw_agent_apply", jobs.method_name)

    # ------------------------------------------------------------------
    # El job: veredicto por marcadores
    # ------------------------------------------------------------------
    def test_job_enable_ok(self):
        self.server.write({"cw_monitoring": True,
                           "cw_ssm_parameter": "/alarm/AWS-CWAgentLinConfig"})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=self._mock_ssm("x\nPCM_CW_OK\n")):
            self.assertTrue(self.server.job_cw_agent_apply(True))
        self.assertEqual(self.server.cw_agent_state, "active")
        self.assertFalse(self.server.cw_agent_error)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "cw_monitoring")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")

    def test_job_enable_error_con_motivo(self):
        # Ej. real: el parámetro no existe en la región → motivo en la ficha.
        self.server.write({"cw_monitoring": True,
                           "cw_ssm_parameter": "/alarm/NO-EXISTE"})
        salida = ("PCM_CW_ERR: fetch-config falló con el parámetro "
                  "/alarm/NO-EXISTE (¿existe en esta región?) — detalle en x")
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=self._mock_ssm(salida)):
            self.assertFalse(self.server.job_cw_agent_apply(True))
        self.assertEqual(self.server.cw_agent_state, "error")
        self.assertIn("/alarm/NO-EXISTE", self.server.cw_agent_error)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "cw_monitoring")], order="id desc", limit=1)
        self.assertEqual(log.result, "failed")

    def test_job_disable(self):
        self.server.write({"cw_monitoring": False, "cw_agent_state": "active"})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=self._mock_ssm("PCM_CW_DISABLED")):
            self.assertTrue(self.server.job_cw_agent_apply(False))
        self.assertEqual(self.server.cw_agent_state, "disabled")

    def test_job_excepcion_ssm_estado_error_sin_reventar(self):
        # "Un error no bloquea nada más": la excepción queda como estado+motivo.
        self.server.write({"cw_monitoring": True,
                           "cw_ssm_parameter": "/alarm/AWS-CWAgentLinConfig"})
        fake = mock.Mock()
        fake.run_script.side_effect = RuntimeError("InvalidInstanceId")
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            self.assertFalse(self.server.job_cw_agent_apply(True))
        self.assertEqual(self.server.cw_agent_state, "error")
        self.assertIn("InvalidInstanceId", self.server.cw_agent_error)

    # ------------------------------------------------------------------
    # Health: el snapshot verifica el SERVICIO
    # ------------------------------------------------------------------
    def test_health_caido_pasa_a_error_y_vuelve(self):
        self.server.write({"cw_monitoring": True, "cw_agent_state": "active"})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=self._mock_ssm("inactive")):
            self.server._cw_agent_verify(self.machine)
        self.assertEqual(self.server.cw_agent_state, "error")
        self.assertIn("amazon-cloudwatch-agent", self.server.cw_agent_error)
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  return_value=self._mock_ssm("active")):
            self.server._cw_agent_verify(self.machine)
        self.assertEqual(self.server.cw_agent_state, "active")
        self.assertFalse(self.server.cw_agent_error)

    def test_health_fallo_de_ssm_no_es_veredicto(self):
        self.server.write({"cw_monitoring": True, "cw_agent_state": "active"})
        fake = mock.Mock()
        fake.run_script.side_effect = RuntimeError("timeout")
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            self.server._cw_agent_verify(self.machine)
        self.assertEqual(self.server.cw_agent_state, "active")

    def test_health_apagado_no_consulta(self):
        self.server.write({"cw_monitoring": False, "cw_agent_state": "disabled"})
        with mock.patch.object(aws_ssm, "AwsSsmService") as servicio:
            self.server._cw_agent_verify(self.machine)
        servicio.assert_not_called()

    def test_snapshot_de_metricas_llama_al_health_cuando_corresponde(self):
        self.server.write({"cw_monitoring": True, "cw_agent_state": "active"})
        with mock.patch.object(type(self.machine), "_take_metrics_snapshot"), \
                mock.patch.object(type(self.account), "_get_aws_service",
                                  return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_cw_agent_verify") as ver:
            self.machine.job_snapshot_metrics()
        ver.assert_called_once()

    # ------------------------------------------------------------------
    # Wizard: default ACTIVADO + parámetro de sistema
    # ------------------------------------------------------------------
    def test_wizard_default_activado_y_pasa_params(self):
        wiz = self.env["primate.cloud.provision.wizard"].create({
            "environment_id": self.server.id, "region": "us-east-1",
            "db_mode": "local_pg", "db_name": "x",
        })
        self.assertTrue(wiz.cw_monitoring)   # hereda el default: activado
        # Default del modo: archivo local (sin parámetro SSM).
        self.assertFalse(wiz.cw_ssm_parameter)
        params = wiz._prepare_params()
        self.assertTrue(params["cw_monitoring"])
        self.assertFalse(params["cw_ssm_parameter"])
        # Y se recuerda al reintentar (config persistida).
        self.assertIn("cw_monitoring", wiz._raw_values())

    def test_default_sale_del_parametro_de_sistema(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.cw_agent_ssm_parameter", "/alarm/Otro")
        self.assertEqual(
            self.env["primate.cloud.environment"]._default_cw_ssm_parameter(),
            "/alarm/Otro")
