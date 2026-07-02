# -*- coding: utf-8 -*-
"""Tests de Fase 5: trazabilidad de repos, commits y módulos (todo mockeado).

GitHub (PyGithub) y SSM se mockean; los tests no tocan red ni infra real.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..services import github_api


def _fake_github_commit(sha, message="msg", author="Ana", date=None):
    """Objeto tipo PyGithub Commit para alimentar al servicio."""
    commit = mock.Mock()
    commit.sha = sha
    commit.commit.message = message
    commit.commit.author.name = author
    commit.commit.author.date = date
    return commit


# ----------------------------------------------------------------------------
# Servicio GitHub
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestGithubService(TransactionCase):
    def test_parse_repo_slug_formas(self):
        parse = github_api.GithubApiService.parse_repo_slug
        self.assertEqual(parse("https://github.com/odoo/odoo"), "odoo/odoo")
        self.assertEqual(parse("https://github.com/odoo/odoo.git"), "odoo/odoo")
        self.assertEqual(parse("git@github.com:OCA/server-tools.git"), "OCA/server-tools")
        self.assertEqual(parse("https://github.com/foo/bar", organization="org"), "org/bar")

    def test_parse_repo_slug_invalido(self):
        with self.assertRaises(ValueError):
            github_api.GithubApiService.parse_repo_slug("")
        with self.assertRaises(ValueError):
            github_api.GithubApiService.parse_repo_slug("https://github.com/solouno")

    def test_list_commits_normaliza_y_limita(self):
        client = mock.Mock()
        repo = client.get_repo.return_value
        repo.get_commits.return_value = [
            _fake_github_commit("h1", "feat: uno", "Ana"),
            _fake_github_commit("h2", "fix: dos", "Beto"),
            _fake_github_commit("h3", "tres"),
        ]
        service = github_api.GithubApiService(client=client)
        commits = service.list_commits("odoo/odoo", branch="19.0", limit=2)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["commit_hash"], "h1")
        self.assertEqual(commits[0]["author"], "Ana")
        self.assertEqual(commits[0]["branch"], "19.0")
        repo.get_commits.assert_called_once_with(sha="19.0")

    def test_get_latest_commit(self):
        client = mock.Mock()
        client.get_repo.return_value.get_commits.return_value = [
            _fake_github_commit("top")
        ]
        latest = github_api.GithubApiService(client=client).get_latest_commit("o/r")
        self.assertEqual(latest["commit_hash"], "top")


# ----------------------------------------------------------------------------
# Repositorio: commits, estado y módulos
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestRepository(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.env_rec = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
        })
        self.instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.env_rec.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
        })
        self.repo = self.env["primate.cloud.repository"].create({
            "name": "Odoo Core", "environment_id": self.env_rec.id,
            "github_url": "https://github.com/odoo/odoo",
            "configured_branch": "19.0", "local_path": "/opt/odoo/odoo",
        })

    # --- Token cifrado ---
    def test_token_se_cifra_y_enmascara(self):
        self.repo.github_token = "ghp_secreto"
        self.repo.invalidate_recordset(["github_token"])
        self.assertEqual(self.repo.github_token, "********")
        self.assertTrue(self.repo.github_token_encrypted)
        self.assertNotIn("ghp_secreto", self.repo.github_token_encrypted)
        self.assertEqual(self.repo._get_github_token(), "ghp_secreto")

    # --- Sincronización de commits (GitHub) ---
    def test_job_sync_commits_upsert_y_latest(self):
        fake = mock.Mock()
        fake.list_commits.return_value = [
            {"commit_hash": "h1", "message": "uno", "author": "Ana",
             "commit_date": False, "branch": "19.0"},
            {"commit_hash": "h2", "message": "dos", "author": "Beto",
             "commit_date": False, "branch": "19.0"},
        ]
        with mock.patch.object(github_api, "GithubApiService", return_value=fake):
            ok = self.repo.job_sync_commits()
        self.assertTrue(ok)
        self.assertEqual(len(self.repo.commit_ids), 2)
        self.assertEqual(self.repo.latest_commit, "h1")
        # Idempotente: segunda corrida no duplica.
        with mock.patch.object(github_api, "GithubApiService", return_value=fake):
            self.repo.job_sync_commits()
        self.assertEqual(len(self.repo.commit_ids), 2)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.repo.id), ("action_type", "=", "commit_sync"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_sync_commits_error(self):
        fake = mock.Mock()
        fake.list_commits.side_effect = Exception("rate limit")
        with mock.patch.object(github_api, "GithubApiService", return_value=fake):
            ok = self.repo.job_sync_commits()
        self.assertFalse(ok)
        self.assertEqual(self.repo.sync_state, "error")

    # --- Estado de sincronización (commit desplegado vía SSM) ---
    def _seed_commits(self, hashes):
        for h in hashes:
            self.env["primate.cloud.commit"].create(
                {"repository_id": self.repo.id, "commit_hash": h}
            )
        self.repo.latest_commit = hashes[0]

    def _patch_ssm(self, output):
        fake_ssm = mock.Mock()
        fake_ssm.run_script.return_value = output
        return mock.patch.object(
            type(self.instance), "_get_ssm_service", return_value=fake_ssm
        )

    def test_check_sync_state_updated(self):
        self._seed_commits(["h1", "h2", "h3"])
        with self._patch_ssm({"status": "Success", "stdout": "h1\n"}):
            self.repo.job_check_sync_state()
        self.assertEqual(self.repo.current_commit, "h1")
        self.assertEqual(self.repo.sync_state, "updated")
        self.assertTrue(self.repo.commit_ids.filtered(
            lambda c: c.commit_hash == "h1").is_current)

    def test_check_sync_state_outdated(self):
        self._seed_commits(["h1", "h2", "h3"])
        with self._patch_ssm({"status": "Success", "stdout": "h3"}):
            self.repo.job_check_sync_state()
        self.assertEqual(self.repo.sync_state, "outdated")

    def test_check_sync_state_divergent(self):
        self._seed_commits(["h1", "h2"])
        with self._patch_ssm({"status": "Success", "stdout": "zzz-manual"}):
            self.repo.job_check_sync_state()
        self.assertEqual(self.repo.sync_state, "divergent")

    def test_check_sync_state_ssm_falla(self):
        self._seed_commits(["h1"])
        with self._patch_ssm({"status": "Failed", "stderr": "no git"}):
            self.repo.job_check_sync_state()
        self.assertEqual(self.repo.sync_state, "error")

    def test_check_sync_sin_instancia_falla(self):
        self.instance.environment_id = False
        with self.assertRaises(UserError):
            self.repo.job_check_sync_state()

    # --- Detección de módulos (SSM + cruce con base) ---
    def test_job_detect_modules_cruza_base(self):
        # Base local asociada al entorno (para el cruce con ir_module_module).
        self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.env_rec.id, "db_type": "local_pg",
        })
        scan_json = (
            '[{"technical_name": "sale_custom", "functional_name": "Sale Custom",'
            ' "author": "Primate", "category": "Sales", "version": "19.0.1.0.0",'
            ' "depends": "sale,stock"},'
            ' {"technical_name": "no_instalado", "functional_name": "Otro",'
            ' "version": "19.0.1.0.0", "depends": ""}]'
        )
        db_lines = "sale_custom|installed|19.0.1.0.0\nbase|installed|19.0"
        fake_ssm = mock.Mock()
        fake_ssm.run_script.side_effect = [
            {"status": "Success", "stdout": scan_json},   # escaneo de módulos
            {"status": "Success", "stdout": db_lines},    # query a la base
        ]
        with mock.patch.object(type(self.instance), "_get_ssm_service", return_value=fake_ssm):
            ok = self.repo.job_detect_modules()
        self.assertTrue(ok)
        self.assertEqual(len(self.repo.module_ids), 2)
        instalado = self.repo.module_ids.filtered(
            lambda m: m.technical_name == "sale_custom")
        self.assertEqual(instalado.db_state, "installed")
        self.assertEqual(instalado.db_version, "19.0.1.0.0")
        self.assertEqual(instalado.depends, "sale,stock")
        ausente = self.repo.module_ids.filtered(
            lambda m: m.technical_name == "no_instalado")
        self.assertEqual(ausente.db_state, "not_found")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.repo.id), ("action_type", "=", "module_detect"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_detect_modules_sin_base_queda_not_found(self):
        scan_json = '[{"technical_name": "mod_a", "version": "1.0"}]'
        fake_ssm = mock.Mock()
        # Sin base asociada: _fetch_db_module_states no hace la 2da llamada.
        fake_ssm.run_script.return_value = {"status": "Success", "stdout": scan_json}
        with mock.patch.object(type(self.instance), "_get_ssm_service", return_value=fake_ssm):
            ok = self.repo.job_detect_modules()
        self.assertTrue(ok)
        self.assertEqual(self.repo.module_ids.technical_name, "mod_a")
        self.assertEqual(self.repo.module_ids.db_state, "not_found")

    def test_action_sync_commits_encola(self):
        with mock.patch.object(type(self.repo), "with_delay") as wd:
            self.repo.action_sync_commits()
            wd.assert_called_once()
