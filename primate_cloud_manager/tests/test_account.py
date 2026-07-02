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
        # El Access Key ID se puede leer descifrado; el secreto se enmascara.
        self.assertEqual(account.iam_access_key_id, "AKIAEXAMPLE")
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
