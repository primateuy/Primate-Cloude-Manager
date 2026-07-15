# -*- coding: utf-8 -*-
"""R4-B1: campos de runtime por instancia + GUARDS de flujos legacy.

La regla permanente que fija esta suite: un flujo aún no recableado que
ESCRIBE/BORRA con rutas del layout legacy se BLOQUEA en runtime sobre
servidores multi-Odoo (no una nota en un doc). El peor caso que motiva el
guard: el restore dropea la base POR NOMBRE y para la unit legacy — sobre
un servidor compartido destruiría la base VIVA de otro cliente. Cada bloque
de R4 levanta su guard al recablear su flujo.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B1(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id})
        self.policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Diaria", "expected_frequency": "daily",
            "managed_by_pcm": True, "s3_bucket": "pcm-test-bucket",
        })

    def _server(self, name, multi=False, segunda=False):
        """Servidor activo con máquina; multi=primaria materializada."""
        server = self.env["primate.cloud.environment"].create({
            "name": name, "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-%s" % name, "account_id": self.account.id,
            "aws_instance_id": "i-%s" % name, "instance_state": "running",
            "region": "us-east-1", "environment_id": server.id,
            "provisioned_by_pcm": True,
        })
        server.ec2_instance_id = machine
        server.state = "active"
        prim = server.primary_instance_id
        prim.state = "active"
        if multi:
            prim.write({"slug": "cliente-a", "service_name": "odoo-cliente-a",
                        "conf_path": "/etc/odoo/cliente-a.conf"})
        if segunda:
            server._create_instance_with_ports({
                "name": "Odoo B", "project_id": self.project.id,
                "slug": "cliente-b", "odoo_version": "19",
            })
        return server, machine

    # ------------------------------------------------------------------
    # Campos de runtime por instancia
    # ------------------------------------------------------------------
    def test_campos_runtime_defaults_legacy(self):
        server, _m = self._server("legacy1")
        prim = server.primary_instance_id
        self.assertEqual(prim.python_bin, "/opt/odoo/venv/bin/python3")
        self.assertEqual(prim.odoo_bin, "/opt/odoo/odoo/odoo-bin")
        self.assertEqual(prim.log_path, "/var/log/odoo/odoo.log")

    def test_materialize_escribe_runtime_y_log(self):
        server, _m = self._server("mat1")
        prim = server.primary_instance_id
        prim.slug = "cliente-a"
        server._materialize_multiodoo_layout(prim, {"db_mode": "local_pg"})
        self.assertEqual(prim.python_bin,
                         "/opt/pcm/runtime/odoo-19/venv/bin/python3")
        self.assertEqual(prim.odoo_bin, "/opt/pcm/runtime/odoo-19/src/odoo-bin")
        self.assertEqual(prim.log_path,
                         "/opt/pcm/instances/cliente-a/log/odoo.log")

    # ------------------------------------------------------------------
    # El guard: razón y entradas interactivas
    # ------------------------------------------------------------------
    def test_guard_razon_multi_ambiguo_y_legacy(self):
        legacy, _m = self._server("g-legacy")
        self.assertFalse(legacy._legacy_flow_blocked_reason("restore"))
        multi, _m = self._server("g-multi", multi=True)
        self.assertIn("bloqueada", multi._legacy_flow_blocked_reason("restore"))
        # Dos instancias no archivadas = destino ambiguo, aunque ambas legacy.
        ambiguo, _m = self._server("g-ambiguo", segunda=True)
        self.assertTrue(ambiguo._legacy_flow_blocked_reason("backup"))
        # Una archivada NO cuenta (la primaria legacy sigue operable).
        archivada, _m = self._server("g-arch", segunda=True)
        archivada.instance_ids.filtered(
            lambda i: i.slug == "cliente-b").write(
            {"state": "archived", "active": False})
        self.assertFalse(archivada._legacy_flow_blocked_reason("backup"))

    def test_guards_interactivos_bloquean_en_multi(self):
        server, machine = self._server("g-int", multi=True)
        server.backup_policy_id = self.policy
        with self.assertRaises(UserError):
            server.action_run_backup()
        with self.assertRaises(UserError):
            server.add_addon({"github_url": "https://github.com/x/y"})
        with self.assertRaises(UserError):
            server._enqueue_staging({"name": "stg"})
        with self.assertRaises(UserError):
            machine.action_save_config({"workers": "2"}, "hash-x")
        with self.assertRaises(UserError):
            machine.deploy_impersonate(["db1"])
        with self.assertRaises(UserError):
            machine.set_impersonate_enabled(["db1"], True)
        wizard = self.env["primate.cloud.backup.restore.wizard"].new({
            "target_environment_id": server.id})
        with self.assertRaises(UserError):
            wizard.action_restore()

    def test_guard_jobs_fallan_honesto(self):
        server, machine = self._server("g-job", multi=True)
        server.backup_policy_id = self.policy
        Log = self.env["primate.cloud.operation.log"]
        # Backup gestionado: False + bitácora con la razón (no skip silencioso).
        self.assertFalse(server.job_run_backup())
        log = Log.search([("action_type", "=", "backup_run"),
                          ("result", "=", "failed"),
                          ("resource_id", "=", server.id)], limit=1,
                         order="id desc")
        self.assertIn("bloqueada", log.error_message)
        # Restore: el peor caso — corta ANTES de tocar nada.
        self.assertFalse(server.job_restore_backup(
            {"backup_id": 99999999, "db_name": "x", "instance_id": machine.id,
             "pre_backup": False}))
        log = Log.search([("action_type", "=", "backup_restore"),
                          ("result", "=", "failed")], limit=1, order="id desc")
        self.assertIn("bloqueada", log.error_message)
        # Config: el job devuelve 'blocked' sin correr SSM.
        self.assertEqual(machine.job_save_config({"workers": "2"}, "h"),
                         "blocked")

    def test_guard_no_bloquea_legacy_puro(self):
        server, machine = self._server("g-ok")
        server.backup_policy_id = self.policy
        with mock.patch.object(type(server), "with_delay") as delayed:
            server.action_run_backup()
            delayed.assert_called_once()
        with mock.patch.object(type(server), "with_delay"):
            repo = server.add_addon({"github_url": "https://github.com/x/y"})
        self.assertTrue(repo)

    # ------------------------------------------------------------------
    # Deploy recableado (fix en vez de guard)
    # ------------------------------------------------------------------
    def _deployment(self, server, dtype, **vals):
        base = {"environment_id": server.id, "deployment_type": dtype}
        base.update(vals)
        return self.env["primate.cloud.deployment"].create(base)

    def test_deploy_script_usa_la_instancia_objetivo(self):
        server, _m = self._server("dep-multi", multi=True)
        prim = server.primary_instance_id
        prim.write({"python_bin": "/opt/pcm/runtime/odoo-19/venv/bin/python3",
                    "odoo_bin": "/opt/pcm/runtime/odoo-19/src/odoo-bin"})
        db = self.env["primate.cloud.database"].create({
            "name": "cliente_a_db", "account_id": self.account.id,
            "environment_id": server.id, "db_type": "local_pg",
        })
        prim.database_id = db
        dep = self._deployment(server, "module_update", module_names="sale")
        script = dep._build_deploy_script()
        self.assertIn("/opt/pcm/runtime/odoo-19/venv/bin/python3", script)
        self.assertIn("/opt/pcm/runtime/odoo-19/src/odoo-bin", script)
        self.assertIn("-c /etc/odoo/cliente-a.conf", script)
        self.assertIn("-d cliente_a_db -u sale", script)
        self.assertIn("systemctl restart odoo-cliente-a", script)
        self.assertNotIn("/opt/odoo/", script)
        restart = self._deployment(server, "service_restart")
        self.assertIn("systemctl restart odoo-cliente-a",
                      restart._build_deploy_script())

    def test_deploy_script_legacy_intacto(self):
        server, _m = self._server("dep-legacy")
        self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": server.id, "db_type": "local_pg",
        })
        dep = self._deployment(server, "module_update", module_names="sale")
        script = dep._build_deploy_script()
        self.assertIn("/opt/odoo/venv/bin/python3", script)
        self.assertIn("-c /etc/odoo/odoo.conf", script)
        self.assertIn("systemctl restart odoo\n", script + "\n")

    # ------------------------------------------------------------------
    # add_addon: ruta por instancia
    # ------------------------------------------------------------------
    def test_add_addon_local_path_por_instancia(self):
        server, _m = self._server("addon1")
        prim = server.primary_instance_id
        with mock.patch.object(type(server), "with_delay"):
            repo = server.add_addon({"github_url": "https://github.com/x/wms"})
        self.assertEqual(repo.local_path, "/opt/odoo/custom-addons/wms")
        self.assertEqual(repo.instance_id, prim)
        prim.addons_dir = "/otra/ruta/addons"
        with mock.patch.object(type(server), "with_delay"):
            repo2 = server.add_addon({"github_url": "https://github.com/x/crm",
                                      "instance_id": prim.id})
        self.assertEqual(repo2.local_path, "/otra/ruta/addons/crm")
