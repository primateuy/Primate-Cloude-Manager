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
        # Config y addons se recablearon (y levantaron su guard) en R4-B2:
        # sus inversos viven en TestR4B2. Acá quedan los aún bloqueados.
        server, machine = self._server("g-int", multi=True)
        server.backup_policy_id = self.policy
        with self.assertRaises(UserError):
            server.action_run_backup()
        with self.assertRaises(UserError):
            server._enqueue_staging({"name": "stg"})
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


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B2(TransactionCase):
    """Panel-ops por instancia (logs/config/addons) + guards LEVANTADOS.

    El patrón que marca esta clase para los bloques siguientes: al recablear
    un flujo, su guard se quita EN EL MISMO cambio y el test inverso fija que
    el flujo ya NO se bloquea en multi (opera con las rutas de la instancia).
    """

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente", "account_id": self.account.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Multi", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b2", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        # Primaria MATERIALIZADA al layout multi: el panel debe operar SUS rutas.
        self.prim = self.server.primary_instance_id
        self.prim.write({"slug": "cliente-a", "state": "active"})
        self.server._materialize_multiodoo_layout(
            self.prim, {"db_mode": "local_pg"})

    def _patch_ssm(self, reply):
        fake = mock.Mock()
        fake.run_script.return_value = dict(reply)
        from odoo.addons.primate_cloud_manager.models import (
            primate_cloud_ec2_instance as ec2_mod,
        )
        patcher = mock.patch.object(
            type(self.machine), "_get_ssm_service", return_value=fake)
        return patcher, fake, ec2_mod

    # --- guards LEVANTADOS (inversos del patrón de B1) -----------------
    def test_guard_config_y_addons_levantados(self):
        self.assertNotIn("config",
                         type(self.server).LEGACY_FLOW_UNBLOCKED_IN)
        self.assertNotIn("addons",
                         type(self.server).LEGACY_FLOW_UNBLOCKED_IN)
        # Los que siguen sin recablear siguen bloqueando.
        self.assertTrue(self.server._legacy_flow_blocked_reason("restore"))

    def test_config_en_multi_ya_no_se_bloquea_y_encola(self):
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.machine), "with_delay",
                               return_value=fake_delay):
            # typed_name: el entorno es producción (fricción B3, se mantiene).
            self.machine.action_save_config({"workers": "2"}, "hash-x",
                                            typed_name=self.server.name)
        args, kwargs = fake_delay.job_save_config.call_args
        self.assertEqual(kwargs["instance_id"], self.prim.id)

    def test_addons_en_multi_ya_no_se_bloquea(self):
        with mock.patch.object(type(self.server), "with_delay"):
            repo = self.server.add_addon(
                {"github_url": "https://github.com/x/wms"})
        self.assertEqual(repo.local_path,
                         "/opt/pcm/instances/cliente-a/addons/wms")
        self.assertEqual(repo.instance_id, self.prim)

    # --- logs por instancia --------------------------------------------
    def test_fetch_logs_odoo_usa_log_path_y_unit_propios(self):
        patcher, fake, _m = self._patch_ssm(
            {"stdout": "linea", "status": "Success"})
        with patcher:
            self.prim.fetch_logs("odoo")
        cmd = fake.run_script.call_args[0][1]
        self.assertIn("/opt/pcm/instances/cliente-a/log/odoo.log", cmd)
        self.assertIn("journalctl -u odoo-cliente-a", cmd)
        self.assertNotIn("/var/log/odoo/odoo.log", cmd)

    def test_fetch_logs_stream_odoo_por_su_archivo(self):
        patcher, fake, _m = self._patch_ssm(
            {"stdout": "x\nPCM_OFFSET:10\n", "status": "Success"})
        with patcher:
            res = self.prim.fetch_logs_stream("odoo")
        self.assertEqual(res["cursor"], "10")
        self.assertIn("/opt/pcm/instances/cliente-a/log/odoo.log",
                      fake.run_script.call_args[0][1])

    def test_stream_offset_detecta_rotacion(self):
        # El comando compara tamaño actual vs offset guardado y, si el archivo
        # achicó (rotó), lee desde 0 y marca PCM_ROTATED. R3 instala logrotate
        # por slug → rota seguro; sin esto el stream quedaría mudo tras la
        # rotación de madrugada.
        cmd = self.machine._file_stream_cmd(
            "/opt/pcm/instances/cliente-a/log/odoo.log",
            from_cursor="5000", lines=200, grep=None)
        self.assertIn('if [ "$SZ" -lt 5000 ]', cmd)
        self.assertIn("PCM_ROTATED:1", cmd)
        self.assertIn("tail -c +1 ", cmd)     # rama rotó: desde el principio
        self.assertIn("tail -c +5001 ", cmd)  # rama normal: desde el offset
        # Primer poll (sin cursor) NO trae la lógica de rotación.
        primer = self.machine._file_stream_cmd(
            "/x/y.log", from_cursor=False, lines=50, grep=None)
        self.assertNotIn("PCM_ROTATED", primer)

    def test_parse_stream_rotacion_resetea_y_avisa(self):
        rotado = self.machine._parse_file_stream(
            "PCM_ROTATED:1\nprimera linea del archivo nuevo\nPCM_OFFSET:37\n",
            "Success")
        self.assertTrue(rotado["rotated"])
        self.assertEqual(rotado["cursor"], "37")   # offset ya reseteado al nuevo
        self.assertIn("primera linea", rotado["text"])
        self.assertNotIn("PCM_ROTATED", rotado["text"])
        # Sin rotación: rotated False.
        normal = self.machine._parse_file_stream(
            "linea\nPCM_OFFSET:99\n", "Success")
        self.assertFalse(normal["rotated"])

    # --- config por instancia -------------------------------------------
    def test_fetch_config_renderiza_el_conf_de_la_instancia(self):
        patcher, fake, _m = self._patch_ssm(
            {"stdout": "PCM_HASH:h\n", "status": "Success"})
        with patcher:
            self.prim.fetch_config()
        script = fake.run_script.call_args[0][1]
        self.assertIn('CONF = "/etc/odoo/cliente-a.conf"', script)
        self.assertNotIn('"/etc/odoo/odoo.conf"', script)

    def test_config_rollback_restaura_el_conf_de_la_instancia(self):
        # EL caso que motivó el guard: el rollback pisaba SIEMPRE el conf
        # legacy. Ahora restaura el de la instancia y reinicia SU unit.
        patcher, fake, _m = self._patch_ssm(
            {"stdout": "", "status": "Success"})
        with patcher:
            self.machine._config_rollback("/etc/odoo/cliente-a.conf.pcm-bak",
                                          self.prim)
        cmd = fake.run_script.call_args[0][1]
        self.assertIn("mv /etc/odoo/cliente-a.conf.pcm-bak "
                      "/etc/odoo/cliente-a.conf", cmd)
        self.assertIn("systemctl restart odoo-cliente-a", cmd)
        self.assertNotIn("/etc/odoo/odoo.conf", cmd)

    def test_config_health_chequea_unit_y_puerto_propios(self):
        patcher, fake, _m = self._patch_ssm(
            {"stdout": "PCM_ACTIVE:active\nPCM_HTTP:ok\n",
             "status": "Success"})
        with patcher:
            health = self.machine._config_health_check(
                str(self.prim.http_port), service=self.prim.service_name)
        self.assertEqual(health, "http")
        cmd = fake.run_script.call_args[0][1]
        self.assertIn("is-active odoo-cliente-a", cmd)
        self.assertIn("127.0.0.1:%d" % self.prim.http_port, cmd)
        self.assertNotIn("is-active odoo ", cmd)

    # --- addons por instancia --------------------------------------------
    def test_clone_script_usa_addons_dir_de_la_instancia(self):
        script = type(self.machine)._build_addon_clone_script(
            "https://github.com/x/y", "/opt/pcm/instances/cliente-a/addons/y",
            None, addons_dir="/opt/pcm/instances/cliente-a/addons")
        self.assertIn("mkdir -p /opt/pcm/instances/cliente-a/addons", script)
        self.assertNotIn("/opt/odoo/custom-addons", script)

    def test_ensure_addons_path_multi_ya_esta_ready(self):
        # En multi el conf del slug YA trae su dir (R3): 'ready', sin editar.
        patcher, fake, _m = self._patch_ssm({"stdout": "", "status": "Success"})
        cfg = {"readonly": {"addons_path":
                            "/opt/pcm/runtime/odoo-19/src/addons,"
                            "/opt/pcm/instances/cliente-a/addons"},
               "config_hash": "h"}
        with patcher, mock.patch.object(type(self.machine), "fetch_config",
                                        return_value=cfg):
            result = self.machine._ensure_custom_addons_path(
                odoo_instance=self.prim)
        self.assertEqual(result, "ready")
        mkdir_cmd = fake.run_script.call_args[0][1]
        self.assertIn("/opt/pcm/instances/cliente-a/addons", mkdir_cmd)

    # --- probe por instancia ----------------------------------------------
    def test_runtime_probe_por_instancia(self):
        cmd = self.machine._runtime_probe_cmd()
        self.assertIn("/opt/pcm/runtime/odoo-19/venv/bin/python3", cmd)
        self.assertIn("/opt/pcm/runtime/odoo-19/src/odoo-bin", cmd)
        self.assertIn("/etc/odoo/cliente-a.conf", cmd)
        self.assertNotIn("/opt/odoo/", cmd)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B3Impersonate(TransactionCase):
    """Impersonación multi-instancia: claves POR INSTANCIA + claim + allowlist
    por vhost propio + guard levantado. El caso cruzado en vivo va en B7."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.proj_a = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id})
        self.proj_b = self.env["primate.cloud.project"].create(
            {"name": "Cliente B", "account_id": self.account.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Compartido", "project_id": self.proj_a.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b3", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True, "public_ip": "1.2.3.4",
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "state": "active",
                           "main_url": "a.pcm.test"})
        self.server._materialize_multiodoo_layout(
            self.inst_a, {"db_mode": "local_pg"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.proj_b.id, "slug": "cliente-b",
            "odoo_version": "19", "state": "active", "main_url": "b.pcm.test",
        })
        self.server._materialize_multiodoo_layout(
            self.inst_b, {"db_mode": "local_pg"})

    # --- claves POR INSTANCIA (el corazón del aislamiento) --------------
    def test_claves_por_instancia_y_no_en_create(self):
        # D-R4.9: nacen sin par (no en create).
        self.assertFalse(self.inst_a.impersonate_privkey_encrypted)
        self.assertFalse(self.inst_b.impersonate_pubkey)
        # Se generan al pedirlas (primer deploy/enable).
        pub_a = self.inst_a.impersonate_public_key()
        pub_b = self.inst_b.impersonate_public_key()
        self.assertTrue(pub_a and pub_b)
        # Pares DISTINTOS por instancia (la base del cruzado imposible).
        self.assertNotEqual(pub_a, pub_b)
        self.assertNotEqual(self.inst_a.impersonate_privkey_encrypted,
                            self.inst_b.impersonate_privkey_encrypted)
        # copy=False: un duplicado no hereda la clave.
        copia = self.inst_a.copy({"slug": "cliente-a-copia", "name": "copia",
                                  "http_port": 8099, "gevent_port": 8102})
        self.assertFalse(copia.impersonate_privkey_encrypted)
        self.assertFalse(copia.impersonate_pubkey)

    def test_token_de_A_no_verifica_con_la_publica_de_B(self):
        # EL invariante del cruzado, verificado criptográficamente (sin red):
        # un token firmado por A NO valida contra la pública de B.
        import base64 as b64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        token = self.inst_a._make_impersonate_token("db_a", 3, "juan")
        payload_b64, sig_b64 = token.split(".")

        def unb64(s):
            return b64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        # Contra la pública de A: valida.
        pub_a = Ed25519PublicKey.from_public_bytes(
            b64.b64decode(self.inst_a.impersonate_public_key()))
        pub_a.verify(unb64(sig_b64), unb64(payload_b64))
        # Contra la pública de B: rompe.
        pub_b = Ed25519PublicKey.from_public_bytes(
            b64.b64decode(self.inst_b.impersonate_public_key()))
        with self.assertRaises(Exception):
            pub_b.verify(unb64(sig_b64), unb64(payload_b64))
        # Y el claim declara la instancia dueña (defensa en profundidad).
        import json
        payload = json.loads(unb64(payload_b64))
        self.assertEqual(payload["instance_ref"], self.inst_a.pcm_ref)
        self.assertNotEqual(payload["instance_ref"], self.inst_b.pcm_ref)

    def test_base_url_por_instancia(self):
        self.assertEqual(self.inst_a._impersonate_base_url(),
                         "https://a.pcm.test")
        self.assertEqual(self.inst_b._impersonate_base_url(),
                         "https://b.pcm.test")

    # --- guard levantado (inverso del patrón) --------------------------
    def test_guard_impersonate_levantado(self):
        self.assertNotIn("impersonate",
                         type(self.server).LEGACY_FLOW_UNBLOCKED_IN)

    def _patch_ssm(self):
        fake = mock.Mock()
        fake.run_script.return_value = {"stdout": "PCM_NGINX:ok",
                                        "status": "Success"}
        return mock.patch.object(type(self.machine), "_get_ssm_service",
                                 return_value=fake), fake

    def test_deploy_por_instancia_addons_runtime_pubkey_y_ref(self):
        patcher, fake = self._patch_ssm()
        with patcher, mock.patch.object(
                type(self.machine), "_ensure_custom_addons_path"), \
                mock.patch.object(type(self.machine), "restart_odoo"):
            self.inst_b.deploy_impersonate(["db_b"])
        scripts = "\n---\n".join(
            c[0][1] for c in fake.run_script.call_args_list)
        # Runtime + addons_dir + unit del SLUG de B (no legacy, no de A).
        self.assertIn("/opt/pcm/instances/cliente-b/addons", scripts)
        self.assertIn("/opt/pcm/runtime/odoo-19/src/odoo-bin", scripts)
        self.assertNotIn("cliente-a", scripts)
        self.assertNotIn("/opt/odoo/", scripts)
        # Escribió la pública de B y su instance_ref.
        self.assertIn("public_key", scripts)
        self.assertIn("instance_ref", scripts)
        self.assertTrue(self.inst_b.impersonate_pubkey)

    def test_nginx_allowlist_camino_feliz_y_puerto_propio(self):
        patcher, fake = self._patch_ssm()  # stdout PCM_NGINX:ok
        with patcher:
            res = self.machine._nginx_allowlist("9.9.9.9", self.inst_b)
        self.assertEqual(res, "fixed")
        script = fake.run_script.call_args[0][1]
        self.assertIn("PORT = '%d'" % self.inst_b.http_port, script)
        self.assertNotIn("8069", script)

    def test_nginx_allowlist_novhost_falla_ruidoso(self):
        # BUG 1: si el needle no matchea ningún vhost, NO se despliega el
        # endpoint sin allowlist en silencio — falla ruidoso.
        fake = mock.Mock()
        fake.run_script.return_value = {"stdout": "PCM_NGINX:novhost",
                                        "status": "Success"}
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            with self.assertRaises(UserError) as ctx:
                self.machine._nginx_allowlist("9.9.9.9", self.inst_b)
        self.assertIn("allow-list", str(ctx.exception))

    def test_nginx_allowlist_deploy_aborta_si_no_hay_vhost(self):
        # El fallo del allowlist ABORTA el deploy (no queda endpoint sin la
        # 2ª capa): _nginx_allowlist real (no mockeado) sobre stdout novhost.
        fake = mock.Mock()

        def _reply(instance_id, script, **kw):
            if "glob.glob" in script:   # el script del allowlist
                return {"stdout": "PCM_NGINX:novhost", "status": "Success"}
            return {"stdout": "", "status": "Success"}
        fake.run_script.side_effect = _reply
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake), \
                mock.patch.object(type(self.machine),
                                  "_ensure_custom_addons_path"), \
                mock.patch.object(type(self.machine), "restart_odoo") as restart:
            with self.assertRaises(UserError):
                self.inst_b.deploy_impersonate(["db_b"], pcm_ip="9.9.9.9")
        restart.assert_not_called()   # abortó antes del restart

    # --- BUG 2: el transform PURO (fuente única del script remoto) ------
    def _apply(self, content, ip, port):
        ns = {}
        exec(type(self.machine)._NGINX_APPLY_SRC, ns)
        return ns["pcm_apply"](content, ip, str(port))

    def _vhost(self, port, extra=""):
        return ("server {\n    server_name x;\n%s"
                "    location / {\n        proxy_pass http://127.0.0.1:%d;\n"
                "    }\n}\n" % (extra, port))

    def test_transform_skip_added_ok(self):
        # skip: no es el vhost de esta instancia.
        _c, act = self._apply(self._vhost(8079), "9.9.9.9", 8089)
        self.assertEqual(act, "skip")
        # added: es su vhost y no tenía bloque.
        nuevo, act = self._apply(self._vhost(8089), "9.9.9.9", 8089)
        self.assertEqual(act, "added")
        self.assertIn("location = /pcm/impersonate", nuevo)
        self.assertIn("proxy_pass http://127.0.0.1:8089;", nuevo)
        # ok: idempotente (ya tiene el bloque correcto).
        _c2, act = self._apply(nuevo, "9.9.9.9", 8089)
        self.assertEqual(act, "ok")

    def test_transform_reescribe_bloque_viejo_mal_apuntado(self):
        # BUG 2: deploy pre-B3 dejó el bloque de ESTE vhost (8089) apuntando a
        # 8069 → NO se saltea, se REESCRIBE al puerto correcto.
        viejo = self._apply(self._vhost(8089), "9.9.9.9", 8069)[0]  # mal: 8069
        # (simulo el escenario: el vhost de B con un bloque que va a 8069)
        vhost_b_con_bloque_malo = self._vhost(8089).replace(
            "    location / {",
            "    location = /pcm/impersonate {\n"
            "        allow 9.9.9.9;\n        deny all;\n"
            "        proxy_pass http://127.0.0.1:8069;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n    }\n\n"
            "    location / {", 1)
        fijado, act = self._apply(vhost_b_con_bloque_malo, "9.9.9.9", 8089)
        self.assertEqual(act, "fixed")
        self.assertIn("proxy_pass http://127.0.0.1:8089;", fijado)
        self.assertNotIn("127.0.0.1:8069", fijado)   # el puerto ajeno se fue
        self.assertEqual(fijado.count("location = /pcm/impersonate"), 1)

    def test_transform_reescribe_si_cambia_la_ip(self):
        # Mismo puerto pero IP distinta (rotó la IP de egreso de PCM) → fixed.
        con_ip_vieja = self._apply(self._vhost(8089), "1.1.1.1", 8089)[0]
        fijado, act = self._apply(con_ip_vieja, "9.9.9.9", 8089)
        self.assertEqual(act, "fixed")
        self.assertIn("allow 9.9.9.9;", fijado)
        self.assertNotIn("allow 1.1.1.1;", fijado)

    def test_enable_y_killswitch_por_instancia(self):
        patcher, fake = self._patch_ssm()
        with patcher:
            self.inst_a.set_impersonate_enabled(["db_a"], False)
        script = fake.run_script.call_args[0][1]
        # Deshabilitar sube el epoch (kill-switch) por el runtime de A.
        self.assertIn("pcm.impersonate.epoch", script)
        self.assertIn("/etc/odoo/cliente-a.conf", script)
        self.assertNotIn("cliente-b", script)

    def test_login_as_audita_sobre_la_instancia(self):
        admin_grp = self.env.ref("primate_cloud_manager.group_cloud_admin")
        admin_grp.write({"user_ids": [(4, self.env.user.id)]})
        # prod exige nombre exacto (fricción intacta).
        with self.assertRaises(UserError):
            self.inst_b.action_login_as("db_b", 5, "ana", typed_name="mal")
        res = self.inst_b.action_login_as("db_b", 5, "ana",
                                          typed_name=self.server.name)
        self.assertIn("https://b.pcm.test/pcm/impersonate?token=", res["url"])
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "impersonate"),
             ("resource_id", "=", self.inst_b.id),
             ("resource_model", "=", "primate.cloud.instance")],
            order="id desc", limit=1)
        self.assertTrue(log)
        self.assertIn("ana", log.error_message)
