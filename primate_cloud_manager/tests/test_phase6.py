# -*- coding: utf-8 -*-
"""Tests de Fase 6: deployments (git / módulos / servicios) y reversión.

SSM mockeado; los tests no tocan infra real.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestDeployment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.env_rec = self.env["primate.cloud.environment"].create(
            {"name": "Forum Prod", "project_id": self.project.id}
        )
        self.instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.env_rec.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
        })
        self.repo = self.env["primate.cloud.repository"].create({
            "name": "Core", "environment_id": self.env_rec.id,
            "local_path": "/opt/odoo/odoo", "configured_branch": "19.0",
        })

    def _deploy(self, **vals):
        base = {"environment_id": self.env_rec.id, "deployment_type": "pull",
                "repository_id": self.repo.id}
        base.update(vals)
        return self.env["primate.cloud.deployment"].create(base)

    def _patch_ssm(self, output):
        fake = mock.Mock()
        fake.run_script.return_value = output
        return mock.patch.object(
            type(self.instance), "_get_ssm_service", return_value=fake
        ), fake

    # --- Panel (dashboard) ---
    def test_dashboard_data(self):
        data = self.env["primate.cloud.dashboard"].get_dashboard_data()
        self.assertEqual(set(data), {"kpis", "recent_deploys", "alerts"})
        self.assertEqual(len(data["kpis"]), 4)
        self.assertEqual(
            {k["key"] for k in data["kpis"]},
            {"env_active", "ec2_running", "accounts_connected", "deploys_today"},
        )
        # Cada KPI trae datos para abrir su vista al click.
        for kpi in data["kpis"]:
            self.assertIn("model", kpi)
            self.assertIn("domain", kpi)

    # --- Panel de instancia (Bloque A) ---
    def test_server_detail_expone_panel(self):
        """get_server_detail alimenta el panel: repos del entorno, backups de sus
        BD, y el flag PCM (importada ⇒ False, no inventa paths)."""
        data = self.env["primate.cloud.dashboard"].get_server_detail(
            self.instance.id)
        # Instancia creada a mano (no por PCM) ⇒ False.
        self.assertFalse(data["provisioned_by_pcm"])
        # Trae los repos del entorno para la tab Addons.
        self.assertIn("repositories", data)
        self.assertIn(self.repo.id, [r["id"] for r in data["repositories"]])
        # Y la lista (vacía) de backups para la tab Backups.
        self.assertIn("backups", data)
        self.assertEqual(data["backups"], [])

    def test_register_provisioned_marca_flag_pcm(self):
        """Una instancia creada por PCM queda marcada: el panel puede confiar en
        el layout estándar de paths."""
        Ec2 = self.env["primate.cloud.ec2.instance"]
        aws_data = {"aws_instance_id": "i-prov", "name": "prov",
                    "instance_state": "running", "region": "us-east-1",
                    "instance_type": "t3.small"}
        inst = Ec2._register_provisioned(
            self.account, aws_data, environment=self.env_rec)
        self.assertTrue(inst.provisioned_by_pcm)
        detail = self.env["primate.cloud.dashboard"].get_server_detail(inst.id)
        self.assertTrue(detail["provisioned_by_pcm"])

    # --- Creación / nombre ---
    def test_create_asigna_referencia(self):
        dep = self._deploy()
        self.assertTrue(dep.name.startswith("Deploy #"))
        self.assertIn("Forum Prod", dep.name)

    # --- Validación ---
    def test_git_sin_repo_falla(self):
        dep = self._deploy(deployment_type="pull", repository_id=False)
        with self.assertRaises(UserError):
            dep.action_run()

    def test_checkout_branch_sin_rama_falla(self):
        dep = self._deploy(deployment_type="checkout_branch")
        with self.assertRaises(UserError):
            dep.action_run()

    def test_checkout_commit_sin_commit_falla(self):
        dep = self._deploy(deployment_type="checkout_commit")
        with self.assertRaises(UserError):
            dep.action_run()

    def test_action_run_encola_y_marca_pending(self):
        dep = self._deploy()
        with mock.patch.object(type(dep), "with_delay") as wd:
            dep.action_run()
            wd.assert_called_once()
        self.assertEqual(dep.state, "pending")
        self.assertEqual(dep.triggered_by, self.env.user)

    # --- Construcción del script ---
    def test_build_script_pull(self):
        script = self._deploy(deployment_type="pull")._build_deploy_script()
        self.assertIn("git pull --ff-only", script)
        self.assertIn("PCM_ORIGIN", script)
        self.assertIn("PCM_TARGET", script)

    def test_build_script_checkout_commit_reinicia(self):
        dep = self._deploy(deployment_type="checkout_commit", target_commit="abc123")
        script = dep._build_deploy_script()
        self.assertIn("git checkout abc123", script)
        self.assertIn("systemctl restart odoo", script)

    def test_build_script_service_restart(self):
        dep = self._deploy(deployment_type="service_restart", repository_id=False)
        script = dep._build_deploy_script()
        self.assertIn("systemctl restart odoo", script)
        self.assertIn("nginx", script)

    def test_build_script_module_update_requiere_base(self):
        dep = self._deploy(deployment_type="module_update", repository_id=False,
                           module_names="sale")
        # Sin base asociada: _db_name lanza UserError.
        with self.assertRaises(UserError):
            dep._build_deploy_script()
        self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.env_rec.id, "db_type": "local_pg",
        })
        script = dep._build_deploy_script()
        self.assertIn("-d forum -u sale", script)

    # --- Ejecución (job) ---
    def test_job_deploy_exito_git(self):
        dep = self._deploy(deployment_type="pull")
        patcher, _fake = self._patch_ssm({
            "status": "Success",
            "stdout": "PCM_ORIGIN:aaaa\nUpdating...\nPCM_TARGET:bbbb\n",
            "stderr": "",
        })
        with patcher:
            ok = dep.job_deploy()
        self.assertTrue(ok)
        self.assertEqual(dep.state, "success")
        self.assertEqual(dep.origin_commit, "aaaa")
        self.assertEqual(dep.target_commit, "bbbb")
        # El repo refleja el commit desplegado.
        self.assertEqual(self.repo.current_commit, "bbbb")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", dep.id), ("action_type", "=", "deploy"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)
        self.assertIn("STDOUT", dep.execution_log)

    def test_job_deploy_falla_ssm(self):
        dep = self._deploy(deployment_type="pull")
        patcher, _fake = self._patch_ssm({
            "status": "Failed", "stdout": "", "stderr": "conflict",
        })
        with patcher:
            ok = dep.job_deploy()
        self.assertFalse(ok)
        self.assertEqual(dep.state, "failed")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", dep.id), ("action_type", "=", "deploy"),
             ("result", "=", "failed")]
        )
        self.assertTrue(log)

    def test_job_deploy_sin_instancia_falla(self):
        self.instance.environment_id = False
        dep = self._deploy()
        with self.assertRaises(UserError):
            dep.job_deploy()

    # --- Reversión ---
    def test_action_revert_crea_y_vincula(self):
        dep = self._deploy(deployment_type="checkout_commit", target_commit="bbbb")
        dep.write({"state": "success", "origin_commit": "aaaa", "target_commit": "bbbb"})
        with mock.patch.object(type(dep), "with_delay"):
            dep.action_revert()
        revert = dep.revert_deployment_id
        self.assertTrue(revert)
        self.assertTrue(revert.is_revert)
        self.assertEqual(revert.reverts_deployment_id, dep)
        self.assertEqual(revert.deployment_type, "checkout_commit")
        self.assertEqual(revert.target_commit, "aaaa")  # vuelve al commit origen

    def test_revert_no_exitoso_falla(self):
        dep = self._deploy(deployment_type="pull")
        dep.state = "failed"
        with self.assertRaises(UserError):
            dep.action_revert()

    def test_revert_job_marca_original_reverted(self):
        orig = self._deploy(deployment_type="pull")
        orig.write({"state": "success", "origin_commit": "aaaa", "target_commit": "bbbb"})
        revert = self._deploy(deployment_type="checkout_commit", target_commit="aaaa",
                              is_revert=True, reverts_deployment_id=orig.id)
        patcher, _fake = self._patch_ssm({
            "status": "Success",
            "stdout": "PCM_ORIGIN:bbbb\nPCM_TARGET:aaaa\n", "stderr": "",
        })
        with patcher:
            ok = revert.job_deploy()
        self.assertTrue(ok)
        self.assertEqual(revert.state, "success")
        self.assertEqual(orig.state, "reverted")
