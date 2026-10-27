# -*- coding: utf-8 -*-
"""Gemelo exacto de servidores importados (punto 3, 2026-10-22).

Objetivo: que la mudanza final sea SOLO un cambio de proveedor — PCM refleja
exactamente lo que existe en cada servidor y en cada Odoo que hospeda: repos
con su SHA (y ALERTA si hay cambios locales que una mudanza por git
perdería), core/enterprise con commit, conf enmascarado, python + pip
freeze, base y filestore con tamaños, vhosts/certificados, crons.

La lectura dentro del servidor es el script FIJO de consulta
``data/pcm_readonly_discovery.sh``:
- servidores IMPORTADOS → vía documento SSM propio (``run_document``),
  el único SendCommand que permite la policy pcm-prod-readonly;
- servidores de PCM / laboratorio → el MISMO script inline (run_script).

Cada captura crea un snapshot FECHADO y VERSIONADO por instancia Odoo,
para comparar contra el ambiente nuevo después de la mudanza.
"""
import base64
import gzip
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from ..services import aws_ssm

_logger = logging.getLogger(__name__)

DISCOVERY_SCRIPT_PATH = "primate_cloud_manager/data/pcm_readonly_discovery.sh"
F2_DISCOVERY_SCRIPT_PATH = "primate_cloud_manager/data/discovery_read.py"
DOCUMENT_JSON_PATH = "primate_cloud_manager/data/ssm/pcm-readonly-discovery.json"
DISCOVERY_DOCUMENT_PARAM = "pcm.readonly_discovery_document"
DISCOVERY_DOCUMENT_DEFAULT = "pcm-readonly-discovery"
DISCOVERY_S3_BUCKET_PARAM = "pcm.readonly_discovery_s3_bucket"


def readonly_document_content():
    """El documento SSM «pcm-readonly-discovery», generado de los DOS scripts
    de solo lectura de data/ (una sola fuente; un test verifica que el JSON
    commiteado en data/ssm/ esté sincronizado con esto).

    SIN parámetros: no hay forma de pasarle un comando. Un solo paso con:
      1) el discovery de F2 (línea PCM_DISCOVERY_JSON — la usa el import);
      2) el gemelo completo (líneas PCM_TWIN_B64 + PCM_TWIN_OK).
    """
    with file_open(F2_DISCOVERY_SCRIPT_PATH) as handle:
        f2 = handle.read()
    with file_open(DISCOVERY_SCRIPT_PATH) as handle:
        twin = handle.read()
    twin_body = [line for line in twin.splitlines()
                 if not line.startswith("#!")]
    run = (["#!/bin/bash", "set -uo pipefail",
            "python3 - <<'PCM_F2_PY'"] + f2.splitlines()
           + ["PCM_F2_PY"] + twin_body)
    return {
        "schemaVersion": "2.2",
        "description": "PCM: discovery de SOLO LECTURA (inventario F2 + "
                       "gemelo exacto). Sin parámetros. No escribe nada en "
                       "el servidor. Único SendCommand permitido por la "
                       "policy pcm-prod-readonly.",
        "parameters": {},
        "mainSteps": [{
            "action": "aws:runShellScript",
            "name": "pcmReadonlyDiscovery",
            "inputs": {"timeoutSeconds": "600", "runCommand": run},
        }],
    }


class PrimateCloudTwinSnapshot(models.Model):
    _name = "primate.cloud.twin.snapshot"
    _description = "Snapshot del gemelo de una instancia Odoo"
    _order = "instance_id, version desc"

    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia Odoo", required=True,
        ondelete="cascade", index=True)
    environment_id = fields.Many2one(
        related="instance_id.environment_id", store=True)
    machine_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Servidor", readonly=True)
    taken_at = fields.Datetime(
        string="Capturado", default=fields.Datetime.now, readonly=True)
    version = fields.Integer(string="Versión", readonly=True,
                             help="Incremental por instancia.")
    payload_json = fields.Text(string="Gemelo (JSON)", readonly=True)
    alerts_json = fields.Text(string="Alertas (JSON)", readonly=True)
    alert_count = fields.Integer(string="Alertas", readonly=True)
    # Resumen plano para listas/comparaciones rápidas.
    odoo_release = fields.Char(readonly=True)
    core_commit = fields.Char(readonly=True)
    enterprise_commit = fields.Char(readonly=True)
    python_version = fields.Char(readonly=True)
    db_size_bytes = fields.Float(readonly=True)
    filestore_bytes = fields.Float(readonly=True)
    repos_count = fields.Integer(readonly=True)
    dirty_repos = fields.Integer(
        string="Repos con cambios locales", readonly=True)

    def payload(self):
        self.ensure_one()
        try:
            return json.loads(self.payload_json or "{}")
        except ValueError:
            return {}

    def compare_to(self, other):
        """Diff plano contra otro snapshot (la verificación post-mudanza):
        devuelve {clave: (de_este, del_otro)} de lo que difiere."""
        self.ensure_one()
        other.ensure_one()
        mio, suyo = self.payload(), other.payload()

        def plano(d, prefix=""):
            flat = {}
            for k, v in sorted((d or {}).items()):
                key = "%s.%s" % (prefix, k) if prefix else k
                if isinstance(v, dict):
                    flat.update(plano(v, key))
                elif isinstance(v, list):
                    flat[key] = json.dumps(v, ensure_ascii=False, sort_keys=True)
                else:
                    flat[key] = v
            return flat

        a, b = plano(mio), plano(suyo)
        return {k: (a.get(k), b.get(k))
                for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


class PrimateCloudTwinMixin(models.AbstractModel):
    """Aplicación del gemelo, compartida por máquina (AWS) y entorno
    (Cloudpepper / cualquier servidor sin EC2: gemelo desde archivo)."""
    _name = "primate.cloud.twin.mixin"
    _description = "Aplicación del gemelo exacto"

    # ------------------------------------------------------------------
    @staticmethod
    def _twin_normalizar(twin):
        """twin_format 2 trae la versión y el pip freeze UNA vez por venv
        (``venvs``); cada instancia los recibe de vuelta acá. El formato 1
        (por instancia) queda igual."""
        venvs = twin.get("venvs") or {}
        for data in twin.get("instances") or []:
            python = data.setdefault("python", {}) or {}
            venv = venvs.get(python.get("bin") or "") or {}
            for clave in ("version", "pip_freeze"):
                if not python.get(clave) and venv.get(clave):
                    python[clave] = venv[clave]
        return twin

    def _twin_apply_to(self, env_rec, twin, machine=None):
        """Vuelca el gemelo a PCM en ``env_rec``: instancias Odoo (creando las
        que falten), repos con alerta de cambios locales, y un snapshot
        versionado por instancia. Los writes van con ``pcm_import_sync`` (es
        EL sync). ``machine`` es opcional: Cloudpepper no tiene EC2."""
        Instance = self.env["primate.cloud.instance"].with_context(
            pcm_import_sync=True)
        Snapshot = self.env["primate.cloud.twin.snapshot"].sudo()
        nginx = twin.get("nginx") or []
        self._twin_normalizar(twin)
        snapshots = Snapshot.browse()
        claimed = Instance.browse()
        twin_confs = {d.get("conf_path") for d in twin.get("instances") or []}
        for data in twin.get("instances") or []:
            instance = self._twin_match_instance(
                Instance, env_rec, data, claimed, twin_confs)
            claimed |= instance
            self._twin_update_instance(instance, data, nginx)
            self._twin_sync_repos(instance, data)
            alerts = self._twin_alerts(data)
            last = Snapshot.search([("instance_id", "=", instance.id)],
                                   order="version desc", limit=1)
            payload = {"system": twin.get("system"), "host": twin.get("host"),
                       "nginx": nginx, "instance": data}
            core = data.get("odoo_core") or {}
            ent = data.get("odoo_enterprise") or {}
            db = data.get("database") or {}
            snapshots |= Snapshot.create({
                "instance_id": instance.id,
                "machine_id": machine.id if machine else False,
                "version": (last.version or 0) + 1,
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "alerts_json": json.dumps(alerts, ensure_ascii=False),
                "alert_count": len(alerts),
                "odoo_release": core.get("release") or "",
                "core_commit": (core.get("commit_sha") or "")[:12],
                "enterprise_commit": (ent.get("commit_sha") or "")[:12],
                "python_version": (data.get("python") or {}).get("version")
                or "",
                "db_size_bytes": db.get("db_size_bytes") or 0,
                "filestore_bytes": (data.get("filestore") or {})
                .get("size_bytes") or 0,
                "repos_count": len(data.get("repos") or []),
                "dirty_repos": len([r for r in data.get("repos") or []
                                    if r.get("alert_local_changes")]),
            })
        return snapshots

    @staticmethod
    def _twin_slug(conf_path):
        base = conf_path.rsplit("/", 1)[-1]
        return re.sub(r"[^a-z0-9-]", "-", base.replace(".conf", "").lower())

    def _twin_match_instance(self, Instance, env_rec, data, claimed=None,
                             twin_confs=()):
        """Asocia los datos a la instancia Odoo CORRECTA — puede haber varias
        por servidor — en este orden: mismo conf, misma base, mismo slug; si
        no, ADOPTA el placeholder que D6 crea con el entorno (instancia
        primaria sin datos reales: si se creara otra, chocaría con su puerto
        8069 por defecto); y recién si no hay, crea una nueva YA con su
        puerto observado."""
        claimed = claimed or Instance.browse()
        conf_path = data.get("conf_path") or ""
        db_name = (data.get("database") or {}).get("db_name") or ""
        dominio = [("environment_id", "=", env_rec.id),
                   ("id", "not in", claimed.ids)]
        instance = Instance.search(dominio + [("conf_path", "=", conf_path)],
                                   limit=1)
        if not instance and db_name:
            instance = Instance.search(
                dominio + [("database_id.name", "=", db_name)], limit=1)
        slug = self._twin_slug(conf_path)
        if not instance:
            instance = Instance.search(dominio + [("slug", "=", slug)], limit=1)
        if not instance:
            snapped = self.env["primate.cloud.twin.snapshot"].sudo().search(
                [("environment_id", "=", env_rec.id)]).mapped("instance_id")
            placeholders = Instance.search(dominio).filtered(
                lambda i: i not in snapped
                and (i.conf_path or "") not in twin_confs
                and not i.database_id)
            instance = placeholders[:1]
            if instance:
                instance.write({"slug": slug, "conf_path": conf_path,
                                "name": db_name or slug})
        if not instance:
            conf = data.get("conf_masked") or {}
            vals = {
                "name": db_name or slug,
                "environment_id": env_rec.id,
                "project_id": env_rec.project_id.id or self.env[
                    "ir.config_parameter"].sudo().get_param(
                    "pcm.unclassified_project_id", 0) or False,
                "slug": slug,
                "conf_path": conf_path,
                "state": "active",
            }
            if (conf.get("http_port") or "").isdigit():
                vals["http_port"] = int(conf["http_port"])
            if (conf.get("gevent_port") or "").isdigit():
                vals["gevent_port"] = int(conf["gevent_port"])
            elif vals.get("http_port"):
                vals["gevent_port"] = vals["http_port"] + 3
            instance = Instance.create(vals)
        return instance

    def _twin_update_instance(self, instance, data, nginx):
        """Refleja en el registro lo observado (sync, no edición humana)."""
        conf = data.get("conf_masked") or {}
        vals = {"conf_path": data.get("conf_path")}
        if conf.get("http_port", "").isdigit():
            vals["http_port"] = int(conf["http_port"])
        if conf.get("gevent_port", "").isdigit():
            vals["gevent_port"] = int(conf["gevent_port"])
        if (data.get("service") or {}).get("unit"):
            vals["service_name"] = data["service"]["unit"]
        if (data.get("python") or {}).get("bin"):
            vals["python_bin"] = data["python"]["bin"]
        if conf.get("data_dir"):
            vals["data_dir"] = conf["data_dir"]
        # Dominio por nginx: el vhost cuyo proxy apunta al puerto http.
        port = str(vals.get("http_port") or instance.http_port or "")
        for site in nginx:
            if port and port in (site.get("proxy_ports") or []):
                nombre = (site.get("server_names") or "").split()
                nombre = [n for n in nombre if n and n != "_"]
                if nombre and not instance.main_url:
                    vals["main_url"] = nombre[0]
                for cert in site.get("certificates") or []:
                    if cert.get("not_after"):
                        vals["ssl_state"] = "active"
                break
        instance.write(vals)

    def _twin_sync_repos(self, instance, data):
        Repo = self.env["primate.cloud.repository"].with_context(
            pcm_import_sync=True)
        for repo in data.get("repos") or []:
            path = (repo.get("path") or "").rstrip("/")
            if not path:
                continue
            vals = {
                "github_url": repo.get("remote_url") or False,
                "configured_branch": repo.get("branch") or False,
                "current_commit": repo.get("commit_sha") or False,
                "local_changes_alert": bool(repo.get("alert_local_changes")),
                "local_changes_detail": _(
                    "%(d)s archivo(s) sin commitear, %(u)s commit(s) sin "
                    "pushear — una mudanza por git LOS PERDERÍA.",
                    d=repo.get("dirty_files") or 0,
                    u=repo.get("unpushed_commits") or 0)
                if repo.get("alert_local_changes") else False,
            }
            existing = Repo.search([
                ("instance_id", "=", instance.id),
                ("local_path", "=", path)], limit=1)
            if existing:
                existing.write(vals)
            else:
                Repo.create(dict(
                    vals, name=path.rsplit("/", 1)[-1],
                    environment_id=instance.environment_id.id,
                    instance_id=instance.id, local_path=path,
                    repo_type=self._twin_repo_type(repo)))

    @staticmethod
    def _twin_repo_type(repo):
        url = (repo.get("remote_url") or "").lower()
        if "github.com/oca/" in url or "github.com:oca/" in url:
            return "oca"
        if "odoo/enterprise" in url:
            return "odoo_enterprise"
        if "odoo/odoo" in url:
            return "odoo_core"
        return "custom_client"

    @staticmethod
    def _twin_alerts(data):
        alerts = []
        for repo in data.get("repos") or []:
            if repo.get("alert_local_changes"):
                alerts.append(_(
                    "Repo %(p)s: %(d)s archivo(s) modificados y %(u)s "
                    "commit(s) locales sin pushear — la mudanza por git los "
                    "perdería.", p=repo.get("path"),
                    d=repo.get("dirty_files") or 0,
                    u=repo.get("unpushed_commits") or 0))
        core = data.get("odoo_core") or {}
        if not core.get("commit_sha"):
            alerts.append(_("No se pudo leer el commit del core de Odoo."))
        if not (data.get("database") or {}).get("db_size_bytes"):
            alerts.append(_("No se pudo medir el tamaño de la base."))
        return alerts


class PrimateCloudEc2InstanceTwin(models.Model):
    _name = "primate.cloud.ec2.instance"
    _inherit = ["primate.cloud.ec2.instance", "primate.cloud.twin.mixin"]

    # Estado del último gemelo (2026-10-27): un gemelo que no llegó entero se
    # marca y se ve, nunca se pierde en silencio (antes la importación dejaba
    # el servidor sin gemelo y sin decir nada).
    twin_status = fields.Selection(
        [("none", "Sin gemelo"), ("ok", "Completo"),
         ("incomplete", "INCOMPLETO")],
        string="Gemelo", default="none", readonly=True, copy=False)
    twin_status_detail = fields.Char(
        string="Detalle del gemelo", readonly=True, copy=False)
    twin_status_date = fields.Datetime(
        string="Último intento de gemelo", readonly=True, copy=False)

    def _twin_marcar(self, estado, detalle=""):
        """Deja el estado del gemelo; si quedó incompleto, también en el
        chatter (visible, con el motivo)."""
        self.ensure_one()
        self.with_context(pcm_import_sync=True).sudo().write({
            "twin_status": estado, "twin_status_detail": (detalle or "")[:500],
            "twin_status_date": fields.Datetime.now()})
        if estado == "incomplete":
            self.sudo().message_post(body=_(
                "Gemelo INCOMPLETO: %s") % (detalle or _("sin detalle")))

    @api.model
    def _twin_parse_seguro(self, stdout):
        """(gemelo, None) si la salida trae el gemelo entero; (None, motivo)
        si no: falta PCM_TWIN_OK (salida cortada) o el blob no decodifica."""
        stdout = stdout or ""
        largo = len(stdout)
        if "PCM_TWIN_B64:" not in stdout:
            return None, _("la salida no trae el gemelo (%s caracteres).") % largo
        if "PCM_TWIN_OK" not in stdout:
            return None, _(
                "falta PCM_TWIN_OK: la salida se cortó (%s caracteres; el "
                "límite inline de SSM es 24.000). Capturarlo por ssh "
                "(infra/gemelo_cloudpepper.sh) o con salida por S3.") % largo
        try:
            return self._twin_parse(stdout), None
        except Exception as error:  # noqa: BLE001 — se reporta, no se traga
            return None, _("el gemelo no decodifica: %s") % str(error)[:200]

    def action_capture_twin(self):
        """Captura el gemelo de ESTE servidor (lectura pura: permitido también
        —y sobre todo— en importados). Si no llega entero, queda marcado
        INCOMPLETO con el motivo, en vez de fallar sin rastro."""
        self.ensure_one()
        try:
            twin = self._twin_fetch()
        except UserError as error:
            self._twin_marcar("incomplete", str(error))
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "warning", "sticky": True,
                           "message": _("Gemelo INCOMPLETO: %s") % error}}
        snapshots = self._twin_apply(twin)
        self.message_post(body=_(
            "Gemelo capturado: %(n)s instancia(s) Odoo, %(a)s alerta(s).",
            n=len(snapshots), a=sum(snapshots.mapped("alert_count"))))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "success", "sticky": False,
                       "message": _("Gemelo capturado: %s instancia(s), "
                                    "%s alerta(s).") % (
                           len(snapshots),
                           sum(snapshots.mapped("alert_count")))},
        }

    def _twin_fetch(self):
        """Corre el discovery de SOLO LECTURA y devuelve el JSON del gemelo."""
        self.ensure_one()
        ssm = aws_ssm.AwsSsmService(self.account_id._get_aws_service())
        region = self.region or self.account_id.default_region
        if self.management_state == "adopted_readonly":
            output = self._run_readonly_document(ssm, region, "pcm twin")
            document = output.get("document")
            if output.get("status") != "Success":
                raise UserError(_(
                    "El discovery de solo lectura falló (%(st)s). En un "
                    "servidor IMPORTADO solo puede correr el documento SSM "
                    "«%(doc)s»: verificá que exista en la cuenta y que el "
                    "servidor tenga el agente SSM (checklist de la mudanza).",
                    st=output.get("status"), doc=document))
        else:
            with file_open(DISCOVERY_SCRIPT_PATH) as handle:
                script = handle.read()
            output = ssm.run_script(
                self.aws_instance_id, script, region=region,
                comment="pcm twin: %s" % self.name,
                timeout=420, check=False)
            if output.get("status") != "Success":
                raise UserError(_("El discovery del gemelo falló: %s")
                                % (output.get("stderr")
                                   or output.get("status")))
        return self._twin_parse(output.get("stdout") or "")

    def _run_readonly_document(self, ssm, region, comment):
        """El ÚNICO camino de lectura dentro de un servidor importado."""
        params = self.env["ir.config_parameter"].sudo()
        document = params.get_param(DISCOVERY_DOCUMENT_PARAM,
                                     DISCOVERY_DOCUMENT_DEFAULT)
        bucket = params.get_param(DISCOVERY_S3_BUCKET_PARAM) or None
        output = ssm.run_document(
            self.aws_instance_id, document, region=region,
            comment="%s: %s" % (comment, self.name), timeout=600,
            output_s3_bucket=bucket,
            output_s3_prefix="pcm-readonly-discovery" if bucket else None)
        return dict(output, document=document)

    @staticmethod
    def _twin_parse(stdout):
        chunks = [line[len("PCM_TWIN_B64:"):] for line in stdout.splitlines()
                  if line.startswith("PCM_TWIN_B64:")]
        if not chunks or "PCM_TWIN_OK" not in stdout:
            raise UserError(_(
                "El discovery no devolvió el gemelo completo: la salida "
                "inline de SSM se corta en 24.000 caracteres (servidor con "
                "muchas instancias) o el script se interrumpió. Configurá un "
                "bucket en «%s» para recibir la salida completa por S3.",
                DISCOVERY_S3_BUCKET_PARAM))
        blob = "".join(chunks)
        return json.loads(gzip.decompress(base64.b64decode(blob)).decode())

    def _twin_apply(self, twin):
        """El gemelo de ESTE servidor, volcado en su entorno."""
        self.ensure_one()
        if not self.environment_id:
            raise UserError(_("El servidor no tiene entorno en PCM."))
        snapshots = self._twin_apply_to(self.environment_id, twin, machine=self)
        self._twin_marcar("ok", _("%s instancia(s) Odoo.") % len(snapshots))
        return snapshots


class PrimateCloudEnvironmentTwin(models.Model):
    _name = "primate.cloud.environment"
    _inherit = ["primate.cloud.environment", "primate.cloud.twin.mixin"]

    def _twin_apply(self, twin):
        """Gemelo volcado directo en el entorno (sin máquina AWS)."""
        self.ensure_one()
        return self._twin_apply_to(self, twin, machine=None)
