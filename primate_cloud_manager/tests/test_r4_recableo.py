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

from odoo.addons.primate_cloud_manager.services import aws_s3, aws_ssm


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B1(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4 A"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id, "partner_id": self.partner.id})
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

    def test_todos_los_guards_levantados(self):
        # R4-B1..B5 recablearon todos los flujos legacy-destructivos: el dict
        # de guards quedó vacío (la maquinaria se conserva por si un flujo
        # futuro la necesita). El inverso de cada flujo vive en su clase.
        self.assertEqual(type(self.env["primate.cloud.environment"])
                         .LEGACY_FLOW_UNBLOCKED_IN, {})

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
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente", "account_id": self.account.id, "partner_id": self.partner.id})
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
                                            typed_name=self.server.name,
                                            odoo_instance=self.prim)
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
        cmd = self.machine._runtime_probe_cmd(self.prim)
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
        self.partner_a = self.env["res.partner"].create({"name": "Cliente Test R4 A"})
        self.partner_b = self.env["res.partner"].create({"name": "Cliente Test R4 B"})
        self.proj_a = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id, "partner_id": self.partner_a.id})
        self.proj_b = self.env["primate.cloud.project"].create(
            {"name": "Cliente B", "account_id": self.account.id, "partner_id": self.partner_b.id})
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
        # stdout con TODOS los centinelas: install verificado, enable/kill-switch
        # verificado y allowlist nginx. Todos los parseos usan ``in``.
        fake.run_script.return_value = {
            "stdout": "PCM_IMP_INSTALL_OK\nPCM_IMP_ENABLE_OK\nPCM_NGINX:ok",
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
        # --no-http en el -i (hallazgo E2E R4-B7): sin esto choca con el puerto
        # de la unit corriendo (Address already in use) y el módulo NO instala.
        self.assertIn("-i pcm_impersonate --stop-after-init --no-http", scripts)
        # Detiene la unit del target ANTES del -i (2º hallazgo E2E: sin liberar
        # su RAM, el 3er proceso Odoo del install OOM-ea en instancias chicas).
        stop_idx = scripts.find("systemctl stop odoo-cliente-b")
        install_idx = scripts.find("-i pcm_impersonate")
        self.assertTrue(0 <= stop_idx < install_idx,
                        "el stop de la unit debe ir ANTES del -i")
        # El install corre SIN heredar el límite de memoria de la instancia
        # (3er hallazgo E2E: limit_memory_hard de prod → RLIMIT_AS → MemoryError).
        self.assertIn("--limit-memory-hard=0", scripts)
        self.assertIn("--workers=0", scripts)

    def test_deploy_aborta_ruidoso_si_install_falla(self):
        # HALLAZGO CENTRAL R4-B7: run_script NO levanta ante exit!=0. Si el -i
        # del companion FALLA (status Failed / sin PCM_IMP_INSTALL_OK), el deploy
        # debe ABORTAR RUIDOSO — NO tragarse el fallo, NO escribir params (que
        # quedarían HUÉRFANOS y harían chocar toda reinstalación futura con
        # UniqueViolation), NO reportar éxito.
        fake = mock.Mock()

        def _reply(instance_id, script, **kw):
            if "-i pcm_impersonate" in script:      # el install: FALLA
                return {"stdout": "", "stderr": "boom", "status": "Failed"}
            return {"stdout": "PCM_NGINX:ok", "status": "Success"}
        fake.run_script.side_effect = _reply
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake), \
                mock.patch.object(type(self.machine),
                                  "_ensure_custom_addons_path"), \
                mock.patch.object(type(self.machine), "restart_odoo") as restart:
            with self.assertRaises(UserError) as ctx:
                self.inst_b.deploy_impersonate(["db_b"])
        self.assertIn("pcm_impersonate", str(ctx.exception))
        # No escribió NINGÚN parámetro tras el install fallido.
        ejecutados = "\n".join(c[0][1] for c in fake.run_script.call_args_list)
        self.assertNotIn("public_key", ejecutados)
        self.assertNotIn("instance_ref", ejecutados)
        restart.assert_not_called()   # abortó antes del restart

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
        # El script relee y confirma el valor grabado + imprime el centinela:
        # el kill-switch no puede reportar éxito sin haber cortado de verdad.
        self.assertIn("PCM_IMP_ENABLE_OK", script)
        self.assertIn("assert p.get_param('pcm.impersonate.enabled')", script)

    def test_killswitch_falla_ruidoso_si_no_confirma(self):
        # EL PEOR FALLO SILENCIOSO del barrido de run_script: un disable del
        # kill-switch que FALLA (status Failed / sin PCM_IMP_ENABLE_OK) NO puede
        # reportar éxito — dejaría la impersonación VIVA con el operador creyendo
        # que la cortó. Debe LEVANTAR.
        for stdout, status in (("", "Failed"),          # SSM falló
                               ("", "Success")):         # corrió pero sin confirmar
            fake = mock.Mock()
            fake.run_script.return_value = {"stdout": stdout, "status": status}
            with mock.patch.object(type(self.machine), "_get_ssm_service",
                                   return_value=fake):
                with self.assertRaises(UserError) as ctx:
                    self.inst_a.set_impersonate_enabled(["db_a"], False)
            self.assertIn("DESHABILITAR", str(ctx.exception))

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


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B4Backups(TransactionCase):
    """Backups + restore POR INSTANCIA + guards backup/restore levantados.
    El restore multi resuelve la instancia Odoo destino INEQUÍVOCA (la
    salvaguarda que reemplaza al guard del peor caso)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner_a = self.env["res.partner"].create({"name": "Cliente Test R4 A"})
        self.partner_b = self.env["res.partner"].create({"name": "Cliente Test R4 B"})
        self.proj_a = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id, "partner_id": self.partner_a.id})
        self.proj_b = self.env["primate.cloud.project"].create(
            {"name": "Cliente B", "account_id": self.account.id, "partner_id": self.partner_b.id})
        self.policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Diaria", "expected_frequency": "daily",
            "managed_by_pcm": True, "s3_bucket": "pcm-bucket",
            "expected_retention_days": 7,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Multi", "project_id": self.proj_a.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community", "backup_policy_id": self.policy.id,
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b4", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "state": "active"})
        self.server._materialize_multiodoo_layout(
            self.inst_a, {"db_mode": "local_pg"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.proj_b.id, "slug": "cliente-b",
            "odoo_version": "19", "state": "active"})
        self.server._materialize_multiodoo_layout(
            self.inst_b, {"db_mode": "local_pg"})
        self.db_a = self.env["primate.cloud.database"].create({
            "name": "db_a", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "instance_id": self.inst_a.id, "ec2_instance_id": self.machine.id})
        self.db_b = self.env["primate.cloud.database"].create({
            "name": "db_b", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "instance_id": self.inst_b.id, "ec2_instance_id": self.machine.id})

    # --- guards levantados --------------------------------------------
    def test_guards_backup_restore_levantados(self):
        d = type(self.server).LEGACY_FLOW_UNBLOCKED_IN
        self.assertNotIn("backup", d)
        self.assertNotIn("restore", d)

    # --- backup: filestore y conf por instancia -----------------------
    def test_backup_paths_por_instancia(self):
        conf_a, fs_a = self.server._backup_instance_paths(self.db_a)
        self.assertEqual(conf_a, "/etc/odoo/cliente-a.conf")
        self.assertEqual(fs_a, "/opt/pcm/instances/cliente-a/data/filestore")
        conf_b, fs_b = self.server._backup_instance_paths(self.db_b)
        self.assertEqual(conf_b, "/etc/odoo/cliente-b.conf")
        self.assertEqual(fs_b, "/opt/pcm/instances/cliente-b/data/filestore")

    def test_backup_script_usa_filestore_del_slug(self):
        script = self.server._build_backup_script(
            "db_b", "pcm-bucket", "k.dump", "kf.tar.gz",
            conf_path="/etc/odoo/cliente-b.conf",
            filestore_base="/opt/pcm/instances/cliente-b/data/filestore")
        self.assertIn("/opt/pcm/instances/cliente-b/data/filestore", script)
        self.assertNotIn("/opt/odoo/.local", script)

    def test_backup_rds_lee_conf_de_la_instancia(self):
        script = self.server._build_backup_script(
            "db_b", "pcm-bucket", "k", "kf", rds_endpoint="x.rds.aws",
            conf_path="/etc/odoo/cliente-b.conf",
            filestore_base="/opt/pcm/instances/cliente-b/data/filestore")
        self.assertIn("/etc/odoo/cliente-b.conf", script)
        self.assertNotIn("/etc/odoo/odoo.conf", script)

    def test_backup_gestionado_en_multi_ya_no_se_bloquea(self):
        # Antes bloqueaba; ahora corre y respalda cada base con SU filestore.
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success",
            "stdout": "PCM_DUMP_SIZE_BYTES=10\nPCM_FS_SIZE_BYTES=5\nPCM_BACKUP_OK"}
        with mock.patch.object(aws_s3.AwsS3Service, "ensure_bucket"), \
                mock.patch.object(aws_s3.AwsS3Service, "put_lifecycle_rule"), \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake):
            ok = self.server.job_run_backup()
        self.assertTrue(ok)
        scripts = [c[0][1] for c in fake.run_script.call_args_list]
        joined = "\n".join(scripts)
        # Cada base respaldada con el filestore de SU slug.
        self.assertIn("/opt/pcm/instances/cliente-a/data/filestore", joined)
        self.assertIn("/opt/pcm/instances/cliente-b/data/filestore", joined)

    # --- restore: unit/pg_user/filestore por instancia ----------------
    def _backup_rec(self, db):
        rec = self.env["primate.cloud.backup"].create({
            "name": "bk", "environment_id": self.server.id,
            "database_id": db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-bucket", "s3_key": "k.dump",
            "s3_filestore_key": "kf.tar.gz", "size_mb": 5})
        rec.write({"state": "completed"})
        return rec

    def test_restore_script_para_la_unit_y_filestore_del_destino(self):
        script = self.server._build_backup_restore_script(
            "db_b", self._backup_rec(self.db_b), True, self.inst_b)
        self.assertIn("systemctl stop odoo-cliente-b", script)
        self.assertIn("systemctl start odoo-cliente-b", script)
        self.assertIn("createdb -O odoo_cliente_b", script)
        self.assertIn("/opt/pcm/instances/cliente-b/data/filestore/db_b", script)
        # NUNCA la unit ni el filestore de A.
        self.assertNotIn("cliente-a", script)
        self.assertNotIn("stop odoo\n", script)

    def test_resolve_restore_instance(self):
        # explícita gana.
        self.assertEqual(
            self.server._resolve_restore_instance(self.inst_b.id), self.inst_b)
        # por la BD destino.
        self.assertEqual(
            self.server._resolve_restore_instance(None, target_db=self.db_a),
            self.inst_a)
        # ambiguo sin pista → vacío (el job falla claro).
        self.assertFalse(
            self.server._resolve_restore_instance(None, target_db=None))

    def test_restore_multi_sin_instancia_resoluble_falla_claro(self):
        # Server multi, db_name nuevo, sin odoo_instance_id → no adivina.
        rec = self._backup_rec(self.db_a)
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}):
            ok = self.server.job_restore_backup({
                "backup_id": rec.id, "db_name": "nueva_db",
                "instance_id": self.machine.id, "pre_backup": False})
        self.assertFalse(ok)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"), ("result", "=", "failed")],
            limit=1, order="id desc")
        self.assertIn("inequívoca", log.error_message)

    def test_restore_multi_con_instancia_explicita_corre(self):
        rec = self._backup_rec(self.db_b)
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success", "stdout": "PCM_DROP_STARTED\nPCM_RESTORE_OK"}
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}), \
                mock.patch.object(type(self.server), "_staging_neutralize"), \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake):
            ok = self.server.job_restore_backup({
                "backup_id": rec.id, "db_name": "db_b",
                "instance_id": self.machine.id, "pre_backup": False,
                "odoo_instance_id": self.inst_b.id})
        self.assertTrue(ok)
        script = fake.run_script.call_args[0][1]
        self.assertIn("systemctl stop odoo-cliente-b", script)

    def test_backup_base_sin_instance_id_en_multi_falla_claro(self):
        # BUG (revisión del usuario): el fallback a primaria haría que el
        # backup de una base sin instance_id tome el filestore de A. En multi
        # → falla claro (registro failed), NUNCA cae a la primaria.
        db_suelta = self.env["primate.cloud.database"].create({
            "name": "db_suelta", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id})
        db_suelta.instance_id = False   # explícitamente sin instancia
        # _backup_instance_paths se niega a adivinar.
        with self.assertRaises(UserError):
            self.server._backup_instance_paths(db_suelta)
        # Y el job marca ESE backup failed sin tumbar el resto ni tocar SSM
        # con paths ajenos.
        rec = self.server._run_database_backup(
            db_suelta, self.policy, "pcm-bucket", "p", "us-east-1")
        self.assertEqual(rec.state, "failed")
        self.assertIn("otro cliente", rec.error_message)

    def test_backup_instance_paths_legacy_puro_si_cae_a_primaria(self):
        # En legacy PURO (una sola instancia) el fallback a primaria es seguro.
        legacy = self.env["primate.cloud.environment"].create({
            "name": "Legacy", "project_id": self.proj_a.id,
            "env_type": "production", "odoo_version": "19"})
        machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "ml", "account_id": self.account.id,
            "aws_instance_id": "i-leg", "instance_state": "running",
            "region": "us-east-1", "environment_id": legacy.id})
        legacy.ec2_instance_id = machine
        db = self.env["primate.cloud.database"].create({
            "name": "leg_db", "account_id": self.account.id,
            "environment_id": legacy.id, "db_type": "local_pg"})
        db.instance_id = False
        conf, fs = legacy._backup_instance_paths(db)
        self.assertEqual(conf, "/etc/odoo/odoo.conf")
        self.assertEqual(fs, "/opt/odoo/.local/share/Odoo/filestore")

    def test_build_backup_script_exige_paths(self):
        # BUG #2 (trampa workers=0): sin conf_path/filestore_base la firma
        # revienta — no filtra paths legacy en silencio.
        with self.assertRaises(TypeError):
            self.server._build_backup_script("db", "b", "k", "kf")

    def test_pre_backup_del_restore_resuelve_paths_por_instancia(self):
        # #3 (revisión del usuario): el pre-backup es la RED de seguridad del
        # restore; si tomara el filestore equivocado, la recuperación
        # restauraría datos de otro cliente. Con pre_backup=True el ejecutor
        # (reuso de _run_database_backup) usa el filestore del SLUG destino.
        rec = self._backup_rec(self.db_b)
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success",
            "stdout": ("PCM_DUMP_SIZE_BYTES=1\nPCM_FS_SIZE_BYTES=1\n"
                       "PCM_BACKUP_OK\nPCM_DROP_STARTED\nPCM_RESTORE_OK")}
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}), \
                mock.patch.object(type(self.server), "_staging_neutralize"), \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake):
            ok = self.server.job_restore_backup({
                "backup_id": rec.id, "db_name": "db_b",
                "instance_id": self.machine.id, "pre_backup": True,
                "odoo_instance_id": self.inst_b.id})
        self.assertTrue(ok)
        # Entre los scripts corridos, el del pre-backup lleva el filestore de B.
        joined = "\n---\n".join(c[0][1] for c in fake.run_script.call_args_list)
        self.assertIn("/opt/pcm/instances/cliente-b/data/filestore", joined)
        self.assertNotIn("cliente-a", joined)
        # Se registró un backup con propósito pre_restore de db_b.
        pre = self.env["primate.cloud.backup"].search(
            [("purpose", "=", "pre_restore"), ("database_id", "=", self.db_b.id)])
        self.assertTrue(pre)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B5Staging(TransactionCase):
    """Staging = instancia en un servidor EXISTENTE (D-R4.6) + guard levantado.
    La prueba real (staging conviviendo + neutralización) va en B7."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4 A"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id, "partner_id": self.partner.id})
        # Servidor origen (multi-Odoo), producción, con su instancia + BD.
        self.origin = self.env["primate.cloud.environment"].create({
            "name": "Prod", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-b5", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.origin.id,
            "provisioned_by_pcm": True})
        self.origin.ec2_instance_id = self.machine
        self.origin.state = "active"
        self.origin_inst = self.origin.primary_instance_id
        self.origin_inst.write({"slug": "prod", "state": "active",
                                "main_url": "prod.pcm.test"})
        self.origin.backup_policy_id = self.env["primate.cloud.backup.policy"].create({
            "name": "P", "expected_frequency": "daily", "managed_by_pcm": True,
            "s3_bucket": "pcm-bucket"})
        self.origin._materialize_multiodoo_layout(
            self.origin_inst, {"db_mode": "local_pg"})
        self.origin_db = self.env["primate.cloud.database"].create({
            "name": "prod_db", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
            "instance_id": self.origin_inst.id,
            "ec2_instance_id": self.machine.id})

    def test_wizard_default_mismo_servidor_si_multi(self):
        wiz = self.env["primate.cloud.staging.create.wizard"].new({
            "origin_environment_id": self.origin.id})
        wiz._onchange_origin()
        # D-R4.6: el default es montar en el MISMO servidor (multi) del origen.
        self.assertEqual(wiz.target_server_id, self.origin)

    def test_enqueue_ramifica_a_instancia_en_servidor_existente(self):
        # Con target_server → crea una INSTANCIA staging (no un entorno/EC2).
        fake_delay = mock.Mock()
        params = {"name": "Prod (Staging)", "domain": "stg.pcm.test",
                  "target_server_id": self.origin.id,
                  "origin_instance_id": self.machine.id,
                  "origin_database_id": self.origin_db.id,
                  "db_name": "prod_staging", "admin_password": "x",
                  "transfer_bucket": "pcm-bucket"}
        n_envs = self.env["primate.cloud.environment"].search_count([])
        with mock.patch.object(type(self.origin), "with_delay",
                               return_value=fake_delay):
            inst = self.origin._enqueue_staging(params)
        # NO se creó un entorno nuevo; sí una instancia staging en el origen.
        self.assertEqual(
            self.env["primate.cloud.environment"].search_count([]), n_envs)
        self.assertEqual(inst.environment_id, self.origin)
        self.assertEqual(inst.env_type, "staging")
        self.assertEqual(inst.origin_instance_id, self.origin_inst)
        # Puerto propio asignado por el allocator (no choca con la primaria).
        self.assertNotEqual(inst.http_port, self.origin_inst.http_port)
        # Se encoló el job por instancia con el origen en params.
        args = fake_delay.job_create_staging_instance.call_args[0]
        self.assertEqual(args[0], inst.id)
        self.assertEqual(args[1]["origin_environment_id"], self.origin.id)
        self.assertEqual(args[1]["db_mode"], "local_pg")
        self.assertTrue(args[1]["db_password"])   # transitoria al encolar

    def test_enqueue_origen_no_primaria_no_asume_la_primera(self):
        # El "[:1]" de Fase 8: stageando la instancia NO-primaria de un
        # servidor multi, el staging debe decir que vino de ESA instancia,
        # no de la primaria. El dato correcto = origin_database.instance_id.
        inst_b = self.origin._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.project.id, "slug": "b",
            "odoo_version": "19", "state": "active"})
        self.origin._materialize_multiodoo_layout(inst_b, {"db_mode": "local_pg"})
        db_b = self.env["primate.cloud.database"].create({
            "name": "b_db", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
            "instance_id": inst_b.id, "ec2_instance_id": self.machine.id})
        fake_delay = mock.Mock()
        params = {"name": "Staging de B", "domain": "stgb.pcm.test",
                  "target_server_id": self.origin.id,
                  "origin_instance_id": self.machine.id,   # la MÁQUINA (ambigua)
                  "origin_database_id": db_b.id,            # la base de B (clara)
                  "db_name": "b_staging", "admin_password": "x",
                  "transfer_bucket": "pcm-bucket"}
        with mock.patch.object(type(self.origin), "with_delay",
                               return_value=fake_delay):
            staging_inst = self.origin._enqueue_staging(params)
        # Vino de la instancia B elegida, NO de la primaria del origen.
        self.assertEqual(staging_inst.origin_instance_id, inst_b)
        self.assertNotEqual(staging_inst.origin_instance_id, self.origin_inst)

    def test_enqueue_servidor_nuevo_sin_target_mantiene_path_viejo(self):
        # Sin target_server → sigue creando un ENTORNO staging (path R2).
        fake_delay = mock.Mock()
        params = {"name": "Stg Nuevo", "domain": "stg2.pcm.test",
                  "origin_instance_id": self.machine.id,
                  "origin_database_id": self.origin_db.id,
                  "region": "us-east-1", "db_mode": "local_pg"}
        with mock.patch.object(type(self.origin), "with_delay",
                               return_value=fake_delay):
            staging = self.origin._enqueue_staging(params)
        self.assertEqual(staging._name, "primate.cloud.environment")
        self.assertEqual(staging.env_type, "staging")
        fake_delay.job_create_staging.assert_called_once()

    def test_job_staging_instance_monta_copia_y_neutraliza(self):
        # Camino feliz completo con SSM/S3 mockeados: monta la instancia,
        # copia la base del origen y neutraliza (aunque el SERVIDOR sea prod).
        inst = self.origin._create_instance_with_ports({
            "name": "Stg", "project_id": self.project.id, "slug": "stg",
            "env_type": "staging", "odoo_version": "19",
            "main_url": "stg.pcm.test", "origin_instance_id": self.origin_inst.id})
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success",
            "stdout": ("PCM_BOOTSTRAP_OK\nPCM_INSTALL_OK\n"
                       "PCM_DUMP_SIZE_BYTES=1\nPCM_FS_SIZE_BYTES=1\n"
                       "PCM_BACKUP_OK\nPCM_DROP_STARTED\nPCM_RESTORE_OK")}
        params = {"region": "us-east-1", "db_name": "prod_staging",
                  "db_password": "PgPass_x", "domain": "stg.pcm.test",
                  "admin_password": "x", "transfer_bucket": "pcm-bucket",
                  "origin_environment_id": self.origin.id,
                  "origin_instance_id": self.machine.id,
                  "origin_database_id": self.origin_db.id}
        neutralized = {}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake), \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake), \
                mock.patch.object(aws_s3.AwsS3Service, "ensure_bucket"), \
                mock.patch.object(aws_s3.AwsS3Service, "head_object",
                                  return_value={"key": "k"}), \
                mock.patch.object(type(self.origin), "_instance_http_alive",
                                  return_value=True), \
                mock.patch.object(type(self.origin), "_staging_neutralize",
                                  side_effect=lambda *a, **k:
                                  neutralized.update(done=True)):
            ok = self.origin.job_create_staging_instance(inst.id, params)
        self.assertTrue(ok)
        self.assertEqual(inst.state, "active")
        # Neutralizó aunque el servidor es producción (mira la INSTANCIA).
        self.assertTrue(neutralized.get("done"))

    def test_restore_neutraliza_por_instancia_no_por_servidor(self):
        # El fix clave de B5: un staging (env_type=staging) en un servidor de
        # PRODUCCIÓN debe neutralizarse — el check mira odoo_instance.env_type.
        stg_inst = self.origin._create_instance_with_ports({
            "name": "Stg2", "project_id": self.project.id, "slug": "stg2",
            "env_type": "staging", "odoo_version": "19",
            "main_url": "stg2.pcm.test"})
        self.origin._materialize_multiodoo_layout(
            stg_inst, {"db_mode": "local_pg"})
        db = self.env["primate.cloud.database"].create({
            "name": "stg2_db", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
            "instance_id": stg_inst.id, "ec2_instance_id": self.machine.id})
        rec = self.env["primate.cloud.backup"].create({
            "name": "b", "environment_id": self.origin.id,
            "database_id": self.origin_db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-bucket", "s3_key": "k.dump", "size_mb": 1})
        rec.write({"state": "completed"})
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success", "stdout": "PCM_DROP_STARTED\nPCM_RESTORE_OK"}
        called = {}
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}), \
                mock.patch.object(type(self.origin), "_staging_neutralize",
                                  side_effect=lambda *a, **k:
                                  called.update(done=True)), \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake):
            ok = self.origin.job_restore_backup({
                "backup_id": rec.id, "db_name": "stg2_db",
                "instance_id": self.machine.id,
                "odoo_instance_id": stg_inst.id, "pre_backup": False})
        self.assertTrue(ok)
        # Servidor = producción, pero la instancia destino es staging → NEUTRALIZA.
        self.assertTrue(called.get("done"))


@tagged("post_install", "-at_install", "primate_cloud")
class TestEnvTypeFriction(TransactionCase):
    """Barrido de env_type: las fricciones de PRODUCCIÓN miran la INSTANCIA,
    no el servidor. El caso peligroso: una instancia que ES producción en un
    servidor cuya PRIMARIA es staging → sin este fix la barrera no se dispara
    y alguien opera producción sin fricción."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4 P"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id, "partner_id": self.partner.id})
        # Servidor cuya PRIMARIA es STAGING (env_type del servidor = staging,
        # arbitrario post-R1) pero que hospeda una instancia de PRODUCCIÓN.
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Compartido", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-f", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True})
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.stg_primary = self.server.primary_instance_id   # env_type=staging
        self.stg_primary.write({"slug": "stg", "state": "active"})
        self.server._materialize_multiodoo_layout(
            self.stg_primary, {"db_mode": "local_pg"})
        # La instancia PROD hospedada (NO primaria).
        self.prod_inst = self.server._create_instance_with_ports({
            "name": "Prod Cliente", "project_id": self.project.id,
            "slug": "prod", "env_type": "production", "odoo_version": "19",
            "state": "active", "main_url": "prod.pcm.test"})
        self.server._materialize_multiodoo_layout(
            self.prod_inst, {"db_mode": "local_pg"})

    def test_servidor_dice_staging_pero_instancia_es_prod(self):
        # La premisa del barrido: el env_type del servidor NO refleja la prod.
        self.assertEqual(self.server.env_type, "staging")
        self.assertEqual(self.prod_inst.env_type, "production")

    def test_config_prod_pide_nombre_por_instancia(self):
        # Editar la config de la instancia PROD exige tipear el nombre aunque
        # el servidor diga staging. (Sin el fix, no pediría nada.)
        with self.assertRaises(UserError) as ctx:
            self.machine.action_save_config(
                {"workers": "2"}, "hash-x", odoo_instance=self.prod_inst)
        self.assertIn("PRODUCCIÓN", str(ctx.exception))
        # Con el nombre correcto (del entorno) pasa a encolar.
        with mock.patch.object(type(self.machine), "with_delay") as wd:
            self.machine.action_save_config(
                {"workers": "2"}, "hash-x", typed_name=self.server.name,
                odoo_instance=self.prod_inst)
            wd.assert_called_once()
        # Editar la instancia STAGING (primaria) NO exige nombre.
        with mock.patch.object(type(self.machine), "with_delay") as wd:
            self.machine.action_save_config(
                {"workers": "2"}, "hash-x", odoo_instance=self.stg_primary)
            wd.assert_called_once()

    def test_impersonar_prod_pide_nombre_por_instancia(self):
        admin = self.env.ref("primate_cloud_manager.group_cloud_admin")
        admin.write({"user_ids": [(4, self.env.user.id)]})
        # Impersonar en la instancia PROD → exige nombre (ya era instance-based).
        with self.assertRaises(UserError) as ctx:
            self.machine.action_login_as("db", 5, "ana",
                                         odoo_instance=self.prod_inst)
        self.assertIn("PRODUCCIÓN", str(ctx.exception))
        # En la staging, no.
        res = self.machine.action_login_as("db", 5, "ana",
                                           odoo_instance=self.stg_primary)
        self.assertIn("token=", res["url"])

    def test_restore_wizard_prod_por_instancia(self):
        db = self.env["primate.cloud.database"].create({
            "name": "prod_db", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "instance_id": self.prod_inst.id, "ec2_instance_id": self.machine.id})
        wiz = self.env["primate.cloud.backup.restore.wizard"].new({
            "target_environment_id": self.server.id, "target_db_name": "prod_db"})
        wiz._compute_target_is_production()
        # La base es de la instancia PROD → fricción, aunque el server=staging.
        self.assertTrue(wiz.target_is_production)
        # Una base de la instancia staging → sin fricción.
        db.write({"name": "stg_db", "instance_id": self.stg_primary.id})
        wiz.target_db_name = "stg_db"
        wiz._compute_target_is_production()
        self.assertFalse(wiz.target_is_production)

    def test_dns_delete_ack_por_instancia(self):
        rec = self.env["primate.cloud.dns.record"].create({
            "name": "prod.pcm.test", "account_id": self.account.id,
            "environment_id": self.server.id, "instance_id": self.prod_inst.id,
            "record_type": "A", "record_value": "1.2.3.4"})
        rec._compute_delete_needs_ack()
        self.assertTrue(rec.delete_needs_ack)   # instancia prod → ack
        rec.instance_id = self.stg_primary
        rec._compute_delete_needs_ack()
        self.assertFalse(rec.delete_needs_ack)  # instancia staging → sin ack


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B6Shims(TransactionCase):
    """R4-B6.1: retiro de shims B (wrappers por EC2-id) y C (default-primaria de
    _panel_target). El inverso del patrón: los shims YA NO existen y todo camino
    del panel declara la instancia explícita."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4 P"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id, "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "S", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id, "aws_instance_id": "i-6",
            "instance_state": "running", "region": "us-east-1",
            "environment_id": self.server.id, "provisioned_by_pcm": True})
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.prim = self.server.primary_instance_id
        self.prim.write({"slug": "a", "state": "active"})
        self.server._materialize_multiodoo_layout(self.prim, {"db_mode": "local_pg"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "B", "project_id": self.project.id, "slug": "b",
            "odoo_version": "19", "state": "active"})
        self.server._materialize_multiodoo_layout(self.inst_b, {"db_mode": "local_pg"})
        self.Dash = self.env["primate.cloud.dashboard"]

    # --- shim C: _panel_target requerido ---
    def test_panel_target_sin_instancia_levanta(self):
        with self.assertRaises(UserError):
            self.machine._panel_target(None)
        with self.assertRaises(UserError):
            self.machine._panel_target(
                self.env["primate.cloud.instance"])   # recordset vacío
        # Con instancia explícita, la devuelve.
        self.assertEqual(self.machine._panel_target(self.inst_b), self.inst_b)

    def test_fetch_config_sin_instancia_ya_no_resuelve_primaria(self):
        # Antes: fetch_config() sin instancia caía a la primaria. Ahora levanta.
        with self.assertRaises(UserError):
            self.machine.fetch_config()

    # --- shim B: wrappers viejos por EC2-id NO existen ---
    def test_wrappers_viejos_no_existen(self):
        for viejo in ("get_instance_logs", "get_instance_config",
                      "save_instance_config", "list_instance_db_users",
                      "add_instance_addon", "get_instance_logs_stream"):
            self.assertFalse(hasattr(self.Dash, viejo),
                             "el wrapper viejo %s debería estar retirado" % viejo)
        # login_as viejo tampoco (el nuevo es odoo_login_as).
        self.assertTrue(hasattr(self.Dash, "odoo_login_as"))
        self.assertTrue(hasattr(self.Dash, "get_odoo_config"))

    def test_wrapper_nuevo_opera_LA_instancia_no_la_primaria(self):
        # El wrapper por instancia-id opera las rutas de ESA instancia (B), no
        # las de la primaria: config de inst_b usa el conf de inst_b.
        fake = mock.Mock()
        fake.run_script.return_value = {
            "status": "Success", "stdout": "PCM_HASH:h"}
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            self.Dash.get_odoo_config(self.inst_b.id)
        script = fake.run_script.call_args[0][1]
        self.assertIn('CONF = "/etc/odoo/b.conf"', script)   # el de B
        self.assertNotIn("/etc/odoo/a.conf", script)         # NO la primaria

    def test_wrapper_config_is_production_por_instancia(self):
        # is_production sale del env_type de la instancia (no del servidor).
        self.inst_b.env_type = "production"
        self.prim.env_type = "staging"
        fake = mock.Mock()
        fake.run_script.return_value = {"status": "Success", "stdout": "PCM_HASH:h"}
        with mock.patch.object(type(self.machine), "_get_ssm_service",
                               return_value=fake):
            data = self.Dash.get_odoo_config(self.inst_b.id)
        self.assertTrue(data["is_production"])   # la instancia B es prod

    def test_server_detail_ya_no_expone_primary_instance_id(self):
        # El puente JS murió en B6.2 → el campo se retiró en B6.3 (vestigial).
        data = self.Dash.get_server_detail(self.machine.id)
        self.assertNotIn("primary_instance_id", data)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B6Pantallas(TransactionCase):
    """R4-B6.2: serializer de instancia (rutas del backend) + pantalla de
    servidor sin acciones por-instancia (el puente JS murió, shim A)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.partner_a = self.env["res.partner"].create({"name": "Cliente Test R4 A"})
        self.partner_b = self.env["res.partner"].create({"name": "Cliente Test R4 B"})
        self.proj_a = self.env["primate.cloud.project"].create(
            {"name": "Cliente A", "account_id": self.account.id, "partner_id": self.partner_a.id})
        self.proj_b = self.env["primate.cloud.project"].create(
            {"name": "Cliente B", "account_id": self.account.id, "partner_id": self.partner_b.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Compartido", "project_id": self.proj_a.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id, "aws_instance_id": "i-p",
            "instance_state": "running", "region": "us-east-1",
            "environment_id": self.server.id, "provisioned_by_pcm": True,
            "public_ip": "1.2.3.4"})
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "cliente-a", "state": "active",
                           "main_url": "a.pcm.test"})
        self.server._materialize_multiodoo_layout(
            self.inst_a, {"db_mode": "local_pg"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.proj_b.id, "slug": "cliente-b",
            "odoo_version": "19", "state": "active", "env_type": "staging",
            "main_url": "b.pcm.test"})
        self.server._materialize_multiodoo_layout(
            self.inst_b, {"db_mode": "local_pg"})
        self.Dash = self.env["primate.cloud.dashboard"]

    # --- serializer de instancia: RUTAS del backend (shim A muerto) ---
    def test_instance_detail_rutas_del_backend(self):
        data = self.Dash.get_odoo_instance_detail(self.inst_b.id)
        self.assertEqual(data["id"], self.inst_b.id)
        # Las rutas salen del slug de ESTA instancia (no PCM_PATHS legacy).
        self.assertEqual(data["paths"]["conf"], "/etc/odoo/cliente-b.conf")
        self.assertEqual(data["paths"]["service"], "odoo-cliente-b")
        self.assertEqual(data["paths"]["addons"],
                         "/opt/pcm/instances/cliente-b/addons")
        self.assertIn("/opt/pcm/runtime/odoo-19", data["paths"]["python"])
        # Cliente + servidor + env_type de la instancia.
        self.assertEqual(data["project_name"], self.proj_b.display_name)
        self.assertEqual(data["env_type"], "staging")
        self.assertEqual(data["server_machine_id"], self.machine.id)

    def test_instance_detail_is_production_por_instancia(self):
        # inst_a es production, inst_b staging — cada una su verdad.
        self.assertTrue(
            self.Dash.get_odoo_instance_detail(self.inst_a.id)["is_production"])
        self.assertFalse(
            self.Dash.get_odoo_instance_detail(self.inst_b.id)["is_production"])

    # --- serializer de servidor: lista de instancias + resumen env_type ---
    def test_server_detail_lista_instancias_con_cliente(self):
        data = self.Dash.get_server_detail(self.machine.id)
        hosted = {h["name"]: h for h in data["hosted_instances"]}
        self.assertIn("Odoo B", hosted)
        self.assertEqual(hosted["Odoo B"]["project_name"], self.proj_b.display_name)
        self.assertEqual(hosted["Odoo B"]["env_type"], "staging")

    def test_server_detail_resumen_env_type_no_miente(self):
        # D-B6.1: el servidor NO tiene env_type; muestra la MEZCLA honesta.
        data = self.Dash.get_server_detail(self.machine.id)
        self.assertIn("producción", data["hosted_summary"].lower())
        self.assertIn("staging", data["hosted_summary"].lower())
        self.assertTrue(data["hosts_production"])

    # --- inverso: la pantalla de servidor NO expone acciones por-instancia ---
    def test_pantalla_servidor_sin_acciones_por_instancia(self):
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        srv_js = open(os.path.join(
            base, "static/src/app/screens/servidor_detalle.js")).read()
        # El puente murió: cero wrappers por-instancia y cero PCM_PATHS.
        for prohibido in ("get_odoo_config", "odoo_login_as", "get_odoo_logs",
                          "add_odoo_addon", "save_odoo_config",
                          "primary_instance_id", "PCM_PATHS"):
            self.assertNotIn(prohibido, srv_js,
                             "la pantalla de servidor no debe referenciar %s"
                             % prohibido)

    def test_pcm_paths_constante_no_existe_en_ninguna_pantalla(self):
        # Shim A muerto: PCM_PATHS ya no es una constante hardcodeada.
        import os, re
        base = os.path.dirname(os.path.dirname(__file__))
        for screen in ("servidor_detalle.js", "instancia_detalle.js"):
            src = open(os.path.join(
                base, "static/src/app/screens", screen)).read()
            self.assertIsNone(
                re.search(r"const\s+PCM_PATHS\s*=", src),
                "PCM_PATHS no debe existir como constante en %s" % screen)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B6Terminate(TransactionCase):
    """R4-B6.3 / D-B6.5 (corregida): Terminate es admin-only DECLARATIVO (no hay
    asignación operador→cliente en el modelo para decidirlo en runtime); la
    confirmación igual lista a los afectados + realza producción para el admin."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.partner_a = self.env["res.partner"].create({"name": "Partner A"})
        self.partner_b = self.env["res.partner"].create({"name": "Partner B"})
        self.proj_a = self.env["primate.cloud.project"].create({
            "name": "Cliente A", "account_id": self.account.id,
            "partner_id": self.partner_a.id})
        self.proj_b = self.env["primate.cloud.project"].create({
            "name": "Cliente B", "account_id": self.account.id,
            "partner_id": self.partner_b.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Compartido", "project_id": self.proj_a.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-compartido", "account_id": self.account.id,
            "aws_instance_id": "i-t", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id})
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst_a = self.server.primary_instance_id
        self.inst_a.write({"slug": "a", "state": "active", "name": "Odoo A",
                           "env_type": "production"})
        self.inst_b = self.server._create_instance_with_ports({
            "name": "Odoo B", "project_id": self.proj_b.id, "slug": "b",
            "odoo_version": "19", "state": "active", "env_type": "staging"})

    def _terminate_wizard(self):
        return self.env["primate.cloud.ec2.terminate.wizard"].create({
            "instance_id": self.machine.id,
            "confirm_name": "srv-compartido", "acknowledge": True})

    def test_confirmacion_lista_afectados_y_produccion(self):
        wiz = self._terminate_wizard()
        wiz._compute_affected()
        # Lista las dos instancias con su cliente (radio de daño, para el admin).
        self.assertIn("Odoo A", wiz.affected_text)
        self.assertIn("Cliente A", wiz.affected_text)
        self.assertIn("Odoo B", wiz.affected_text)
        self.assertIn("Cliente B", wiz.affected_text)
        # Realza la de producción.
        self.assertIn("PRODUCCIÓN", wiz.affected_text)
        self.assertTrue(wiz.hosts_production)
        self.assertIn("Cliente A", wiz.affected_clients)
        self.assertIn("Cliente B", wiz.affected_clients)

    def test_operador_no_puede_terminar_admin_only_declarativo(self):
        # D-B6.5 corregida: admin-only SIEMPRE (server-side), no un chequeo
        # cross-cliente que en Primate daría siempre True.
        operator = self.env["res.users"].create({
            "name": "Op", "login": "op_termbe",
            "group_ids": [(6, 0, [self.env.ref(
                "primate_cloud_manager.group_cloud_operator").id])]})
        with self.assertRaises(UserError):
            self.machine.with_user(operator).action_terminate()
        # El chequeo imperativo muerto ya no existe.
        self.assertFalse(hasattr(type(self.machine), "_terminate_is_cross_client"))

    def test_admin_puede_terminar(self):
        admin = self.env["res.users"].create({
            "name": "Adm", "login": "adm_termbe",
            "group_ids": [(6, 0, [self.env.ref(
                "primate_cloud_manager.group_cloud_admin").id])]})
        with mock.patch.object(type(self.machine), "with_delay") as wd:
            self.machine.with_user(admin).action_terminate()
            wd.assert_called()


@tagged("post_install", "-at_install", "primate_cloud")
class TestR4B6DnsYRefresh(TransactionCase):
    """R4-B6.4: DNS best-effort (§8.1) + refresh de staging por-instancia (D-B6.6)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.partner = self.env["res.partner"].create({"name": "Cliente Test R4 P"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id, "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "S", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id, "aws_instance_id": "i-d",
            "instance_state": "running", "region": "us-east-1",
            "environment_id": self.server.id, "public_ip": "1.2.3.4"})
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "a", "state": "active", "main_url": "a.pcm.test"})
        self.server._materialize_multiodoo_layout(self.inst, {"db_mode": "local_pg"})

    # --- DNS best-effort ---
    def test_dns_pending_se_puebla_y_expone(self):
        self.inst._set_dns_pending("Route53 timeout", {
            "hosted_zone_id": "Z1", "ttl": 300, "domain": "a.pcm.test"})
        data = self.inst._dns_pending_data()
        self.assertEqual(data["reason"], "Route53 timeout")
        self.assertEqual(data["hosted_zone_id"], "Z1")
        # El serializer de instancia lo expone para el badge.
        detail = self.env["primate.cloud.dashboard"].get_odoo_instance_detail(
            self.inst.id)
        self.assertTrue(detail["dns_pending"])
        self.assertEqual(detail["dns_pending"]["domain"], "a.pcm.test")

    def test_retry_dns_reusa_provision_sin_install(self):
        # El reintento crea el registro reusando _provision_dns; NO corre SSM
        # de install (cero DIRTY_SLUG). Éxito → limpia el flag + crea el record.
        self.inst._set_dns_pending("timeout", {
            "hosted_zone_id": "Z1", "ttl": 300, "domain": "a.pcm.test"})
        called = {}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_provision_dns",
                                  side_effect=lambda *a, **k:
                                  called.update(dns=True)):
            ok = self.inst.job_retry_dns()
        self.assertTrue(ok)
        self.assertTrue(called.get("dns"))
        self.assertFalse(self.inst.dns_pending)   # flag limpio

    def test_retry_dns_falla_refresca_motivo(self):
        self.inst._set_dns_pending("timeout 1", {
            "hosted_zone_id": "Z1", "ttl": 300, "domain": "a.pcm.test"})
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(type(self.server), "_provision_dns",
                                  side_effect=Exception("timeout 2")):
            ok = self.inst.job_retry_dns()
        self.assertFalse(ok)
        self.assertIn("timeout 2", self.inst._dns_pending_data()["reason"])

    def test_action_retry_sin_pendiente_falla(self):
        with self.assertRaises(UserError):
            self.inst.action_retry_dns()

    # --- Refresh por-instancia (D-B6.6) ---
    def test_resolve_refresh_origin_instance_based(self):
        # Staging instance-based: usa origin_instance_id (no la primaria).
        origin = self.inst
        origin_db = self.env["primate.cloud.database"].create({
            "name": "prod_db", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "instance_id": origin.id})
        origin.database_id = origin_db
        stg = self.server._create_instance_with_ports({
            "name": "Stg", "project_id": self.project.id, "slug": "stg",
            "odoo_version": "19", "env_type": "staging",
            "origin_instance_id": origin.id})
        oi, odb = stg._resolve_refresh_origin()
        self.assertEqual(oi, origin)
        self.assertEqual(odb, origin_db)

    def test_resolve_refresh_origin_env_based_no_primaria(self):
        # Staging env-based (fase 8): deriva de staging_origin_database_id
        # .instance_id, NO de la primaria del origen.
        origin_db = self.env["primate.cloud.database"].create({
            "name": "prod_db", "account_id": self.account.id,
            "environment_id": self.server.id, "db_type": "local_pg",
            "instance_id": self.inst.id})
        stg_env = self.env["primate.cloud.environment"].create({
            "name": "StgEnv", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "staging_origin_database_id": origin_db.id})
        stg_prim = stg_env.primary_instance_id
        oi, odb = stg_prim._resolve_refresh_origin()
        self.assertEqual(odb, origin_db)
        self.assertEqual(oi, self.inst)   # la instancia de la BD, no [:1]

    def test_resolve_refresh_origin_sin_dato_falla(self):
        stg = self.server._create_instance_with_ports({
            "name": "Stg2", "project_id": self.project.id, "slug": "stg2",
            "odoo_version": "19", "env_type": "staging"})
        with self.assertRaises(UserError):
            stg._resolve_refresh_origin()

    def test_refresh_no_staging_falla(self):
        with self.assertRaises(UserError):
            self.inst.action_refresh_staging()   # inst es production
