# -*- coding: utf-8 -*-
"""Tests de la bitácora inmutable."""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestOperationLog(TransactionCase):
    """La bitácora se crea pero no se edita ni elimina."""

    def setUp(self):
        super().setUp()
        self.Log = self.env["primate.cloud.operation.log"]

    def test_log_operation_basico(self):
        entry = self.Log.log_operation("sync", name="Sync de prueba")
        self.assertTrue(entry)
        self.assertEqual(entry.action_type, "sync")
        self.assertEqual(entry.result, "success")
        self.assertEqual(entry.user_id, self.env.user)

    def test_log_operation_con_recurso(self):
        account = self.env["primate.cloud.account"].create({"name": "Cuenta Test"})
        entry = self.Log.log_operation("connection_test", record=account, result="failed",
                                       error_message="boom")
        self.assertEqual(entry.resource_model, "primate.cloud.account")
        self.assertEqual(entry.resource_id, account.id)
        self.assertEqual(entry.resource_name, account.display_name)
        self.assertEqual(entry.result, "failed")
        self.assertEqual(entry.error_message, "boom")

    def test_write_bloqueado(self):
        entry = self.Log.log_operation("sync")
        with self.assertRaises(UserError):
            entry.write({"name": "modificado"})

    def test_unlink_bloqueado(self):
        entry = self.Log.log_operation("sync")
        with self.assertRaises(UserError):
            entry.unlink()
