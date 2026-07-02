# -*- coding: utf-8 -*-
"""Tests de proyecto y entorno (relaciones y herencia de cuenta)."""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestProjectEnvironment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({"name": "Cuenta Proj"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )

    def test_environment_hereda_cuenta_del_proyecto(self):
        env = self.env["primate.cloud.environment"].create(
            {"name": "Forum Prod", "project_id": self.project.id}
        )
        self.assertEqual(env.account_id, self.account)

    def test_environment_permite_sobrescribir_cuenta(self):
        otra = self.env["primate.cloud.account"].create({"name": "Otra"})
        env = self.env["primate.cloud.environment"].create(
            {"name": "Forum Stg", "project_id": self.project.id, "account_id": otra.id}
        )
        self.assertEqual(env.account_id, otra)

    def test_project_environment_count(self):
        self.env["primate.cloud.environment"].create(
            {"name": "E1", "project_id": self.project.id}
        )
        self.env["primate.cloud.environment"].create(
            {"name": "E2", "project_id": self.project.id}
        )
        self.project.invalidate_recordset(["environment_count"])
        self.assertEqual(self.project.environment_count, 2)
