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

from odoo.exceptions import UserError
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


PAQUETES = (
    "Babel asn1crypto boto3 botocore cbor2 certifi cffi chardet cryptography "
    "decorator docutils ebaysdk freezegun geoip2 gevent greenlet idna Jinja2 "
    "libsass lxml lxml-html-clean MarkupSafe num2words ofxparse openpyxl "
    "passlib Pillow polib psutil psycopg2 pydot pyOpenSSL PyPDF2 pyserial "
    "python-dateutil python-ldap python-stdnum pytz pyusb qrcode reportlab "
    "requests rjsmin urllib3 vobject Werkzeug xlrd XlsxWriter xlwt zeep "
    "paramiko pycryptodome unidecode phonenumbers pdfminer.six xmlsec "
    "zxcvbn Markdown pandas numpy openupgradelib simplejson suds-community "
    "pysftp pyjwt oauthlib requests-oauthlib python-docx pyotp html2text "
    "astor schwifty pydantic email-validator jsonschema attrs rpds-py "
    "referencing typing_extensions six ply pycparser bcrypt pynacl "
    "isodate requests-toolbelt cached-property lxml-stubs zope.event "
    "zope.interface rsa pyasn1 google-auth cachetools xmltodict "
    "Unidecode PyYAML holidays python-slugify text-unidecode s3transfer "
    "jmespath packaging pyparsing chardet charset-normalizer").split()


def _twin_realista(n_instancias, n_venvs):
    """Servidor tipo Cloudpepper: cada instancia con ~25 repos (SHA de
    verdad, poco comprimibles), conf largo y su venv (de n_venvs)."""
    import random
    azar = random.Random(42)

    def sha():
        return "".join(azar.choice("0123456789abcdef") for _ in range(40))

    venvs = {}
    for v in range(n_venvs):
        bin_py = "/opt/odoo/venvs/odoo17-%d/bin/python3" % v
        venvs[bin_py] = {"version": "Python 3.10.12", "pip_freeze": "\n".join(
            "%s==%d.%d.%d" % (pkg, azar.randint(0, 9), azar.randint(0, 30),
                              azar.randint(0, 20)) for pkg in PAQUETES)}
    instancias = []
    for i in range(n_instancias):
        base = "/opt/odoo/cliente-%02d" % i
        repos = []
        for r in range(25):
            ruta = "%s/addons/repo-%02d-%s" % (base, r, azar.choice(
                ("account-financial-tools", "server-tools", "web", "l10n-uy",
                 "custom", "reporting-engine", "sale-workflow", "stock-logistics")))
            repos.append({"path": ruta, "remote_url": "git@github.com:org-%d/%s.git"
                          % (i, ruta.rsplit("/", 1)[-1]), "branch": "17.0",
                          "commit_sha": sha(), "commit_date": "2026-09-%02dT10:00:00-03:00"
                          % (r % 28 + 1), "submodules": [], "dirty_files": r % 3,
                          "unpushed_commits": 0, "alert_local_changes": bool(r % 3)})
        conf = {"opcion_%02d" % k: "valor-%s" % sha()[:12] for k in range(40)}
        conf.update({"db_name": "cliente_%02d" % i, "admin_passwd": "******",
                     "http_port": str(8069 + i * 10),
                     "gevent_port": str(8072 + i * 10)})
        bin_py = list(venvs)[i % n_venvs]
        instancias.append({
            "conf_path": "/etc/odoo/cliente-%02d.conf" % i, "conf_masked": conf,
            "addons_path": [r["path"] for r in repos], "workers": "4",
            "limits": {"limit_time_real": "1200"},
            "service": {"unit": "odoo-cliente-%02d.service" % i, "python_bin": bin_py,
                        "odoo_bin": "/opt/odoo/src/odoo-bin", "active": "active",
                        "enabled": "enabled"},
            "odoo_core": {"path": "/opt/odoo/src", "release": "17.0",
                          "commit_sha": sha(), "branch": "17.0"},
            "odoo_enterprise": {"path": "/opt/odoo/enterprise", "commit_sha": sha(),
                                "branch": "17.0"},
            "python": {"bin": bin_py},
            "database": {"db_name": "cliente_%02d" % i, "pg_version": "14.9",
                         "db_size_bytes": azar.randint(10 ** 9, 5 * 10 ** 10)},
            "filestore": {"path": "%s/data/filestore/cliente_%02d" % (base, i),
                          "size_bytes": azar.randint(10 ** 9, 10 ** 10)},
            "repos": repos})
    return {"twin_format": 2, "host": "cp-servidor",
            "system": {"os": "Ubuntu 22.04.4 LTS", "kernel": "5.15", "packages": {},
                       "cron_system": "", "cron_d": [], "cron_root": ""},
            "nginx": [{"site": "cliente-%02d" % i, "server_names": "cliente-%02d.example.com" % i,
                       "proxy_ports": [str(8069 + i * 10)], "certificates": []}
                      for i in range(n_instancias)],
            "instances": instancias, "venvs": venvs}


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

    def test_script_enmascara_mas_claves_del_conf(self):
        """MASCARA cubre password, passwd, secret, token, key y api."""
        ns, _sh = self._funciones_del_script()
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "x.conf")
            with open(conf, "w") as fh:
                fh.write("[options]\nadmin_passwd = a1\nsmtp_passwd = a2\n"
                         "s3_secret_access = a3\nsibra_token = a4\n"
                         "google_api_key = a5\nPAGOS_APIKEY = a6\n"
                         "db_name = prod\nhttp_port = 8069\n"
                         "list_db = False\n")
            _opts, masked = ns["leer_conf"](conf)
        for clave in ("admin_passwd", "smtp_passwd", "s3_secret_access",
                      "sibra_token", "google_api_key", "pagos_apikey"):
            self.assertEqual(masked[clave], "******", clave)
        self.assertEqual(masked["db_name"], "prod")
        self.assertEqual(masked["http_port"], "8069")
        texto = json.dumps(masked)
        for secreto in ("a1", "a2", "a3", "a4", "a5", "a6"):
            self.assertNotIn('"%s"' % secreto, texto)

    def test_script_tapa_secretos_del_crontab(self):
        ns, _sh = self._funciones_del_script()
        crontab = (
            "SHELL=/bin/sh\n"
            "PGPASSWORD=claveDePg pg_dump prod > /backup/x.sql\n"
            "DB_PASSWORD='con espacios' /opt/x.sh\n"
            "0 3 * * * mysqldump -uroot -pMiClave db > /tmp/d.sql\n"
            "5 3 * * * /opt/sync --password=ClaveLarga --verbose\n"
            "6 3 * * * /opt/sync --token ElToken\n"
            "7 3 * * * curl https://user:ClaveUrl@api.ejemplo.com/hook\n"
            "8 3 * * * API_KEY=abc123 SIBRA_TOKEN=t0k /opt/factura.sh\n"
            "9 3 * * * ssh -p 22 backup@host 'ls'\n")
        tapado = ns["tapar_secretos"](crontab)
        for secreto in ("claveDePg", "con espacios", "MiClave", "ClaveLarga",
                        "ElToken", "ClaveUrl", "abc123", "t0k"):
            self.assertNotIn(secreto, tapado, secreto)
        # La forma queda: se ve QUÉ corre, no con qué clave.
        self.assertIn("pg_dump prod", tapado)
        self.assertIn("PGPASSWORD=******", tapado)
        self.assertIn("-p******", tapado)
        self.assertIn("ssh -p 22 backup@host", tapado)   # -p con espacio: puerto
        self.assertIn("SHELL=/bin/sh", tapado)

    def test_script_une_confs_de_etc_y_de_systemd(self):
        """Un Odoo con el conf fuera de /etc/odoo (lo usa una unit) no se
        pierde, y el mismo conf no aparece dos veces."""
        ns, _sh = self._funciones_del_script()
        with tempfile.TemporaryDirectory() as tmp:
            etc = os.path.join(tmp, "etc")
            os.makedirs(os.path.join(etc, "odoo"))
            a = os.path.join(etc, "odoo", "a.conf")
            fuera = os.path.join(tmp, "opt", "cliente-x", "odoo.conf")
            os.makedirs(os.path.dirname(fuera))
            for ruta in (a, fuera):
                open(ruta, "w").close()
            units = {os.path.realpath(fuera): {"unit": "odoo-x.service"},
                     os.path.realpath(a): {"unit": "odoo-a.service"}}
            confs = ns["confs_a_leer"](units, base_dir=etc)
            reales = {os.path.realpath(c) for c in confs}
            self.assertEqual(len(confs), 2)
            self.assertEqual(reales, {os.path.realpath(fuera),
                                      os.path.realpath(a)})

    def test_script_conf_desde_el_argv(self):
        ns, _sh = self._funciones_del_script()
        f = ns["conf_de_argv"]
        self.assertEqual(f(["python", "odoo-bin", "-c", "/opt/x.conf"]), "/opt/x.conf")
        self.assertEqual(f(["odoo", "--config", "/o/y.conf"]), "/o/y.conf")
        self.assertEqual(f(["odoo", "--config=/o/z.conf"]), "/o/z.conf")
        self.assertEqual(f(["odoo"]), "")

    def test_script_un_solo_enterprise(self):
        """La palabra «enterprise» en otra ruta no la vuelve enterprise."""
        ns, sh = self._funciones_del_script()
        with tempfile.TemporaryDirectory() as tmp:
            ent = os.path.join(tmp, "enterprise")
            otro = os.path.join(tmp, "addons-enterprise-cliente")
            con_web = os.path.join(tmp, "odoo-ee-17")
            for ruta in (ent, otro, os.path.join(con_web, "web_enterprise")):
                os.makedirs(ruta)
            self.assertTrue(ns["es_enterprise"](ent))
            self.assertTrue(ns["es_enterprise"](con_web))
            self.assertFalse(ns["es_enterprise"](otro))
        self.assertIn("if enterprise is None and es_enterprise(base):", sh)

    def test_script_submodulos_con_safe_directory(self):
        _ns, sh = self._funciones_del_script()
        self.assertIn("git -c safe.directory='*' submodule status", sh)
        self.assertNotRegex(sh, r"\"git submodule status\"")

    def test_formato_2_pip_freeze_por_venv(self):
        """twin_format 2: versión y pip freeze una vez por venv; PCM los
        devuelve a cada instancia al aplicar."""
        twin = _twin_dos_instancias()
        venv = twin["instances"][0]["python"]["bin"]
        freeze = twin["instances"][0]["python"].get("pip_freeze") or "odoo==19"
        version = twin["instances"][0]["python"].get("version") or "Python 3"
        for inst in twin["instances"]:
            inst["python"] = {"bin": venv}
        twin["twin_format"] = 2
        twin["venvs"] = {venv: {"version": version, "pip_freeze": freeze}}
        snaps = self.machine._twin_apply(twin)
        self.assertTrue(snaps)
        for snap in snaps:
            data = json.loads(snap.payload_json)
            self.assertEqual(data["instance"]["python"]["pip_freeze"], freeze)
        self.assertEqual(self.machine.twin_status, "ok")

    def test_gemelo_incompleto_se_marca_y_se_ve(self):
        """Salida cortada (sin PCM_TWIN_OK) o blob roto: motivo explícito."""
        cortada = "\n".join(l for l in _stdout_de(_twin_dos_instancias())
                            .splitlines() if l != "PCM_TWIN_OK")
        twin, motivo = self.machine._twin_parse_seguro(cortada)
        self.assertIsNone(twin)
        self.assertIn("PCM_TWIN_OK", motivo)
        self.assertIn("24.000", motivo)
        twin, motivo = self.machine._twin_parse_seguro(
            "PCM_TWIN_B64:no-es-base64!!\nPCM_TWIN_OK")
        self.assertIsNone(twin)
        self.assertIn("no decodifica", motivo)
        bien, motivo = self.machine._twin_parse_seguro(
            _stdout_de(_twin_dos_instancias()))
        self.assertTrue(bien)
        self.assertIsNone(motivo)

    def test_captura_incompleta_queda_marcada(self):
        with mock.patch.object(type(self.machine), "_twin_fetch",
                               side_effect=UserError("salida cortada")):
            res = self.machine.action_capture_twin()
        self.assertEqual(res["params"]["type"], "warning")
        self.assertEqual(self.machine.twin_status, "incomplete")
        self.assertIn("salida cortada", self.machine.twin_status_detail)
        self.assertIn("Gemelo INCOMPLETO", self.machine.message_ids[0].body)
        data = self.env["primate.cloud.dashboard"].get_server_detail(
            self.machine.id)
        self.assertEqual(data["twin_status"], "incomplete")

    def test_discovery_con_gemelo_cortado_trae_el_motivo(self):
        """El import ya no lo traga: _run_discovery devuelve _twin_error."""
        cortada = "PCM_DISCOVERY_JSON:{\"units\": []}\n" + "\n".join(
            l for l in _stdout_de(_twin_dos_instancias()).splitlines()
            if l != "PCM_TWIN_OK")
        with mock.patch.object(type(self.machine), "_run_readonly_document",
                               return_value={"status": "Success",
                                             "stdout": cortada}):
            datos = self.machine._run_discovery()
        self.assertIsNone(datos["_twin"])
        self.assertIn("PCM_TWIN_OK", datos["_twin_error"])

    def _salida_por_pasos(self, twin):
        """Lo que devolverían los pasos gemelo y venvs del documento."""
        ns, _sh = self._funciones_del_script()
        sin_freeze = json.loads(json.dumps(twin))
        for datos in sin_freeze.get("venvs", {}).values():
            datos.pop("pip_freeze", None)
        gemelo = "\n".join(ns["lineas_gemelo"](sin_freeze))
        venvs = "\n".join(ns["lineas_venvs"](twin.get("venvs") or {}))
        legado = "\n".join(ns["trozos"]("PCM_TWIN_B64:", ns["empaquetar"](twin))
                           + ["PCM_TWIN_OK"])
        return gemelo, venvs, legado

    def test_cinco_instancias_realistas_entran_en_los_pasos(self):
        """5 instancias × 25 repos, 3 venvs de ~120 paquetes: cada paso por
        debajo de los 24.000 de SSM, y el gemelo llega ENTERO con su pip
        freeze. Los tamaños quedan en el mensaje (medición)."""
        twin = _twin_realista(5, 3)
        gemelo, venvs, legado = self._salida_por_pasos(twin)
        medicion = "gemelo=%d venvs=%d (un solo blob: %d)" % (
            len(gemelo), len(venvs), len(legado))
        self.assertLess(len(gemelo), twin_mod.SSM_INLINE_LIMIT, medicion)
        self.assertLess(len(venvs), twin_mod.SSM_INLINE_LIMIT, medicion)
        parseado, motivo = self.machine._twin_parse_seguro(gemelo + "\n" + venvs)
        self.assertIsNone(motivo, medicion)
        self.assertEqual(len(parseado["instances"]), 5)
        snaps = self.machine._twin_apply(parseado)
        self.assertEqual(len(snaps), 5)
        datos = json.loads(snaps[0].payload_json)["instance"]
        self.assertIn("==", datos["python"]["pip_freeze"])
        self.assertEqual(self.machine.twin_status, "ok")

    def test_muchas_instancias_se_parte_por_instancia(self):
        """Con 25 instancias el paso pasa los 24.000: SSM lo corta, pero lo
        que entró llega ENTERO y lo que no queda nombrado."""
        twin = _twin_realista(25, 4)
        gemelo, venvs, _legado = self._salida_por_pasos(twin)
        self.assertGreater(len(gemelo), twin_mod.SSM_INLINE_LIMIT)
        cortado = gemelo[:twin_mod.SSM_INLINE_LIMIT]
        parseado, motivo = self.machine._twin_parse_seguro(cortado + "\n" + venvs)
        self.assertTrue(parseado)
        llegaron = len(parseado["instances"])
        self.assertTrue(0 < llegaron < 25, llegaron)
        self.assertIn("faltan %d de 25 instancias" % (25 - llegaron), motivo)
        # Las que llegaron están completas (25 repos cada una).
        for inst in parseado["instances"]:
            self.assertEqual(len(inst["repos"]), 25)
        # Captura: aplica lo que llegó y marca PARCIAL, visible.
        with mock.patch.object(type(self.machine), "_twin_fetch",
                               return_value=(parseado, motivo)):
            res = self.machine.action_capture_twin()
        self.assertEqual(res["params"]["type"], "warning")
        self.assertIn("PARCIAL", res["params"]["message"])
        self.assertEqual(self.machine.twin_status, "incomplete")
        self.assertIn("faltan", self.machine.twin_status_detail)

    def test_sin_el_paso_de_venvs_se_avisa(self):
        twin = _twin_realista(2, 1)
        gemelo, _venvs, _legado = self._salida_por_pasos(twin)
        parseado, motivo = self.machine._twin_parse_seguro(gemelo)
        self.assertEqual(len(parseado["instances"]), 2)
        self.assertIn("pip freeze", motivo)

    def test_inventario_comprimido(self):
        datos = {"units": [], "units_error": None, "databases": [],
                 "databases_error": None}
        linea = "PCM_DISCOVERY_B64:" + base64.b64encode(
            gzip.compress(json.dumps(datos).encode())).decode()
        with mock.patch.object(type(self.machine), "_run_readonly_document",
                               return_value={"status": "Success",
                                             "stdout": linea}):
            resultado = self.machine._run_discovery()
        self.assertEqual(resultado["units"], [])
        self.assertIsNone(resultado["_twin"])

    def test_run_document_lee_cada_paso(self):
        """GetCommandInvocation por PluginName: cada paso con su salida (y su
        tope); terminal cuando lo son todos; un documento viejo de un paso
        se lee entero."""
        base = mock.Mock()
        client = mock.Mock()
        base.get_client.return_value = client
        client.send_command.return_value = {"Command": {"CommandId": "c1"}}
        salidas = {"pcmInventario": "INV", "pcmGemelo": "GEM",
                   "pcmGemeloVenvs": "VEN"}

        def invocacion(**kw):
            return {"Status": "Success",
                    "StandardOutputContent": salidas[kw["PluginName"]]}

        client.get_command_invocation.side_effect = invocacion
        out = aws_ssm.AwsSsmService(base).run_document(
            "i-1", "pcm-readonly-discovery", steps=twin_mod.DOCUMENT_STEPS,
            _sleep=lambda s: None)
        self.assertEqual(out["status"], "Success")
        self.assertEqual(out["stdout"], "INV\nGEM\nVEN")
        self.assertEqual(out["steps"]["pcmGemelo"]["chars"], 3)

        def viejo(**kw):
            if "PluginName" in kw:
                raise Exception("An error occurred (InvalidPluginName)")
            return {"Status": "Success", "StandardOutputContent": "TODO"}

        client.get_command_invocation.side_effect = viejo
        out = aws_ssm.AwsSsmService(base).run_document(
            "i-1", "pcm-readonly-discovery", steps=twin_mod.DOCUMENT_STEPS,
            _sleep=lambda s: None)
        self.assertEqual(out["stdout"], "TODO")

    def test_script_lee_el_argv_de_systemd(self):
        """systemctl show -p ExecStart devuelve «{ path=… ; argv[]=… }»: el
        python del venv sale del argv (antes tomaba «path=…» y pip quedaba
        vacío — cazado en el servidor real del laboratorio)."""
        ns, _sh = self._funciones_del_script()
        crudo = ("{ path=/opt/x/venv/bin/python3 ; argv[]=/opt/x/venv/bin/python3 "
                 "/opt/x/src/odoo-bin -c /etc/odoo/a.conf ; ignore_errors=no ; "
                 "start_time=[n/a] ; pid=0 ; code=(null) }")
        argv = ns["argv_de"](crudo)
        self.assertEqual(argv.split()[0], "/opt/x/venv/bin/python3")
        self.assertIn("-c /etc/odoo/a.conf", argv)
        self.assertEqual(ns["argv_de"]("/usr/bin/python3 odoo-bin -c b.conf"),
                         "/usr/bin/python3 odoo-bin -c b.conf")

    def test_script_lee_repos_ajenos_sin_tocar_config(self):
        """Root leyendo repos de otro usuario: git exige safe.directory; va
        por invocación (-c), nunca escribiendo la config global."""
        _ns, sh = self._funciones_del_script()
        self.assertNotIn("git config --global", sh)
        self.assertNotRegex(sh, r"sh\(\"git (?!-c safe\.directory='\*')")

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

    def test_script_apaga_escrituras_laterales(self):
        """Lecturas que escriben solas: git status refresca .git/index y
        Python deja .pyc al importar pip. El script las apaga ANTES de
        arrancar Python (y todo subproceso hereda el entorno)."""
        with file_open(twin_mod.DISCOVERY_SCRIPT_PATH) as handle:
            texto = handle.read()
        antes_de_python = texto.split("python3 - <<", 1)[0]
        for variable in ("GIT_OPTIONAL_LOCKS=0", "PYTHONDONTWRITEBYTECODE=1",
                         "PIP_DISABLE_PIP_VERSION_CHECK=1",
                         "PIP_NO_CACHE_DIR=1"):
            with self.subTest(variable=variable):
                self.assertRegex(antes_de_python,
                                 r"(?m)^export [^\n]*\b%s" % re.escape(variable))

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
        pasos = {p["name"]: "\n".join(p["inputs"]["runCommand"])
                 for p in commiteado["mainSteps"]}
        self.assertEqual(list(pasos), list(twin_mod.DOCUMENT_STEPS))
        self.assertIn("PCM_DISCOVERY_B64", pasos["pcmInventario"])
        self.assertIn("export PCM_TWIN_MODO=gemelo", pasos["pcmGemelo"])
        self.assertIn("export PCM_TWIN_MODO=venvs", pasos["pcmGemeloVenvs"])
        # Un documento SSM admite hasta 64 KB.
        self.assertLess(len(json.dumps(commiteado, separators=(",", ":"))),
                        64 * 1024)
        texto = "\n".join(pasos.values())
        for patron in (r"\brm\s", r"\bmv\s", r"systemctl\s+(start|stop|"
                       r"restart|reload)", r"mktemp", r"\btee\b"):
            self.assertIsNone(re.search(patron, texto), patron)



@tagged("post_install", "-at_install", "primate_cloud")
class TestCloudpepperImport(TransactionCase):
    """Servidor externo SIN máquina AWS: gemelo desde archivo, marca de
    origen heredada por todo lo que cuelga del entorno, solo lectura."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Cuenta DNS", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})

    def _importar(self, env_rec=None, twin=None):
        twin = twin or _twin_dos_instancias()
        wiz = self.env["primate.cloud.external.import.wizard"].create({
            "server_name": "cp-clientes-1",
            "environment_id": env_rec.id if env_rec else False,
            "twin_file": base64.b64encode(_stdout_de(twin).encode()),
            "twin_filename": "gemelo.txt"})
        wiz.action_import()
        return self.env["primate.cloud.environment"].search(
            [("name", "=", "cp-clientes-1")], limit=1)

    def test_import_acepta_el_json_decodificado(self):
        """infra/gemelo_cloudpepper.sh deja un .json por servidor."""
        twin = _twin_dos_instancias()
        wiz = self.env["primate.cloud.external.import.wizard"].create({
            "server_name": "cp-clientes-1",
            "twin_file": base64.b64encode(json.dumps(twin).encode()),
            "twin_filename": "cp-clientes-1.json"})
        wiz.action_import()
        env_rec = self.env["primate.cloud.environment"].search(
            [("name", "=", "cp-clientes-1")], limit=1)
        self.assertEqual(env_rec.pcm_origin, "imported_cloudpepper")
        self.assertEqual(len(env_rec.instance_ids), 2)

    def test_import_rechaza_un_json_que_no_es_gemelo(self):
        wiz = self.env["primate.cloud.external.import.wizard"].create({
            "server_name": "cp-x",
            "twin_file": base64.b64encode(b'{"hola": 1}'),
            "twin_filename": "x.json"})
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_import_crea_entorno_marcado_con_gemelo(self):
        env_rec = self._importar()
        self.assertEqual(env_rec.pcm_origin, "imported_cloudpepper")
        self.assertTrue(env_rec.pcm_is_imported())
        self.assertFalse(env_rec.ec2_instance_ids, "Cloudpepper no tiene EC2")
        snaps = self.env["primate.cloud.twin.snapshot"].search(
            [("environment_id", "=", env_rec.id)])
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(i.pcm_is_imported() for i in snaps.instance_id))
        # Re-importar sobre el mismo entorno = versión 2, sin duplicar.
        self._importar(env_rec)
        self.assertEqual(max(self.env["primate.cloud.twin.snapshot"].search(
            [("environment_id", "=", env_rec.id)]).mapped("version")), 2)
        self.assertEqual(len(env_rec.instance_ids.filtered("conf_path")), 2)

    def test_gemelo_por_ssh_de_un_servidor_aws_sin_ssm_va_a_ese_servidor(self):
        """«Sin acceso SSM»: el .json de gemelo_cloudpepper.sh se carga sobre
        el servidor AWS ya importado, sin crear otro entorno."""
        server = self.env["primate.cloud.environment"].with_context(
            pcm_import_sync=True).create({
                "name": "aws-sin-ssm", "account_id": self.account.id,
                "project_id": self.env[
                    "primate.cloud.server.import"]._proyecto_centinela().id,
                "env_type": "production", "state": "active",
                "pcm_origin": "imported_aws"})
        machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "aws-sin-ssm", "account_id": self.account.id,
            "environment_id": server.id, "aws_instance_id": "i-nossm",
            "instance_state": "running", "region": "us-east-2"})
        server.ec2_instance_id = machine
        machine.with_context(pcm_import_sync=True).sudo().write(
            {"management_state": "adopted_readonly"})
        antes = self.env["primate.cloud.environment"].search_count([])
        wiz = self.env["primate.cloud.external.import.wizard"].create({
            "environment_id": server.id,
            "twin_file": base64.b64encode(
                json.dumps(_twin_dos_instancias()).encode()),
            "twin_filename": "aws-sin-ssm.json"})
        wiz.action_import()
        self.assertEqual(
            self.env["primate.cloud.environment"].search_count([]), antes)
        snaps = self.env["primate.cloud.twin.snapshot"].search(
            [("environment_id", "=", server.id)])
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps.machine_id, machine)
        self.assertEqual(server.pcm_origin, "imported_aws")
        self.assertEqual(machine.management_state, "adopted_readonly")

    def test_lo_importado_de_cloudpepper_no_se_toca(self):
        env_rec = self._importar()
        inst = env_rec.instance_ids.filtered("conf_path")[:1]
        dns = self.env["primate.cloud.dns.record"].create({
            "name": "a.cliente.com", "environment_id": env_rec.id,
            "account_id": self.account.id, "hosted_zone_id": "ZCP", "record_type": "A",
            "record_value": "5.5.5.5", "state": "active"})
        intentos = {
            "backup": lambda: env_rec.action_run_backup(),
            "agregar instancia": lambda: env_rec.job_add_instance(inst.id, {}),
            "staging": lambda: env_rec.action_create_staging(),
            "restore externo": lambda: env_rec.action_restore_external_dump(),
            "HTTPS": lambda: inst.action_enable_ssl(),
            "cambiar dominio": lambda: inst.write({"main_url": "x.y.com"}),
            "borrar instancia": lambda: inst.unlink(),
            "borrar DNS": lambda: dns.action_open_delete(),
            "cambiar marca": lambda: env_rec.write({"pcm_origin": "pcm"}),
            "borrar entorno": lambda: env_rec.unlink(),
        }
        for nombre, fn in intentos.items():
            with self.subTest(accion=nombre):
                try:
                    fn()
                    self.fail("%s no se bloqueó" % nombre)
                except UserError:
                    pass

    def test_frontera_conoce_los_nombres_dns_de_cloudpepper(self):
        from odoo.addons.primate_cloud_manager.models.primate_cloud_readonly_guard import (
            ReadonlyGuard)
        env_rec = self._importar()
        self.env["primate.cloud.dns.record"].create({
            "name": "a.cliente.com", "environment_id": env_rec.id,
            "account_id": self.account.id, "hosted_zone_id": "ZCP", "record_type": "A",
            "record_value": "5.5.5.5", "state": "active"})
        guard = ReadonlyGuard.from_env(self.env)
        self.assertIn("a.cliente.com", guard.dns_names)

    def test_la_marca_no_se_pone_a_mano(self):
        proyecto = self.env["primate.cloud.server.import"]._proyecto_centinela()
        env_pcm = self.env["primate.cloud.environment"].create({
            "name": "montado-por-pcm", "env_type": "testing",
            "project_id": proyecto.id})
        with self.assertRaises(UserError):
            env_pcm.write({"pcm_origin": "imported_cloudpepper"})
