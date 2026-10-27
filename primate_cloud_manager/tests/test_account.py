# -*- coding: utf-8 -*-
"""Tests de la cuenta AWS: cifrado de credenciales y validación de conexión."""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from ..models import primate_cloud_account
from ..services import aws_base


@tagged("post_install", "-at_install", "primate_cloud")
class TestAccount(TransactionCase):
    """Credenciales cifradas en BD, secreto enmascarado y flujo de validación."""

    def setUp(self):
        super().setUp()
        self.Account = self.env["primate.cloud.account"]

    def test_credenciales_se_guardan_cifradas(self):
        account = self.Account.create(
            {
                "name": "Cuenta Cifrado",
                "iam_access_key_id": "AKIAEXAMPLE",
                "iam_secret_access_key": "secreto-real",
            }
        )
        # En BD no se guarda el texto plano.
        self.assertTrue(account.iam_access_key_id_encrypted)
        self.assertNotEqual(account.iam_access_key_id_encrypted, "AKIAEXAMPLE")
        self.assertNotIn("secreto-real", account.iam_secret_access_key_encrypted or "")
        # Lectura fresca (como en el formulario): se recalcula desde lo cifrado.
        account.invalidate_recordset(["iam_access_key_id", "iam_secret_access_key"])
        # Ni el Access Key ID sale en claro (2026-10-27): los dos enmascarados.
        self.assertEqual(account.iam_access_key_id, "AKIA…MPLE")
        self.assertEqual(
            account.iam_secret_access_key, primate_cloud_account.SECRET_MASK
        )

    def test_decrypted_credentials_internas(self):
        account = self.Account.create(
            {
                "name": "Cuenta Creds",
                "iam_access_key_id": "AKIA2",
                "iam_secret_access_key": "shhh",
            }
        )
        creds = account._get_decrypted_credentials()
        self.assertEqual(creds["access_key_id"], "AKIA2")
        self.assertEqual(creds["secret_access_key"], "shhh")

    def test_mascara_no_sobreescribe_secreto(self):
        account = self.Account.create(
            {
                "name": "Cuenta Mask",
                "iam_access_key_id": "AKIA3",
                "iam_secret_access_key": "original",
            }
        )
        original_cipher = account.iam_secret_access_key_encrypted
        # Reescribir con la máscara no debe cambiar el secreto almacenado.
        account.write({"iam_secret_access_key": primate_cloud_account.SECRET_MASK})
        self.assertEqual(account.iam_secret_access_key_encrypted, original_cipher)

    def test_job_validate_connection_exito(self):
        account = self.Account.create(
            {
                "name": "Cuenta OK",
                "iam_access_key_id": "AKIA",
                "iam_secret_access_key": "sk",
            }
        )
        fake_service = mock.Mock()
        fake_service.test_connection.return_value = {
            "success": True,
            "account_id": "123456789012",
            "error": None,
        }
        with mock.patch.object(type(account), "_get_aws_service", return_value=fake_service):
            account.job_validate_connection()
        self.assertEqual(account.connection_state, "connected")
        self.assertEqual(account.aws_account_id, "123456789012")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_model", "=", "primate.cloud.account"),
             ("resource_id", "=", account.id),
             ("action_type", "=", "connection_test")]
        )
        self.assertTrue(log)
        self.assertEqual(log[0].result, "success")

    def test_job_validate_connection_falla(self):
        account = self.Account.create(
            {
                "name": "Cuenta Falla",
                "iam_access_key_id": "AKIA",
                "iam_secret_access_key": "sk",
            }
        )
        fake_service = mock.Mock()
        fake_service.test_connection.return_value = {
            "success": False,
            "account_id": None,
            "error": "AccessDenied",
        }
        with mock.patch.object(type(account), "_get_aws_service", return_value=fake_service):
            result = account.job_validate_connection()
        self.assertFalse(result)
        self.assertEqual(account.connection_state, "error")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", account.id), ("result", "=", "failed")]
        )
        self.assertTrue(log)
        self.assertIn("AccessDenied", log[0].error_message)

    # ---- keys cruzadas: usuario IAM esperado (2026-10-27) ----
    def _validar(self, expected, arn):
        account = self.Account.create({
            "name": "Cuenta %s" % (expected or "libre"),
            "iam_access_key_id": "AKIA", "iam_secret_access_key": "sk",
            "expected_iam_user": expected,
        })
        fake = mock.Mock()
        fake.test_connection.return_value = {
            "success": True, "account_id": "123456789012", "arn": arn,
            "error": None}
        with mock.patch.object(type(account), "_get_aws_service",
                               return_value=fake):
            ok = account.job_validate_connection()
        return account, ok

    def test_nombre_del_principal_desde_el_arn(self):
        f = primate_cloud_account.iam_principal_name
        self.assertEqual(f("arn:aws:iam::123456789012:user/pcm-prod-ro"),
                         "pcm-prod-ro")
        self.assertEqual(f("arn:aws:iam::123456789012:user/ruta/pcm-prod-prov"),
                         "pcm-prod-prov")
        self.assertEqual(
            f("arn:aws:sts::123456789012:assumed-role/PcmRol/sesion"), "PcmRol")
        self.assertEqual(f(""), "")

    def test_validar_con_el_usuario_esperado_conecta_y_guarda_el_arn(self):
        arn = "arn:aws:iam::123456789012:user/pcm-prod-prov"
        account, ok = self._validar("pcm-prod-prov", arn)
        self.assertTrue(ok)
        self.assertEqual(account.connection_state, "connected")
        self.assertEqual(account.caller_arn, arn)

    def test_validar_con_keys_cruzadas_falla_con_mensaje_claro(self):
        # Keys de lectura pegadas en la cuenta de aprovisionamiento.
        arn = "arn:aws:iam::123456789012:user/pcm-prod-ro"
        account, ok = self._validar("pcm-prod-prov", arn)
        self.assertFalse(ok)
        self.assertEqual(account.connection_state, "error")
        self.assertEqual(account.caller_arn, arn)
        self.assertFalse(account.aws_account_id)
        log = self.env["primate.cloud.operation.log"].search([
            ("resource_id", "=", account.id), ("result", "=", "failed"),
            ("action_type", "=", "connection_test")])
        self.assertTrue(log)
        self.assertIn("pcm-prod-ro", log[0].error_message)
        self.assertIn("pcm-prod-prov", log[0].error_message)
        self.assertIn("cruzadas", log[0].error_message)

    def test_sin_usuario_esperado_no_se_controla(self):
        account, ok = self._validar(False, "arn:aws:iam::1:user/cualquiera")
        self.assertTrue(ok)
        self.assertEqual(account.connection_state, "connected")

    def test_test_connection_devuelve_el_arn(self):
        service = aws_base.AwsBaseService.__new__(aws_base.AwsBaseService)
        sts = mock.Mock()
        sts.get_caller_identity.return_value = {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/pcm-prod-ro"}
        with mock.patch.object(aws_base.AwsBaseService, "get_client",
                               return_value=sts):
            result = service.test_connection()
        self.assertEqual(result["arn"],
                         "arn:aws:iam::123456789012:user/pcm-prod-ro")

    def test_assume_role_requiere_role_arn(self):
        account = self.Account.create(
            {"name": "Cuenta Rol", "auth_method": "assume_role"}
        )
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            account._get_aws_service()

    def test_action_validate_connection_encola_job(self):
        account = self.Account.create(
            {
                "name": "Cuenta Encola",
                "iam_access_key_id": "AKIA",
                "iam_secret_access_key": "sk",
            }
        )
        # with_delay debe usarse (no ejecución síncrona en el botón).
        with mock.patch.object(type(account), "with_delay") as with_delay:
            account.action_validate_connection()
            with_delay.assert_called_once()
