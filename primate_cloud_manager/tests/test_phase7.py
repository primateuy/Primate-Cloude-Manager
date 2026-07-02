# -*- coding: utf-8 -*-
"""Tests de Fase 7: staging (neutralización, scripts, wizard y flujo de 12 pasos).

AWS/SSM mockeados; los tests no tocan infra real.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


def _running_instance(instance_id="i-stg", name="staging", public_ip="1.2.3.4"):
    return {
        "Reservations": [{"Instances": [{
            "InstanceId": instance_id,
            "State": {"Name": "running"},
            "InstanceType": "t3.small",
            "PublicIpAddress": public_ip,
            "PrivateIpAddress": "10.0.0.9",
            "Tags": [{"Key": "Name", "Value": name}],
            "LaunchTime": None,
        }]}]
    }


def _staging_base():
    """Base falsa que responde a EC2, SSM (install), Route 53 y S3."""
    base = mock.Mock()
    client = base.get_client.return_value
    client.run_instances.return_value = {"Instances": [{"InstanceId": "i-stg"}]}
    client.describe_instances.return_value = _running_instance("i-stg")
    client.send_command.return_value = {"Command": {"CommandId": "cmd"}}
    client.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": "ok", "ResponseCode": 0,
    }
    client.list_buckets.return_value = {"Buckets": []}
    client.create_bucket.return_value = {}
    client.change_resource_record_sets.return_value = {"ChangeInfo": {"Id": "/c/1"}}
    return base


@tagged("post_install", "-at_install", "primate_cloud")
class TestStaging(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.origin = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id, "env_type": "production",
            "state": "active", "odoo_version": "19", "odoo_edition": "community",
            "main_url": "forum.primate.cloud",
        })
        self.origin_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-srv", "account_id": self.account.id,
            "environment_id": self.origin.id, "aws_instance_id": "i-prod",
            "instance_state": "running", "region": "us-east-1",
        })
        self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
        })
        self.env["primate.cloud.repository"].create({
            "name": "Custom", "environment_id": self.origin.id,
            "github_url": "https://github.com/primateuy/custom",
            "local_path": "/opt/odoo/custom", "current_commit": "abc123",
            "configured_branch": "19.0",
        })

    # --- Neutralización ---
    def test_render_neutralization_reemplaza_url(self):
        Env = self.env["primate.cloud.environment"]
        sql = Env._render_neutralization_sql("https://staging.forum.primate.cloud")
        self.assertIn("https://staging.forum.primate.cloud", sql)
        self.assertNotIn("%%STAGING_URL%%", sql)
        # Correcciones D5: sin el catchall inútil; desactiva todos los crons.
        self.assertNotIn("mail.catchall.alias", sql)
        self.assertIn("UPDATE ir_cron SET active = false", sql)
        self.assertIn("ir_mail_server", sql)

    # --- Constructores de scripts ---
    # NOTA (Fase 8, Bloque 5): _build_dump_script/_build_restore_script se
    # eliminaron; la copia de staging reusa el pipeline de backups (Bloques
    # 3-4), cuyos scripts tienen sus golden en test_phase8.

    def test_build_neutralize_y_clone_script(self):
        Env = self.env["primate.cloud.environment"]
        neutral = Env._build_neutralize_script("forum_staging", "SELECT 1;")
        self.assertIn("psql -d forum_staging", neutral)
        self.assertIn("PCMSQL", neutral)
        clone = Env._build_clone_script("https://github.com/x/y", "/opt/odoo/y", "abc123")
        self.assertIn("git clone https://github.com/x/y /opt/odoo/y", clone)
        self.assertIn("checkout abc123", clone)

    # --- Wizard ---
    def _wizard(self, **vals):
        base = {
            "origin_environment_id": self.origin.id, "region": "us-east-1",
            "name": "Forum Staging", "domain": "staging.forum.primate.cloud",
            "instance_type": "t3.small", "image_id": "ami-1",
            "transfer_bucket": "pcm-transfer", "db_mode": "local_pg",
            "create_dns": False,
        }
        base.update(vals)
        return self.env["primate.cloud.staging.create.wizard"].create(base)

    def test_wizard_sin_bucket_falla(self):
        # Espacios: pasa el NOT NULL del campo pero falla la validación.
        wiz = self._wizard(transfer_bucket="   ")
        with self.assertRaises(UserError):
            wiz.action_create_staging()

    def test_wizard_rds_sin_password_falla(self):
        wiz = self._wizard(db_mode="rds", rds_identifier="forum-stg-db")
        with self.assertRaises(UserError):
            wiz.action_create_staging()

    def test_wizard_recuerda_config(self):
        # Lo ingresado se guarda en el ORIGEN y el wizard se precarga al reabrir.
        Wiz = self.env["primate.cloud.staging.create.wizard"]
        wiz = self._wizard(image_id="ami-stg", instance_profile="pcm-ssm-role",
                           security_group_ids="sg-9")
        with mock.patch.object(type(self.origin), "with_delay"):
            wiz.action_create_staging()
        cfg = self.origin._load_provision_config(field="staging_config_encrypted")
        self.assertEqual(cfg["image_id"], "ami-stg")
        self.assertEqual(cfg["instance_profile"], "pcm-ssm-role")
        defaults = Wiz.with_context(
            default_origin_environment_id=self.origin.id
        ).default_get(list(Wiz._fields))
        self.assertEqual(defaults["image_id"], "ami-stg")
        self.assertEqual(defaults["security_group_ids"], "sg-9")
        self.assertEqual(defaults["instance_profile"], "pcm-ssm-role")

    def test_wizard_crea_staging_y_encola(self):
        wiz = self._wizard()
        with mock.patch.object(
            type(self.origin), "with_delay"
        ) as wd:
            wiz.action_create_staging()
            wd.assert_called_once()
        staging = self.env["primate.cloud.environment"].search(
            [("origin_environment_id", "=", self.origin.id)]
        )
        self.assertEqual(len(staging), 1)
        self.assertEqual(staging.env_type, "staging")
        self.assertEqual(staging.state, "provisioning")

    # --- Flujo completo (job) ---
    def _make_staging_env(self):
        return self.env["primate.cloud.environment"].create({
            "name": "Forum Staging", "project_id": self.project.id,
            "account_id": self.account.id, "env_type": "staging",
            "state": "provisioning", "odoo_version": "19", "odoo_edition": "community",
            "main_url": "staging.forum.primate.cloud",
            "origin_environment_id": self.origin.id,
        })

    def _params(self, **overrides):
        params = {
            "name": "Forum Staging", "region": "us-east-1",
            "domain": "staging.forum.primate.cloud", "instance_type": "t3.small",
            "image_id": "ami-1", "disk_size_gb": 30, "security_group_ids": [],
            "db_mode": "local_pg", "db_name": "forum_staging", "db_user": "odoo",
            "db_password": "secret", "transfer_bucket": "pcm-transfer",
            "create_dns": False, "admin_password": "adminpw",
        }
        params.update(overrides)
        return params

    def test_job_create_staging_end_to_end(self):
        staging = self._make_staging_env()
        base = _staging_base()
        fake_ssm = mock.Mock()
        # Un solo mock para todas las llamadas SSM (install, backup del origen,
        # restore, neutralización, clone, restart): emite los marcadores del
        # pipeline de Bloques 3-4 que la copia de staging reusa desde Fase 8.
        fake_ssm.run_script.return_value = {
            "status": "Success",
            "stdout": "PCM_DUMP_SIZE_BYTES=1048576\nPCM_FS_SIZE_BYTES=0\n"
                      "PCM_BACKUP_OK\nPCM_DROP_STARTED\nPCM_RESTORE_OK\n"
                      "NEUTRALIZED",
            "stderr": "",
        }
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base), \
             mock.patch.object(type(self.origin_instance), "_get_ssm_service",
                               return_value=fake_ssm):
            ok = staging.job_create_staging(self._params())
        self.assertTrue(ok)
        self.assertEqual(staging.state, "active")
        self.assertTrue(staging.staging_creation_date)
        self.assertTrue(staging.staging_origin_backup)
        self.assertTrue(staging.staging_neutralization_log)
        # EC2 propia del staging.
        self.assertEqual(len(staging.ec2_instance_ids), 1)
        # Repos clonados desde el origen.
        self.assertEqual(len(staging.repository_ids), 1)
        self.assertEqual(staging.repository_ids.current_commit, "abc123")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", staging.id), ("action_type", "=", "staging_create"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_create_staging_dump_falla_deja_error(self):
        staging = self._make_staging_env()
        base = _staging_base()
        fake_ssm = mock.Mock()
        # El pg_dump del origen falla.
        fake_ssm.run_script.return_value = {"status": "Failed", "stderr": "no pg_dump"}
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base), \
             mock.patch.object(type(self.origin_instance), "_get_ssm_service",
                               return_value=fake_ssm):
            ok = staging.job_create_staging(self._params())
        self.assertFalse(ok)
        self.assertEqual(staging.state, "error")

    # --- Refresh ---
    def test_action_refresh_no_staging_falla(self):
        with self.assertRaises(UserError):
            self.origin.action_refresh_staging()

    def test_job_refresh_staging(self):
        staging = self._make_staging_env()
        staging.write({"state": "active", "staging_origin_backup": "pcm-staging/1-forum.dump"})
        self.env["primate.cloud.ec2.instance"].create({
            "name": "stg-srv", "account_id": self.account.id,
            "environment_id": staging.id, "aws_instance_id": "i-stg",
            "instance_state": "running", "region": "us-east-1",
        })
        self.env["primate.cloud.database"].create({
            "name": "forum_staging", "account_id": self.account.id,
            "environment_id": staging.id, "db_type": "local_pg",
        })
        # El refresco reusa el pipeline de backups: el origen necesita un
        # bucket (política gestionada) para el dump fresco.
        policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Staging bucket (test)", "policy_type": "custom",
            "managed_by_pcm": True, "s3_bucket": "pcm-transfer",
        })
        self.origin.backup_policy_id = policy
        fake_ssm = mock.Mock()
        fake_ssm.run_script.return_value = {
            "status": "Success",
            "stdout": "PCM_DUMP_SIZE_BYTES=1048576\nPCM_FS_SIZE_BYTES=0\n"
                      "PCM_BACKUP_OK\nPCM_DROP_STARTED\nPCM_RESTORE_OK\n"
                      "NEUTRALIZED",
            "stderr": "",
        }
        base = _staging_base()
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base), \
             mock.patch.object(type(self.origin_instance), "_get_ssm_service",
                               return_value=fake_ssm):
            ok = staging.job_refresh_staging()
        self.assertTrue(ok)
        self.assertTrue(staging.staging_neutralization_log)
        # La fuente quedó registrada en el pipeline de backups.
        self.assertTrue(staging.staging_origin_backup)
