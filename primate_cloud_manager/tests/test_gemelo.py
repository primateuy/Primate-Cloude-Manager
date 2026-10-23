# -*- coding: utf-8 -*-
"""Gemelo exacto (punto 3, 2026-10-22).

1. Volcado: servidor con VARIAS instancias Odoo → cada repo, base y dominio
   queda asociado a la instancia correcta; snapshot fechado y versionado.
2. Repo con cambios locales → ALERTA visible (en el snapshot y en el repo).
3. El script REAL de discovery: secretos enmascarados en el conf, detección
   de archivos sin commitear y commits sin pushear (sobre un git temporal).
4. El script es de solo lectura por construcción (guarda estática).
5. Importados: el discovery va por el documento SSM, nunca por shell.
"""
import base64
import gzip
import json
import os
import re
import subprocess
import tempfile
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import file_open

from odoo.addons.primate_cloud_manager.models import primate_cloud_twin as twin_mod
from odoo.addons.primate_cloud_manager.services import aws_ssm


def _twin_dos_instancias():
    def repo(path, dirty=0, ahead=0):
        return {"path": path, "remote_url": "git@github.com:x/%s.git"
                % path.rsplit("/", 1)[-1], "branch": "17.0",
                "commit_sha": "a" * 40, "submodules": [],
                "dirty_files": dirty, "unpushed_commits": ahead,
                "alert_local_changes": bool(dirty or ahead)}

    return {
        "twin_format": 1, "host": "prod-1",
        "system": {"os": "Ubuntu 22.04", "kernel": "6.5", "packages": {},
                   "cron_system": "", "cron_d": [], "cron_root": ""},
        "nginx": [
            {"site": "cliente-a", "server_names": "a.cliente.com",
             "proxy_ports": ["8069"], "certificates": [
                 {"path": "/etc/letsencrypt/live/a/cert.pem",
                  "not_after": "Jan  1 00:00:00 2027 GMT"}]},
            {"site": "cliente-b", "server_names": "b.cliente.com",
             "proxy_ports": ["8079"], "certificates": []},
        ],
        "instances": [
            {"conf_path": "/etc/odoo/cliente-a.conf",
             "conf_masked": {"http_port": "8069", "db_name": "a_prod",
                             "admin_passwd": "******"},
             "addons_path": ["/opt/a/core/addons", "/opt/a/custom"],
             "workers": "4", "limits": {},
             "service": {"unit": "odoo-a.service", "active": "active"},
             "odoo_core": {"release": "17.0", "commit_sha": "c" * 40},
             "odoo_enterprise": {"commit_sha": "e" * 40},
             "python": {"bin": "/opt/a/venv/bin/python3",
                        "version": "Python 3.10.12", "pip_freeze": "x==1"},
             "database": {"db_name": "a_prod", "pg_version": "14.9",
                          "db_size_bytes": 2 * 1048576},
             "filestore": {"path": "/opt/a/data/filestore/a_prod",
                           "size_bytes": 3 * 1048576},
             "repos": [repo("/opt/a/custom", dirty=2, ahead=1)]},
            {"conf_path": "/etc/odoo/cliente-b.conf",
             "conf_masked": {"http_port": "8079", "db_name": "b_prod"},
             "addons_path": ["/opt/b/core/addons", "/opt/b/oca",
                             "/opt/b/custom"],
             "workers": "2", "limits": {},
             "service": {"unit": "odoo-b.service", "active": "active"},
             "odoo_core": {"release": "17.0", "commit_sha": "d" * 40},
             "odoo_enterprise": None,
             "python": {"bin": "/opt/b/venv/bin/python3",
                        "version": "Python 3.10.12", "pip_freeze": ""},
             "database": {"db_name": "b_prod", "pg_version": "14.9",
                          "db_size_bytes": 1048576},
             "filestore": {"path": "/opt/b/data/filestore/b_prod",
                           "size_bytes": 0},
             "repos": [repo("/opt/b/oca"), repo("/opt/b/custom")]},
        ],
    }


def _stdout_de(twin):
    blob = base64.b64encode(gzip.compress(json.dumps(twin).encode())).decode()
    lines = ["PCM_TWIN_B64:%s" % blob[i:i + 4000]
             for i in range(0, len(blob), 4000)]
    return "\n".join(lines + ["PCM_TWIN_OK"])


@tagged("post_install", "-at_install", "primate_cloud")
class TestGemelo(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Prod", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        partner = self.env["res.partner"].create({"name": "Cliente gemelo"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Cliente gemelo", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "prod-compartido", "project_id": self.project.id,
            "account_id": self.account.id, "env_type": "production",
            "odoo_version": "17", "odoo_edition": "enterprise",
            "state": "active"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-1", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-twin",
            "instance_state": "running", "region": "us-east-1"})
        self.server.ec2_instance_id = self.machine
        self.machine.with_context(pcm_import_sync=True).sudo().write(
            {"management_state": "adopted_readonly"})

    def test_parse_roundtrip(self):
        twin = _twin_dos_instancias()
        self.assertEqual(self.machine._twin_parse(_stdout_de(twin)), twin)

    def test_varias_instancias_cada_cosa_en_su_lugar(self):
        snaps = self.machine._twin_apply(_twin_dos_instancias())
        self.assertEqual(len(snaps), 2)
        insts = snaps.mapped("instance_id")
        a = insts.filtered(lambda i: i.conf_path == "/etc/odoo/cliente-a.conf")
        b = insts.filtered(lambda i: i.conf_path == "/etc/odoo/cliente-b.conf")
        self.assertTrue(a and b and a != b)
        self.assertEqual(a.main_url, "a.cliente.com")
        self.assertEqual(b.main_url, "b.cliente.com")
        self.assertEqual(a.http_port, 8069)
        self.assertEqual(b.http_port, 8079)
        self.assertEqual(a.ssl_state, "active")   # tenía certificado
        repos_a = self.env["primate.cloud.repository"].search(
            [("instance_id", "=", a.id)])
        repos_b = self.env["primate.cloud.repository"].search(
            [("instance_id", "=", b.id)])
        self.assertEqual(repos_a.mapped("local_path"), ["/opt/a/custom"])
        self.assertEqual(sorted(repos_b.mapped("local_path")),
                         ["/opt/b/custom", "/opt/b/oca"])
        snap_a = snaps.filtered(lambda s: s.instance_id == a)
        self.assertEqual(snap_a.version, 1)
        self.assertEqual(snap_a.enterprise_commit, "e" * 12)
        self.assertEqual(snap_a.db_size_bytes, 2 * 1048576)

    def test_repo_con_cambios_locales_es_alerta(self):
        snaps = self.machine._twin_apply(_twin_dos_instancias())
        snap_a = snaps.filtered(
            lambda s: s.instance_id.conf_path == "/etc/odoo/cliente-a.conf")
        self.assertEqual(snap_a.dirty_repos, 1)
        self.assertTrue(any("perdería" in a for a in json.loads(
            snap_a.alerts_json)))
        repo = self.env["primate.cloud.repository"].search(
            [("local_path", "=", "/opt/a/custom")])
        self.assertTrue(repo.local_changes_alert)
        self.assertIn("2 archivo", repo.local_changes_detail)
        data = self.env["primate.cloud.dashboard"].get_odoo_instance_detail(
            snap_a.instance_id.id)
        self.assertTrue(data["twin"]["repos"][0]["dirty"])
        self.assertTrue(data["twin"]["alerts"])

    def test_snapshots_versionados_y_comparables(self):
        twin = _twin_dos_instancias()
        primero = self.machine._twin_apply(twin)
        twin["instances"][0]["odoo_core"]["commit_sha"] = "f" * 40
        segundo = self.machine._twin_apply(twin)
        a1 = primero.filtered(lambda s: "cliente-a" in s.instance_id.conf_path)
        a2 = segundo.filtered(lambda s: "cliente-a" in s.instance_id.conf_path)
        self.assertEqual(a2.version, 2)
        diff = a2.compare_to(a1)
        self.assertIn("instance.odoo_core.commit_sha", diff)
        self.assertEqual(diff["instance.odoo_core.commit_sha"][0], "f" * 40)
        # Re-capturar no duplica instancias ni repos.
        self.assertEqual(len(self.server.instance_ids.filtered(
            lambda i: i.conf_path)), 2)

    def test_importado_usa_documento_ssm_nunca_shell(self):
        fake = mock.Mock()
        fake.run_document.return_value = {
            "status": "Success", "stdout": _stdout_de(_twin_dos_instancias())}
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService", return_value=fake):
            self.machine.action_capture_twin()
        self.assertTrue(fake.run_document.called)
        self.assertFalse(fake.run_script.called)
        self.assertEqual(fake.run_document.call_args.args[1],
                         "pcm-readonly-discovery")

    # ------------------------------------------------- el script real
    def _funciones_del_script(self):
        with file_open(twin_mod.DISCOVERY_SCRIPT_PATH) as handle:
            sh = handle.read()
        py = sh.split("<<'PCM_TWIN_PY'\n", 1)[1].split("\nPCM_TWIN_PY", 1)[0]
        # Solo las definiciones: lo de abajo de "twin = {" corre el discovery.
        defs = py.split("\ntwin = {", 1)[0]
        ns = {}
        exec(compile(defs, "pcm_readonly_discovery", "exec"), ns)  # noqa: S102
        return ns, sh

    def test_script_enmascara_secretos(self):
        ns, _sh = self._funciones_del_script()
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "x.conf")
            with open(conf, "w") as fh:
                fh.write("[options]\nadmin_passwd = superSecreto\n"
                         "db_password = otraClave\nsmtp_password = s\n"
                         "db_name = prod\nhttp_port = 8069\n")
            _opts, masked = ns["leer_conf"](conf)
        self.assertEqual(masked["admin_passwd"], "******")
        self.assertEqual(masked["db_password"], "******")
        self.assertEqual(masked["smtp_password"], "******")
        self.assertEqual(masked["db_name"], "prod")
        self.assertNotIn("superSecreto", json.dumps(masked))

    def test_script_detecta_cambios_locales(self):
        ns, _sh = self._funciones_del_script()
        with tempfile.TemporaryDirectory() as tmp:
            remoto = os.path.join(tmp, "remoto.git")
            local = os.path.join(tmp, "local")
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            run = lambda *a, cwd=tmp: subprocess.run(  # noqa: E731
                a, cwd=cwd, env=env, check=True, capture_output=True)
            run("git", "init", "--bare", "-q", remoto)
            run("git", "clone", "-q", remoto, local)
            with open(os.path.join(local, "a.py"), "w") as fh:
                fh.write("x = 1\n")
            run("git", "add", ".", cwd=local)
            run("git", "commit", "-qm", "base", cwd=local)
            run("git", "push", "-q", "origin", "HEAD", cwd=local)
            limpio = ns["git_info"](local)
            self.assertFalse(limpio["alert_local_changes"])
            # commit local sin pushear + archivo sin commitear
            with open(os.path.join(local, "a.py"), "a") as fh:
                fh.write("y = 2\n")
            run("git", "commit", "-qam", "local", cwd=local)
            with open(os.path.join(local, "b.py"), "w") as fh:
                fh.write("z = 3\n")
            sucio = ns["git_info"](local)
        self.assertTrue(sucio["alert_local_changes"])
        self.assertEqual(sucio["unpushed_commits"], 1)
        self.assertEqual(sucio["dirty_files"], 1)

    def test_script_es_de_solo_lectura(self):
        """Guarda estática: el discovery no escribe archivos, no borra, no
        mueve, no instala, no reinicia, no crea temporales."""
        _ns, sh = self._funciones_del_script()
        prohibidos = [
            r"open\([^)]*['\"][wa]b?['\"]", r"\brm\s", r"\bmv\s", r"\bcp\s",
            r"\btee\b", r"mktemp", r"systemctl\s+(start|stop|restart|reload|"
            r"enable|disable)", r"apt(-get)?\s+install", r"pip\s+install",
            r"\bchmod\b", r"\bchown\b", r"git\s+(fetch|pull|push|checkout|"
            r"reset|clean|commit|stash)", r"(?<![<2&\w])>\s*/(?!dev/null)",
            r"psql[^\n]*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)",
        ]
        for patron in prohibidos:
            with self.subTest(patron=patron):
                self.assertIsNone(re.search(patron, sh),
                                  "el discovery contiene algo mutante: %s"
                                  % patron)

    def test_documento_ssm_sincronizado_y_sin_parametros(self):
        """El JSON que se sube a AWS (data/ssm/) es EXACTAMENTE el generado
        de los dos scripts de solo lectura, y no acepta parámetros."""
        esperado = twin_mod.readonly_document_content()
        with file_open(twin_mod.DOCUMENT_JSON_PATH) as handle:
            commiteado = json.load(handle)
        self.assertEqual(commiteado, esperado,
                         "data/ssm/pcm-readonly-discovery.json desactualizado: "
                         "regeneralo con readonly_document_content()")
        self.assertEqual(commiteado["parameters"], {})
        texto = "\n".join(
            commiteado["mainSteps"][0]["inputs"]["runCommand"])
        self.assertIn("PCM_DISCOVERY_JSON", texto)
        self.assertIn("PCM_TWIN_OK", texto)
        for patron in (r"\brm\s", r"\bmv\s", r"systemctl\s+(start|stop|"
                       r"restart|reload)", r"mktemp", r"\btee\b"):
            self.assertIsNone(re.search(patron, texto), patron)

