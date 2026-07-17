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
        self.partner = self.env["res.partner"].create({"name": "Cliente Test Phase6"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id, "partner_id": self.partner.id}
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

    def _patch_ssm_routed(self, routes):
        """SSM falso que responde según un substring del comment. Un valor lista
        se consume como secuencia (para simular polls de health sucesivos)."""
        fake = mock.Mock()
        counters = {}

        def run_script(instance_id, cmd, **kw):
            comment = kw.get("comment", "")
            for key, resp in routes.items():
                if key in comment:
                    if isinstance(resp, list):
                        idx = min(counters.get(key, 0), len(resp) - 1)
                        counters[key] = counters.get(key, 0) + 1
                        return resp[idx]
                    return resp
            return {"stdout": "", "status": "Success"}

        fake.run_script.side_effect = run_script
        return mock.patch.object(
            type(self.instance), "_get_ssm_service", return_value=fake
        ), fake

    def _last_config_log(self):
        return self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "config_edit")], order="id desc", limit=1)

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
        """get_server_detail = SOLO la máquina (R4-B6.3): info AWS, métricas y la
        lista de instancias hospedadas. Config/repos/backups son de la
        INSTANCIA (get_odoo_instance_detail), no del servidor."""
        data = self.env["primate.cloud.dashboard"].get_server_detail(
            self.instance.id)
        # Instancia creada a mano (no por PCM) ⇒ False.
        self.assertFalse(data["provisioned_by_pcm"])
        # El servidor lista sus instancias hospedadas (con cliente/env_type).
        self.assertIn("hosted_instances", data)
        self.assertIn("hosted_summary", data)
        self.assertIn("metrics", data)
        # Los campos por-instancia se RETIRARON del serializer de servidor.
        for retirado in ("repositories", "backups", "is_production", "runtime",
                         "primary_instance_id", "main_url"):
            self.assertNotIn(retirado, data,
                             "%s ya no es del servidor (es de la instancia)"
                             % retirado)

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

    # --- Runtime probe (Bloque B1) ---
    def test_probe_runtime_requiere_pcm(self):
        """Sin provisioned_by_pcm no se sondea (paths desconocidos)."""
        self.assertFalse(self.instance.provisioned_by_pcm)
        with self.assertRaises(UserError):
            self.instance.action_probe_runtime()

    def test_parse_runtime_marcadores(self):
        """Parsea PCM_PY/ODOO/WORKERS y escribe el cache."""
        stdout = ("PCM_PY:Python 3.12.3\n"
                  "PCM_ODOO:Odoo Server 19.0\n"
                  "PCM_WORKERS:workers=2\n")
        self.instance._parse_runtime(stdout)
        self.assertEqual(self.instance.runtime_python_version, "3.12.3")
        self.assertEqual(self.instance.runtime_odoo_version, "Odoo Server 19.0")
        self.assertEqual(self.instance.runtime_workers, "2")
        self.assertTrue(self.instance.last_runtime_probe)

    def test_parse_runtime_sin_workers_default_cero(self):
        """Sin línea workers en el conf ⇒ default de Odoo (0), no vacío."""
        self.instance._parse_runtime("PCM_PY:Python 3.12.3\nPCM_WORKERS:\n")
        self.assertEqual(self.instance.runtime_workers, "0")

    def test_probe_runtime_comando_sin_credenciales(self):
        """El sondeo NO vuelca el odoo.conf ni credenciales (solo grep de una
        línea de workers)."""
        self.instance.provisioned_by_pcm = True
        self.instance.instance_state = "running"
        patcher, fake = self._patch_ssm({"stdout": "", "status": "Success"})
        with patcher:
            self.instance.job_probe_runtime()
        cmd = fake.run_script.call_args[0][1]
        self.assertNotIn("db_password", cmd)
        self.assertNotIn("admin_passwd", cmd)
        self.assertNotIn("cat ", cmd)   # nunca vuelca el archivo entero
        self.assertIn("workers", cmd)   # solo la línea de workers

    # --- Logs streaming incremental (Bloque B2) ---
    # R4-B2: "odoo" pasó a streaming de ARCHIVO (el log_path de la instancia,
    # igual en legacy y multi); el mecanismo journal se fija con "postgres".
    def test_logs_stream_journal_parsea_cursor(self):
        """Primer poll (sin cursor): usa -n y --show-cursor; separa el cursor."""
        out = ("2026-01-01T00:00 log line 1\n2026-01-01T00:01 log line 2\n"
               "-- cursor: s=abc123\n")
        patcher, fake = self._patch_ssm({"stdout": out, "status": "Success"})
        with patcher:
            res = self.instance.fetch_logs_stream("postgres", from_cursor=False)
        self.assertEqual(res["cursor"], "s=abc123")
        self.assertIn("log line 1", res["text"])
        self.assertNotIn("-- cursor:", res["text"])
        cmd = fake.run_script.call_args[0][1]
        self.assertIn("--show-cursor", cmd)
        self.assertIn("-u postgresql", cmd)
        self.assertNotIn("--after-cursor", cmd)

    def test_logs_stream_journal_incremental(self):
        """Con cursor: usa --after-cursor y sin novedad devuelve texto vacío."""
        patcher, fake = self._patch_ssm(
            {"stdout": "-- cursor: s=z9\n", "status": "Success"})
        with patcher:
            res = self.instance.fetch_logs_stream("postgres",
                                                  from_cursor="s=abc123")
        self.assertEqual(res["cursor"], "s=z9")
        self.assertEqual(res["text"], "")
        cmd = fake.run_script.call_args[0][1]
        self.assertIn("--after-cursor", cmd)
        self.assertIn("s=abc123", cmd)

    def test_logs_stream_odoo_por_archivo_de_la_instancia(self):
        """R4-B2: "odoo" streamea el log_path de la instancia (offset)."""
        out = "linea odoo\nPCM_OFFSET:2048\n"
        patcher, fake = self._patch_ssm({"stdout": out, "status": "Success"})
        with patcher:
            res = self.instance.fetch_logs_stream(
                "odoo", from_cursor=False,
                odoo_instance=self.env_rec.primary_instance_id)
        self.assertEqual(res["cursor"], "2048")
        cmd = fake.run_script.call_args[0][1]
        # log_path de la primaria (legacy en este entorno).
        self.assertIn("/var/log/odoo/odoo.log", cmd)
        self.assertIn("PCM_OFFSET", cmd)

    def test_logs_stream_archivo_offset(self):
        """Fuente de archivo: cursor = offset de bytes; tail -c +(offset+1)."""
        out = "linea nueva nginx\nPCM_OFFSET:4096\n"
        patcher, fake = self._patch_ssm({"stdout": out, "status": "Success"})
        with patcher:
            res = self.instance.fetch_logs_stream("nginx", from_cursor="1024")
        self.assertEqual(res["cursor"], "4096")
        self.assertIn("linea nueva nginx", res["text"])
        self.assertNotIn("PCM_OFFSET", res["text"])
        self.assertIn("tail -c +1025", fake.run_script.call_args[0][1])

    def test_logs_stream_sin_credenciales(self):
        """El streaming NO referencia el odoo.conf ni credenciales."""
        patcher, fake = self._patch_ssm({"stdout": "", "status": "Success"})
        with patcher:
            self.instance.fetch_logs_stream(
                "odoo", from_cursor=False,
                odoo_instance=self.env_rec.primary_instance_id)
        cmd = fake.run_script.call_args[0][1]
        self.assertNotIn("db_password", cmd)
        self.assertNotIn("odoo.conf", cmd)
        self.assertNotIn("cat ", cmd)

    def test_logs_stream_instancia_detenida(self):
        """El wrapper del dashboard corta si la instancia no corre (echoa cursor)."""
        self.instance.instance_state = "stopped"
        res = self.env["primate.cloud.dashboard"].get_odoo_logs_stream(
            self.env_rec.primary_instance_id.id, "odoo", "s=prev")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["cursor"], "s=prev")

    # --- Editar config / odoo.conf (Bloque B3) ---
    def test_validate_config_castea_y_rechaza(self):
        inst = self.instance
        clean = inst._validate_config(
            {"workers": "2", "proxy_mode": True, "log_level": "info"})
        self.assertEqual(
            clean, {"workers": "2", "proxy_mode": "True", "log_level": "info"})
        with self.assertRaises(UserError):   # no castea
            inst._validate_config({"limit_time_cpu": "abc"})
        with self.assertRaises(UserError):   # fuera de rango
            inst._validate_config({"workers": "999"})
        with self.assertRaises(UserError):   # no editable
            inst._validate_config({"db_host": "x"})
        with self.assertRaises(UserError):   # enum inválido
            inst._validate_config({"log_level": "loud"})
        with self.assertRaises(UserError):   # coherencia soft/hard
            inst._validate_config(
                {"limit_memory_soft": "200", "limit_memory_hard": "100"})

    def test_config_read_parse_separa_y_omite(self):
        stdout = ("PCM_HASH:abc123\nPCM_VAL:proxy_mode=True\n"
                  "PCM_VAL:db_host=localhost\nPCM_VAL:limit_time_cpu=60\n")
        data = self.instance._parse_config_read(stdout)
        self.assertEqual(data["config_hash"], "abc123")
        self.assertEqual(data["editable"]["limit_time_cpu"], "60")
        self.assertEqual(data["readonly"]["db_host"], "localhost")
        self.assertNotIn("db_password", data["editable"])
        self.assertNotIn("db_password", data["readonly"])

    def test_save_config_prod_exige_nombre(self):
        self.instance.provisioned_by_pcm = True
        self.env_rec.env_type = "production"
        self.env_rec.name = "Prod X"
        prim = self.env_rec.primary_instance_id
        with self.assertRaises(UserError):
            self.instance.action_save_config(
                {"workers": "2"}, "h", typed_name="mal", odoo_instance=prim)
        # Nombre correcto → encola sin error (no corre el job).
        self.instance.action_save_config(
            {"workers": "2"}, "h", typed_name="Prod X", odoo_instance=prim)

    def test_job_save_config_ok_registra_diff(self):
        self.instance.provisioned_by_pcm = True
        apply_out = {"stdout": (
            "PCM_OLD:workers=0\nPCM_BAK:/etc/odoo/odoo.conf.pcm-bak-1\n"
            "PCM_HTTP_PORT:8069\nPCM_RESULT:applied\n"), "status": "Success"}
        patcher, fake = self._patch_ssm_routed({"config apply": apply_out})
        with patcher, mock.patch.object(
                type(self.instance), "_config_health_check", return_value="http"):
            self.instance.job_save_config({"workers": "2"}, "h", instance_id=self.env_rec.primary_instance_id.id)
        log = self._last_config_log()
        self.assertEqual(log.result, "success")
        self.assertIn("workers: 0 → 2", log.error_message)
        comments = [c.kwargs.get("comment", "")
                    for c in fake.run_script.call_args_list]
        self.assertTrue(any("restart" in c for c in comments))
        self.assertFalse(any("rollback" in c for c in comments))

    def test_job_save_config_stale_no_reinicia(self):
        self.instance.provisioned_by_pcm = True
        patcher, fake = self._patch_ssm_routed(
            {"config apply": {"stdout": "PCM_RESULT:stale\n", "status": "Success"}})
        with patcher:
            self.instance.job_save_config({"workers": "2"}, "viejo", instance_id=self.env_rec.primary_instance_id.id)
        self.assertEqual(self._last_config_log().result, "failed")
        comments = [c.kwargs.get("comment", "")
                    for c in fake.run_script.call_args_list]
        self.assertFalse(any("restart" in c for c in comments))  # NO reinició

    def test_job_save_config_rollback_si_no_levanta(self):
        self.instance.provisioned_by_pcm = True
        apply_out = {"stdout": (
            "PCM_OLD:workers=0\nPCM_BAK:/etc/odoo/odoo.conf.pcm-bak-1\n"
            "PCM_HTTP_PORT:8069\nPCM_RESULT:applied\n"), "status": "Success"}
        patcher, fake = self._patch_ssm_routed({"config apply": apply_out})
        with patcher, mock.patch.object(
                type(self.instance), "_config_health_check", return_value="failed"):
            self.instance.job_save_config({"workers": "2"}, "h", instance_id=self.env_rec.primary_instance_id.id)
        self.assertEqual(self._last_config_log().result, "failed")
        comments = [c.kwargs.get("comment", "")
                    for c in fake.run_script.call_args_list]
        self.assertTrue(any("rollback" in c for c in comments))  # restauró

    def test_config_health_check_espera_y_estados(self):
        """No marca caído demasiado rápido; distingue http/degraded/failed."""
        self.instance.provisioned_by_pcm = True
        slept = []
        _sleep = lambda s: slept.append(s)   # noqa: E731
        # (a) activo+http recién al 3er poll → 'http', esperó 2 veces.
        seq = [{"stdout": "PCM_ACTIVE:activating\nPCM_HTTP:no\n", "status": "S"},
               {"stdout": "PCM_ACTIVE:activating\nPCM_HTTP:no\n", "status": "S"},
               {"stdout": "PCM_ACTIVE:active\nPCM_HTTP:ok\n", "status": "S"}]
        with self._patch_ssm_routed({"config health": seq})[0]:
            self.assertEqual(
                self.instance._config_health_check("8069", _sleep=_sleep), "http")
        self.assertEqual(len(slept), 2)   # esperó, no marcó caído al primer no
        # (b) nunca activo → 'failed' tras agotar el timeout.
        with self._patch_ssm_routed(
                {"config health": {"stdout": "PCM_ACTIVE:failed\nPCM_HTTP:no\n",
                                   "status": "S"}})[0]:
            self.assertEqual(
                self.instance._config_health_check("8069", _sleep=_sleep), "failed")
        # (c) activo sin HTTP y NO servía antes (proxy/socket) → 'degraded'.
        with self._patch_ssm_routed(
                {"config health": {"stdout": "PCM_ACTIVE:active\nPCM_HTTP:no\n",
                                   "status": "S"}})[0]:
            self.assertEqual(
                self.instance._config_health_check(
                    "8069", http_was_ok=False, _sleep=_sleep), "degraded")
        # (d) activo sin HTTP pero SÍ servía antes → el cambio lo rompió →
        # 'failed' (dispara rollback; no se degrada un Odoo que quedó sin servir).
        with self._patch_ssm_routed(
                {"config health": {"stdout": "PCM_ACTIVE:active\nPCM_HTTP:no\n",
                                   "status": "S"}})[0]:
            self.assertEqual(
                self.instance._config_health_check(
                    "8069", http_was_ok=True, _sleep=_sleep), "failed")

    # --- Agregar addon a la instancia (Bloque B4) ---
    def test_addon_clone_script_token_seguro(self):
        import base64
        Ec2 = self.env["primate.cloud.ec2.instance"]
        tok_b64 = base64.b64encode(b"ghp_secreto").decode()
        script = Ec2._build_addon_clone_script(
            "https://x-access-token@github.com/o/r.git",
            "/opt/odoo/custom-addons/r", "19.0", token_b64=tok_b64)
        self.assertIn("GIT_ASKPASS", script)
        self.assertIn("trap", script)             # borra el token pase lo que pase
        self.assertNotIn("ghp_secreto", script)   # token NO en claro (solo base64)
        self.assertNotIn("ghp_secreto@", script)  # NUNCA en la URL
        pub = Ec2._build_addon_clone_script(
            "https://github.com/o/r.git", "/p", None)
        self.assertNotIn("GIT_ASKPASS", pub)      # público: sin askpass
        self.assertIn("git clone", pub)

    def test_config_internal_keys_permite_addons_path(self):
        inst = self.instance
        with self.assertRaises(UserError):
            inst._validate_config({"addons_path": "/x"})   # usuario: no editable
        clean = inst._validate_config(
            {"addons_path": "/a,/b"}, internal_keys={"addons_path"})
        self.assertEqual(clean["addons_path"], "/a,/b")

    def test_add_addon_crea_repo_no_verificado(self):
        repo = self.env_rec.add_addon(
            {"github_url": "https://github.com/oca/web"})
        self.assertEqual(repo.environment_id, self.env_rec)
        self.assertEqual(repo.sync_state, "unknown")     # sin verificar
        self.assertIn("custom-addons", repo.local_path)

    def test_job_add_addon_clone_falla_borra_repo(self):
        repo = self.env_rec.add_addon(
            {"github_url": "https://github.com/oca/web"})
        with mock.patch.object(type(self.instance), "clone_addon",
                               return_value=False):
            self.env_rec.job_add_addon(repo.id)
        self.assertFalse(repo.exists())   # ni registro ni clon (consistencia)

    def test_job_add_addon_ok_verifica(self):
        repo = self.env_rec.add_addon(
            {"github_url": "https://github.com/oca/web"})
        with mock.patch.object(type(self.instance), "clone_addon", return_value=True), \
             mock.patch.object(type(self.instance), "_ensure_custom_addons_path",
                               return_value="ready"), \
             mock.patch.object(type(self.instance), "restart_odoo"), \
             mock.patch.object(type(self.instance), "addon_module_count",
                               return_value=2):
            self.env_rec.job_add_addon(repo.id)
        self.assertEqual(repo.sync_state, "updated")   # verificado
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "addon_add")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")

    def test_job_add_addon_sin_modulos_no_verificado(self):
        repo = self.env_rec.add_addon(
            {"github_url": "https://github.com/oca/web"})
        with mock.patch.object(type(self.instance), "clone_addon", return_value=True), \
             mock.patch.object(type(self.instance), "_ensure_custom_addons_path",
                               return_value="restarted"), \
             mock.patch.object(type(self.instance), "addon_module_count",
                               return_value=0):
            self.env_rec.job_add_addon(repo.id)
        self.assertEqual(repo.sync_state, "unknown")   # registrado, no verificado
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "addon_add")], order="id desc", limit=1)
        self.assertEqual(log.result, "partial")

    def test_job_add_addon_addons_path_rollback_es_parcial(self):
        repo = self.env_rec.add_addon(
            {"github_url": "https://github.com/oca/web"})
        with mock.patch.object(type(self.instance), "clone_addon", return_value=True), \
             mock.patch.object(type(self.instance), "_ensure_custom_addons_path",
                               return_value="rolled_back"):
            self.env_rec.job_add_addon(repo.id)
        self.assertTrue(repo.exists())                 # queda registrado
        self.assertEqual(repo.sync_state, "unknown")   # pero no verificado
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "addon_add")], order="id desc", limit=1)
        self.assertEqual(log.result, "partial")

    # --- Login as / impersonación (Bloque B5) ---
    def _unb64url(self, s):
        import base64
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def test_impersonate_token_firma_y_verifica(self):
        # R4-B3: el token lo firma la clave de LA INSTANCIA (no la cuenta) y
        # lleva el claim instance_ref (su pcm_ref inmutable).
        import json
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        inst = self.env_rec.primary_instance_id
        token = inst._make_impersonate_token("demo", 7, "juan")
        payload_b64, sig_b64 = token.split(".")
        payload = json.loads(self._unb64url(payload_b64))
        self.assertEqual(payload["db"], "demo")
        self.assertEqual(payload["uid"], 7)
        self.assertEqual(payload["login"], "juan")
        self.assertEqual(payload["instance_ref"], inst.pcm_ref)
        self.assertIn("nonce", payload)
        self.assertIn("exp", payload)
        # La pública de la INSTANCIA valida la firma sobre el payload.
        pub = Ed25519PublicKey.from_public_bytes(
            __import__("base64").b64decode(inst.impersonate_public_key()))
        pub.verify(self._unb64url(sig_b64), self._unb64url(payload_b64))  # no raise
        with self.assertRaises(Exception):   # payload alterado → firma inválida
            pub.verify(self._unb64url(sig_b64),
                       self._unb64url(payload_b64) + b"x")

    def test_login_as_gating_y_auditoria(self):
        inst = self.instance
        inst.public_ip = "1.2.3.4"
        admin_grp = self.env.ref("primate_cloud_manager.group_cloud_admin")
        # Sin grupo admin cloud → rechaza.
        admin_grp.write({"user_ids": [(3, self.env.user.id)]})
        with self.assertRaises(UserError):
            inst.action_login_as("demo", 7, "juan", odoo_instance=self.env_rec.primary_instance_id)
        admin_grp.write({"user_ids": [(4, self.env.user.id)]})
        # Prod exige el nombre exacto.
        self.env_rec.env_type = "production"
        self.env_rec.name = "Prod Z"
        with self.assertRaises(UserError):
            inst.action_login_as("demo", 7, "juan", typed_name="mal", odoo_instance=self.env_rec.primary_instance_id)
        # Destino admin sin ack → rechaza (nunca un click más).
        self.env_rec.env_type = "development"
        with self.assertRaises(UserError):
            inst.action_login_as("demo", 7, "juan",
                                 is_admin_target=True, admin_ack=False,
                                 odoo_instance=self.env_rec.primary_instance_id)
        # Camino normal → URL + auditoría (quién→a quién).
        res = inst.action_login_as("demo", 7, "juan", odoo_instance=self.env_rec.primary_instance_id)
        self.assertIn("/pcm/impersonate?token=", res["url"])
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "impersonate")], order="id desc", limit=1)
        self.assertEqual(log.result, "success")
        self.assertIn("juan", log.error_message)

    def test_login_as_admin_target_marca_auditoria(self):
        inst = self.instance
        inst.public_ip = "1.2.3.4"
        admin_grp = self.env.ref("primate_cloud_manager.group_cloud_admin")
        admin_grp.write({"user_ids": [(4, self.env.user.id)]})
        self.env_rec.env_type = "development"
        inst.action_login_as("demo", 2, "admin",
                             is_admin_target=True, admin_ack=True,
                             odoo_instance=self.env_rec.primary_instance_id)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "impersonate")], order="id desc", limit=1)
        self.assertIn("ADMIN", log.error_message)   # marca distinta en auditoría

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
