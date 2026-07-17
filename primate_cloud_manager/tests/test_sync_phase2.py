# -*- coding: utf-8 -*-
"""Tests del upsert de inventario y de la orquestación de sincronización."""
import json
from datetime import datetime, timezone
from unittest import mock

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestInventorySync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({"name": "Cuenta Sync"})

    # --- EC2 ---
    def test_ec2_upsert(self):
        Ec2 = self.env["primate.cloud.ec2.instance"]
        data = [
            {
                "aws_instance_id": "i-1",
                "name": "srv-1",
                "instance_state": "running",
                "instance_type": "t3.small",
                "region": "us-east-1",
                "public_ip": "1.1.1.1",
                "private_ip": "10.0.0.1",
                "tags": {"Name": "srv-1", "env": "prod"},
                "created_at": datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc),
            }
        ]
        res = Ec2._sync_from_aws(self.account, data)
        self.assertEqual(res, {"created": 1, "updated": 0})
        inst = Ec2.search([("account_id", "=", self.account.id), ("aws_instance_id", "=", "i-1")])
        self.assertEqual(inst.name, "srv-1")
        self.assertEqual(inst.instance_state, "running")
        self.assertEqual(json.loads(inst.aws_tags)["env"], "prod")
        # created_at se guarda naive (sin tzinfo).
        self.assertIsNone(inst.aws_created_at.tzinfo)

        # Segundo sync con cambio: actualiza, no duplica.
        data[0]["instance_state"] = "stopped"
        res2 = Ec2._sync_from_aws(self.account, data)
        self.assertEqual(res2, {"created": 0, "updated": 1})
        inst.invalidate_recordset()
        self.assertEqual(inst.instance_state, "stopped")
        self.assertEqual(Ec2.search_count([("aws_instance_id", "=", "i-1")]), 1)

    def test_ec2_no_pisa_entorno(self):
        Ec2 = self.env["primate.cloud.ec2.instance"]
        partner = self.env["res.partner"].create({"name": "Cliente Test SyncPhase2"})
        env = self.env["primate.cloud.environment"].create(
            {"name": "Env", "project_id": self.env["primate.cloud.project"].create({"name": "P", "partner_id": partner.id}).id}
        )
        data = [{"aws_instance_id": "i-9", "name": "s", "instance_state": "running",
                 "region": "us-east-1", "tags": {}, "created_at": None}]
        Ec2._sync_from_aws(self.account, data)
        inst = Ec2.search([("aws_instance_id", "=", "i-9")])
        inst.environment_id = env
        # Re-sincronizar no debe borrar la asociación manual.
        Ec2._sync_from_aws(self.account, data)
        inst.invalidate_recordset()
        self.assertEqual(inst.environment_id, env)

    # --- RDS ---
    def test_rds_solo_postgres_y_version(self):
        Db = self.env["primate.cloud.database"]
        data = [
            {"rds_identifier": "pg1", "name": "pg1", "engine": "postgres",
             "engine_version": "16.1", "rds_instance_class": "db.t3.medium",
             "rds_storage_gb": 20, "rds_multi_az": False, "backup_retention_days": 7,
             "status": "available", "endpoint": "pg1.rds"},
            {"rds_identifier": "my1", "name": "my1", "engine": "mysql",
             "engine_version": "8.0", "status": "available"},
            {"rds_identifier": "pg2", "name": "pg2", "engine": "postgres",
             "engine_version": "11.5", "status": "backing-up"},
        ]
        res = Db._sync_rds_from_aws(self.account, data)
        self.assertEqual(res["created"], 2)  # los dos postgres
        self.assertEqual(res["skipped"], 1)  # el mysql
        pg1 = Db.search([("rds_identifier", "=", "pg1")])
        self.assertEqual(pg1.pg_version, "16")
        self.assertEqual(pg1.state, "available")
        pg2 = Db.search([("rds_identifier", "=", "pg2")])
        # version 11 no soportada -> False; status no nominal -> error
        self.assertFalse(pg2.pg_version)
        self.assertEqual(pg2.state, "error")

    # --- DNS ---
    def test_dns_filtra_tipos(self):
        Dns = self.env["primate.cloud.dns.record"]
        data = [
            {"hosted_zone_id": "Z1", "name": "a.primate.cloud", "record_type": "A",
             "record_value": "1.2.3.4", "ttl": 300},
            {"hosted_zone_id": "Z1", "name": "primate.cloud", "record_type": "NS",
             "record_value": "ns-1", "ttl": 172800},
        ]
        res = Dns._sync_from_aws(self.account, data)
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["skipped"], 1)
        self.assertTrue(Dns.search([("name", "=", "a.primate.cloud"), ("record_type", "=", "A")]))


@tagged("post_install", "-at_install", "primate_cloud")
class TestSyncOrchestration(TransactionCase):
    def test_job_sync_resources_exito(self):
        account = self.env["primate.cloud.account"].create(
            {"name": "Cuenta Orq", "iam_access_key_id": "AK", "iam_secret_access_key": "sk"}
        )
        fake_base = mock.Mock()
        ec2_data = [{"aws_instance_id": "i-1", "name": "n", "instance_state": "running",
                     "region": "us-east-1", "tags": {}, "created_at": None}]
        rds_data = [{"rds_identifier": "d1", "name": "d1", "engine": "postgres",
                     "engine_version": "15.4", "status": "available"}]

        with mock.patch.object(type(account), "_get_aws_service", return_value=fake_base), \
             mock.patch("odoo.addons.primate_cloud_manager.models.primate_cloud_account.aws_ec2.AwsEc2Service") as Ec2S, \
             mock.patch("odoo.addons.primate_cloud_manager.models.primate_cloud_account.aws_rds.AwsRdsService") as RdsS, \
             mock.patch("odoo.addons.primate_cloud_manager.models.primate_cloud_account.aws_route53.AwsRoute53Service") as R53S:
            Ec2S.return_value.list_instances.return_value = ec2_data
            RdsS.return_value.list_instances.return_value = rds_data
            R53S.return_value.list_zones.return_value = [{"id": "Z1", "name": "primate.cloud"}]
            R53S.return_value.list_records.return_value = [
                {"hosted_zone_id": "Z1", "name": "forum.primate.cloud", "record_type": "A",
                 "record_value": "1.2.3.4", "ttl": 300}
            ]
            result = account.job_sync_resources()

        self.assertTrue(result)
        self.assertTrue(account.last_sync_date)
        self.assertEqual(self.env["primate.cloud.ec2.instance"].search_count([("account_id", "=", account.id)]), 1)
        self.assertEqual(self.env["primate.cloud.database"].search_count([("account_id", "=", account.id)]), 1)
        self.assertEqual(self.env["primate.cloud.dns.record"].search_count([("account_id", "=", account.id)]), 1)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", account.id), ("action_type", "=", "sync"), ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_sync_resources_falla_queda_en_log(self):
        account = self.env["primate.cloud.account"].create(
            {"name": "Cuenta Err", "iam_access_key_id": "AK", "iam_secret_access_key": "sk"}
        )
        with mock.patch.object(type(account), "_get_aws_service", side_effect=Exception("boom")):
            result = account.job_sync_resources()
        self.assertFalse(result)
        self.assertFalse(account.last_sync_date)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", account.id), ("action_type", "=", "sync"), ("result", "=", "failed")]
        )
        self.assertTrue(log)
        self.assertIn("boom", log.error_message)

    def test_action_sync_encola_job(self):
        account = self.env["primate.cloud.account"].create({"name": "Cuenta Enc"})
        with mock.patch.object(type(account), "with_delay") as with_delay:
            account.action_sync_resources()
            with_delay.assert_called_once()
