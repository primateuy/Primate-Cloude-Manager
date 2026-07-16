# -*- coding: utf-8 -*-
"""Tests de Fase 3: acciones EC2, SSM, jobs y wizards (todo mockeado)."""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..services import aws_ec2, aws_ssm


def _base_with_client():
    """AwsBaseService falso que devuelve siempre el mismo client mock."""
    base = mock.Mock()
    return base, base.get_client.return_value


@tagged("post_install", "-at_install", "primate_cloud")
class TestEc2ServiceActions(TransactionCase):
    def test_lifecycle_devuelve_request_id(self):
        base, client = _base_with_client()
        client.start_instances.return_value = {"ResponseMetadata": {"RequestId": "req-1"}}
        service = aws_ec2.AwsEc2Service(base)
        self.assertEqual(service.start_instance("i-1", region="us-east-1"), "req-1")
        client.start_instances.assert_called_once_with(InstanceIds=["i-1"])

    def test_terminate_llama_api(self):
        base, client = _base_with_client()
        client.terminate_instances.return_value = {"ResponseMetadata": {"RequestId": "r"}}
        aws_ec2.AwsEc2Service(base).terminate_instance("i-9")
        client.terminate_instances.assert_called_once_with(InstanceIds=["i-9"])


@tagged("post_install", "-at_install", "primate_cloud")
class TestSsmService(TransactionCase):
    def test_send_command_normaliza_str(self):
        base, client = _base_with_client()
        client.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
        cid = aws_ssm.AwsSsmService(base).send_command("i-1", "ls -la")
        self.assertEqual(cid, "cmd-1")
        _, kwargs = client.send_command.call_args
        self.assertEqual(kwargs["Parameters"]["commands"], ["ls -la"])

    def test_run_script_espera_estado_terminal(self):
        base, client = _base_with_client()
        client.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
        # Primero "InProgress", luego "Success".
        client.get_command_invocation.side_effect = [
            {"Status": "InProgress"},
            {"Status": "Success", "StandardOutputContent": "ok", "ResponseCode": 0},
        ]
        out = aws_ssm.AwsSsmService(base).run_script(
            "i-1", "echo ok", poll_interval=0, _sleep=lambda s: None
        )
        self.assertEqual(out["status"], "Success")
        self.assertEqual(out["stdout"], "ok")
        self.assertEqual(out["command_id"], "cmd-1")

    def test_run_script_espera_agente_y_reintenta(self):
        # El agente SSM no está listo: el primer SendCommand falla, el segundo va.
        base, client = _base_with_client()
        client.send_command.side_effect = [
            Exception("An error occurred (InvalidInstanceId): not in a valid state"),
            {"Command": {"CommandId": "cmd-1"}},
        ]
        client.get_command_invocation.return_value = {
            "Status": "Success", "StandardOutputContent": "ok", "ResponseCode": 0,
        }
        out = aws_ssm.AwsSsmService(base).run_script(
            "i-1", "echo ok", poll_interval=0, agent_poll=0, _sleep=lambda s: None
        )
        self.assertEqual(out["status"], "Success")
        self.assertEqual(client.send_command.call_count, 2)

    def test_run_script_agente_nunca_listo_relanza(self):
        base, client = _base_with_client()
        client.send_command.side_effect = Exception(
            "An error occurred (InvalidInstanceId): not in a valid state"
        )
        with self.assertRaises(Exception):
            aws_ssm.AwsSsmService(base).run_script(
                "i-1", "echo", agent_timeout=1, agent_poll=1, _sleep=lambda s: None
            )

    def test_run_script_timeout(self):
        base, client = _base_with_client()
        client.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
        client.get_command_invocation.return_value = {"Status": "InProgress"}
        out = aws_ssm.AwsSsmService(base).run_script(
            "i-1", "sleep 999", timeout=2, poll_interval=1, _sleep=lambda s: None
        )
        self.assertEqual(out["status"], "TimedOut")


@tagged("post_install", "-at_install", "primate_cloud")
class TestEc2Jobs(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create(
            {"name": "C", "iam_access_key_id": "AK", "iam_secret_access_key": "sk"}
        )
        self.instance = self.env["primate.cloud.ec2.instance"].create(
            {
                "name": "srv",
                "account_id": self.account.id,
                "aws_instance_id": "i-1",
                "instance_state": "running",
                "region": "us-east-1",
            }
        )

    def test_action_stop_no_cambia_estado_y_avisa(self):
        # El botón NO cambia el estado: solo encola y muestra "por favor espere".
        with mock.patch.object(type(self.instance), "with_delay") as wd:
            res = self.instance.action_stop()
            wd.assert_called_once()
        self.assertEqual(self.instance.instance_state, "running")  # sin cambios
        self.assertEqual(res["tag"], "display_notification")
        self.assertIn("espere", res["params"]["message"])

    def test_job_lifecycle_stop_exito(self):
        fake = mock.Mock()
        fake.stop_instance.return_value = "req-xyz"
        fake.get_instance.return_value = {"instance_state": "stopping"}
        with mock.patch.object(type(self.instance), "_get_ec2_service", return_value=fake):
            ok = self.instance.job_lifecycle("ec2_stop")
        self.assertTrue(ok)
        # El job refleja el estado real que reporta AWS recién al terminar.
        self.assertEqual(self.instance.instance_state, "stopping")
        fake.stop_instance.assert_called_once_with("i-1", region="us-east-1")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.instance.id), ("action_type", "=", "ec2_stop")]
        )
        self.assertEqual(log.result, "success")
        self.assertEqual(log.aws_request_id, "req-xyz")

    def test_job_lifecycle_falla_loguea_motivo(self):
        fake = mock.Mock()
        fake.start_instance.side_effect = Exception("AccessDenied")
        fake.get_instance.side_effect = Exception("unreachable")
        with mock.patch.object(type(self.instance), "_get_ec2_service", return_value=fake):
            ok = self.instance.job_lifecycle("ec2_start")
        self.assertFalse(ok)
        # Sin estado optimista: el estado no cambió.
        self.assertEqual(self.instance.instance_state, "running")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.instance.id), ("action_type", "=", "ec2_start"),
             ("result", "=", "failed")]
        )
        # El motivo del error queda registrado (y se notifica al usuario).
        self.assertIn("AccessDenied", log.error_message)

    def test_job_sync_from_aws_terminada(self):
        fake = mock.Mock()
        fake.get_instance.return_value = None  # ya no existe en AWS
        with mock.patch.object(type(self.instance), "_get_ec2_service", return_value=fake):
            self.instance.job_sync_from_aws()
        self.assertEqual(self.instance.instance_state, "terminated")

    def test_job_execute_command_exito(self):
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success", "stdout": "hola", "stderr": "",
            "command_id": "cmd-1", "response_code": 0,
        }
        with mock.patch.object(type(self.instance), "_get_ssm_service", return_value=fake):
            ok = self.instance.job_execute_command("echo hola")
        self.assertTrue(ok)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.instance.id), ("action_type", "=", "ssm_command")]
        )
        self.assertEqual(log.result, "success")
        self.assertEqual(log.aws_request_id, "cmd-1")

    def test_action_start_encola(self):
        with mock.patch.object(type(self.instance), "with_delay") as wd:
            self.instance.action_start()
            wd.assert_called_once()


@tagged("post_install", "-at_install", "primate_cloud")
class TestEc2Wizards(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({"name": "C"})
        self.instance = self.env["primate.cloud.ec2.instance"].create(
            {"name": "prod-srv", "account_id": self.account.id,
             "aws_instance_id": "i-1", "instance_state": "running", "region": "us-east-1"}
        )

    def test_command_wizard_encola(self):
        wiz = self.env["primate.cloud.ec2.command.wizard"].create(
            {"instance_id": self.instance.id, "command": "uptime"}
        )
        with mock.patch.object(type(self.instance), "with_delay") as wd:
            wiz.action_run()
            wd.assert_called_once()

    def test_command_wizard_vacio_falla(self):
        wiz = self.env["primate.cloud.ec2.command.wizard"].create(
            {"instance_id": self.instance.id, "command": "   "}
        )
        with self.assertRaises(UserError):
            wiz.action_run()

    def test_terminate_wizard_nombre_incorrecto(self):
        wiz = self.env["primate.cloud.ec2.terminate.wizard"].create(
            {"instance_id": self.instance.id, "confirm_name": "otro", "acknowledge": True}
        )
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_terminate_wizard_sin_acknowledge(self):
        wiz = self.env["primate.cloud.ec2.terminate.wizard"].create(
            {"instance_id": self.instance.id, "confirm_name": "prod-srv", "acknowledge": False}
        )
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_terminate_wizard_ok_encola(self):
        wiz = self.env["primate.cloud.ec2.terminate.wizard"].create(
            {"instance_id": self.instance.id, "confirm_name": "prod-srv", "acknowledge": True}
        )
        with mock.patch.object(type(self.instance), "with_delay") as wd:
            wiz.action_confirm()
            wd.assert_called_once()


@tagged("post_install", "-at_install", "primate_cloud")
class TestTerminatePermisos(TransactionCase):
    def test_operator_no_puede_terminar(self):
        # Terminate es admin-only DECLARATIVO (D-B6.5 corregido: no hay
        # asignación operador→cliente en el modelo, así que no se relaja).
        account = self.env["primate.cloud.account"].create({"name": "C"})
        instance = self.env["primate.cloud.ec2.instance"].create(
            {"name": "s", "account_id": account.id, "aws_instance_id": "i-1",
             "instance_state": "running", "region": "us-east-1"}
        )
        operator = new_test_user(
            self.env, login="op_cloud",
            groups="primate_cloud_manager.group_cloud_operator",
        )
        with self.assertRaises(UserError):
            instance.with_user(operator).action_terminate()
