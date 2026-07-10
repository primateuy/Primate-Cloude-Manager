# -*- coding: utf-8 -*-
"""Instancia EC2: inventario (Fase 2) y acciones de operación (Fase 3)."""
import base64
import hashlib
import json
import logging
import shlex
import time
import uuid

from odoo import _, SUPERUSER_ID, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from ..services import aws_ec2, aws_ssm
from ..tools import bus, dates

# --- Config del odoo.conf (Bloque B3) ---
CONFIG_READ_SCRIPT_PATH = "primate_cloud_manager/data/config_read.py"
CONFIG_APPLY_SCRIPT_PATH = "primate_cloud_manager/data/config_apply.py"
# Parámetros EDITABLES (los que fallan "suave": límites/comportamiento).
# tipo -> ("bool"|"int"|"enum", spec). Duplicado como capa 2 en config_apply.py.
CONFIG_EDITABLE = {
    "proxy_mode": ("bool", None),
    "list_db": ("bool", None),
    "workers": ("int", (0, 64)),
    "max_cron_threads": ("int", (0, 16)),
    "limit_time_cpu": ("int", (0, 86400)),
    "limit_time_real": ("int", (0, 86400)),
    "limit_request": ("int", (0, 2000000)),
    "limit_memory_soft": ("int", (0, 2 ** 40)),
    "limit_memory_hard": ("int", (0, 2 ** 40)),
    "log_level": ("enum", {"debug", "info", "warn", "error", "critical",
                           "debug_sql", "debug_rpc"}),
}
# READ-ONLY en v1: mal puestos IMPIDEN el arranque de Odoo (regla del usuario).
CONFIG_READONLY = [
    "db_host", "db_port", "db_user", "db_name", "data_dir",
    "http_port", "http_interface", "addons_path", "server_wide_modules",
]
# Health check post-restart: timeout GENEROSO (Odoo tarda en levantar) para no
# disparar un rollback innecesario. Poll cada HEALTH_POLL s hasta HEALTH_TIMEOUT.
CONFIG_HEALTH_TIMEOUT = 90
CONFIG_HEALTH_POLL = 5

# Dir estándar de addons de cliente (Bloque B4). Las instancias nuevas lo traen
# en el addons_path (install_odoo.sh); las viejas lo reciben retroactivamente al
# agregar el primer addon.
CUSTOM_ADDONS_DIR = "/opt/odoo/custom-addons"

_logger = logging.getLogger(__name__)

# Estados posibles devueltos por AWS (describe_instances → State.Name).
EC2_STATES = [
    ("pending", "Pendiente"),
    ("running", "En ejecución"),
    ("shutting-down", "Apagando"),
    ("stopping", "Deteniendo"),
    ("stopped", "Detenida"),
    ("terminated", "Terminada"),
]


class PrimateCloudEc2Instance(models.Model):
    """Instancia EC2 importada desde AWS y operable desde Odoo."""

    _name = "primate.cloud.ec2.instance"
    _description = "Instancia EC2"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, help="Tag Name en AWS.")
    # account_id es obligatorio: el inventario importa instancias que todavía no
    # están asociadas a ningún entorno. El entorno se vincula manualmente luego.
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS",
        required=True,
        ondelete="cascade",
        index=True,
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno",
        ondelete="set null",
        index=True,
    )
    aws_instance_id = fields.Char(string="Instance ID", required=True, index=True)
    instance_state = fields.Selection(EC2_STATES, string="Estado", tracking=True)
    # region como Char: el inventario debe aceptar cualquier región sin fallar.
    region = fields.Char(string="Región")
    instance_type = fields.Char(string="Tipo de instancia")
    public_ip = fields.Char(string="IP pública")
    private_ip = fields.Char(string="IP privada")
    os_type = fields.Selection(
        [
            ("ubuntu_22", "Ubuntu 22.04"),
            ("ubuntu_24", "Ubuntu 24.04"),
            ("amazon_linux", "Amazon Linux"),
            ("other", "Otro"),
        ],
        string="Sistema operativo",
    )
    disk_size_gb = fields.Integer(string="Disco raíz (GB)")
    aws_tags = fields.Text(string="Tags AWS", help="Tags de AWS serializados en JSON.")
    aws_created_at = fields.Datetime(string="Creada en AWS", readonly=True)
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True)

    # --- Métricas (cache de la última snapshot, Fase 9) ---
    last_cpu = fields.Float(string="CPU % (última)", readonly=True)
    last_status_check_failed = fields.Float(
        string="Status check (última)", readonly=True,
        help="≥1 = falló algún chequeo de estado de EC2 (señal de error real).",
    )
    last_metric_date = fields.Datetime(string="Métricas al", readonly=True)

    # True solo si la instancia fue APROVISIONADA por PCM (layout estándar de
    # install_odoo.sh garantizado). Para las importadas por sync queda en False:
    # el panel muestra "desconocido" en lugar de inventar paths (mismo criterio
    # ausente≠inventado que en métricas).
    provisioned_by_pcm = fields.Boolean(
        string="Aprovisionada por PCM", default=False, readonly=True,
        help="Marca si PCM creó la instancia (paths/comandos del panel válidos).",
    )

    # --- Runtime detectado on-demand por SSM (Fase panel, Bloque B1) ---
    runtime_python_version = fields.Char(string="Versión Python", readonly=True)
    runtime_odoo_version = fields.Char(string="Versión Odoo", readonly=True)
    runtime_workers = fields.Char(string="Workers", readonly=True)
    last_runtime_probe = fields.Datetime(string="Runtime detectado al", readonly=True)

    _aws_instance_uniq = models.Constraint(
        "UNIQUE(account_id, aws_instance_id)",
        "Esa instancia EC2 ya existe para la cuenta.",
    )

    # ------------------------------------------------------------------
    # Inventario / sincronización
    # ------------------------------------------------------------------
    @staticmethod
    def _aws_vals(data, now):
        """Construye el dict de campos comunes a partir de datos normalizados."""
        return {
            "name": data.get("name"),
            "instance_state": data.get("instance_state"),
            "region": data.get("region"),
            "instance_type": data.get("instance_type"),
            "public_ip": data.get("public_ip") or False,
            "private_ip": data.get("private_ip") or False,
            "aws_tags": json.dumps(data.get("tags") or {}, ensure_ascii=False),
            "aws_created_at": dates.to_naive_utc(data.get("created_at")),
            "last_sync_date": now,
        }

    def _sync_from_aws(self, account, instances):
        """Crea/actualiza instancias EC2 a partir de datos normalizados de AWS.

        Hace upsert por (cuenta, instance_id). No toca el vínculo con el entorno
        si ya existe (el inventario no pisa asociaciones manuales).

        Args:
            account (recordset): la cuenta AWS de origen.
            instances (list[dict]): salida de ``AwsEc2Service.list_instances``.

        Returns:
            dict: ``{"created": int, "updated": int}``.
        """
        now = fields.Datetime.now()
        created = updated = 0
        for data in instances:
            vals = self._aws_vals(data, now)
            existing = self.search(
                [
                    ("account_id", "=", account.id),
                    ("aws_instance_id", "=", data["aws_instance_id"]),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                vals.update(
                    {
                        "account_id": account.id,
                        "aws_instance_id": data["aws_instance_id"],
                    }
                )
                self.create(vals)
                created += 1
        return {"created": created, "updated": updated}

    def _register_provisioned(self, account, aws_data, environment=None, os_type=None, disk_size_gb=None):
        """Crea el registro de una instancia recién creada en AWS (Fase 4).

        Reutilizado por el wizard de creación de EC2 y por el flujo de
        aprovisionamiento. Hace upsert por (cuenta, instance_id) por si la
        instancia ya fue sincronizada antes de registrarse.

        Args:
            account (recordset): cuenta AWS dueña de la instancia.
            aws_data (dict): instancia normalizada (salida de ``create_instance``).
            environment (recordset, optional): entorno al que se asocia.
            os_type (str, optional): sistema operativo (campo informativo).
            disk_size_gb (int, optional): tamaño del disco raíz solicitado.

        Returns:
            recordset: la instancia creada o actualizada.
        """
        vals = self._aws_vals(aws_data, fields.Datetime.now())
        # La creó PCM: el panel puede confiar en el layout estándar de paths.
        vals["provisioned_by_pcm"] = True
        if environment:
            vals["environment_id"] = environment.id
        if os_type:
            vals["os_type"] = os_type
        if disk_size_gb:
            vals["disk_size_gb"] = disk_size_gb
        if aws_data.get("instance_type"):
            vals["instance_type"] = aws_data["instance_type"]
        existing = self.search(
            [
                ("account_id", "=", account.id),
                ("aws_instance_id", "=", aws_data["aws_instance_id"]),
            ],
            limit=1,
        )
        if existing:
            existing.write(vals)
            record = existing
        else:
            vals.update(
                {"account_id": account.id,
                 "aws_instance_id": aws_data["aws_instance_id"]}
            )
            record = self.create(vals)
        # R1 (D6): la máquina 1:1 del servidor. Se fija si estaba vacía o si
        # la anterior quedó terminada (re-provisión tras un fallo).
        if environment and (
            not environment.ec2_instance_id
            or environment.ec2_instance_id.instance_state == "terminated"
        ):
            environment.ec2_instance_id = record.id
        return record

    # ------------------------------------------------------------------
    # Helpers de servicios AWS
    # ------------------------------------------------------------------
    def _get_ec2_service(self):
        """Devuelve el adaptador EC2 autenticado con la cuenta de la instancia."""
        self.ensure_one()
        return aws_ec2.AwsEc2Service(self.account_id._get_aws_service())

    def _get_ssm_service(self):
        """Devuelve el adaptador SSM autenticado con la cuenta de la instancia."""
        self.ensure_one()
        return aws_ssm.AwsSsmService(self.account_id._get_aws_service())

    # Fuentes de log (spec §11.1) → comando de lectura. NINGUNO vuelca
    # credenciales ni el odoo.conf: solo se leen archivos/journal de log.
    _LOG_SOURCES = {
        "odoo": "journalctl -u odoo --no-pager",
        "nginx": "tail -n %(lines)s /var/log/nginx/error.log "
                 "/var/log/nginx/access.log 2>/dev/null",
        "postgres": "journalctl -u postgresql --no-pager 2>/dev/null || "
                    "tail -n %(lines)s /var/log/postgresql/*.log 2>/dev/null",
        "os": "journalctl --no-pager",
    }

    def fetch_logs(self, source, lines=200, since=None, until=None, grep=None):
        """Trae logs en vivo por SSM (on-demand, NO se persisten).

        Los comandos solo LEEN archivos/journal de log — nunca el odoo.conf ni
        variables con credenciales. El texto se devuelve para mostrar/descargar,
        no se guarda en BD.

        Args:
            source (str): ``odoo`` / ``nginx`` / ``postgres`` / ``os``.
            lines (int): cantidad de líneas (100/500/1000...).
            since/until (str, optional): rango para journalctl (``YYYY-MM-DD``).
            grep (str, optional): filtro de texto libre.

        Returns:
            dict: ``{"status": str, "text": str}``.
        """
        self.ensure_one()
        if source not in self._LOG_SOURCES:
            raise UserError(_("Fuente de log no soportada: %s.") % source)
        try:
            lines = int(lines)
        except (TypeError, ValueError):
            lines = 200
        base = self._LOG_SOURCES[source] % {"lines": lines}
        # journalctl acepta rango y --lines; los archivos ya traen -n arriba.
        if base.startswith("journalctl"):
            if since:
                base += " --since %s" % shlex.quote(since)
            if until:
                base += " --until %s" % shlex.quote(until)
            base += " -n %d" % lines
        if grep:
            base += " | grep -a -i -- %s" % shlex.quote(grep)
        # Cota dura de salida para no traer megabytes a la UI.
        command = "%s | tail -n %d" % (base, lines)
        output = self._get_ssm_service().run_script(
            self.aws_instance_id, command, region=self.region,
            comment="pcm logs: %s" % source, timeout=120)
        text = (output.get("stdout") or "") or (output.get("stderr") or "")
        return {"status": output.get("status") or "Unknown", "text": text}

    # --- Streaming incremental de logs (Bloque B2) ---
    # Fuentes de journal (usan cursor de journalctl) → unidad systemd.
    _JOURNAL_STREAM = {"odoo": "-u odoo", "postgres": "-u postgresql", "os": ""}
    # Fuentes de archivo (usan offset de bytes como cursor).
    _FILE_STREAM = {"nginx": "/var/log/nginx/error.log"}

    def fetch_logs_stream(self, source, from_cursor=False, lines=200, grep=None):
        """Trae SOLO lo nuevo desde ``from_cursor`` (streaming incremental por SSM).

        Lectura interactiva read-only y ACOTADA → corre síncrona con timeout
        corto (política de lecturas interactivas de CLAUDE.md), no por queue_job.
        Igual que ``fetch_logs``: nunca toca el odoo.conf ni credenciales.

        El cursor es OPACO para el front (lo reenvía en el próximo poll): en
        journal es el cursor de journalctl; en archivos, el offset de bytes leído.
        (OJO: el parámetro NO puede llamarse ``cursor`` — ``_()`` inspecciona los
        locales y toma un ``cursor`` como si fuese el cursor de BD → rompe.)

        Returns:
            dict: ``{"status": str, "text": str, "cursor": str|bool}`` con solo
            las líneas nuevas (``text`` vacío si no hubo novedad).
        """
        self.ensure_one()
        if source not in self._LOG_SOURCES:
            raise UserError(_("Fuente de log no soportada: %s.") % source)
        try:
            lines = int(lines)
        except (TypeError, ValueError):
            lines = 200
        journal = source in self._JOURNAL_STREAM
        cmd = (self._journal_stream_cmd(source, from_cursor, lines, grep) if journal
               else self._file_stream_cmd(source, from_cursor, lines, grep))
        output = self._get_ssm_service().run_script(
            self.aws_instance_id, cmd, region=self.region,
            comment="pcm logs stream: %s" % source, timeout=45, agent_timeout=20)
        text = output.get("stdout") or ""
        status = output.get("status") or "Unknown"
        return (self._parse_journal_stream(text, status, from_cursor) if journal
                else self._parse_file_stream(text, status))

    def _journal_stream_cmd(self, source, from_cursor, lines, grep):
        """Comando journalctl que trae lo nuevo tras el cursor + muestra el nuevo."""
        base = ("journalctl %s --no-pager -o short-iso --show-cursor"
                % self._JOURNAL_STREAM[source]).strip()
        if from_cursor:
            base += " --after-cursor %s" % shlex.quote(from_cursor)
        else:
            base += " -n %d" % lines
        if grep:
            # Filtra el contenido pero CONSERVA la línea de cursor final.
            base += " | grep -a -i -e '^-- cursor:' -e %s" % shlex.quote(grep)
        return base

    def _file_stream_cmd(self, source, from_cursor, lines, grep):
        """Comando tail por offset de bytes; emite el tamaño actual como cursor."""
        path = shlex.quote(self._FILE_STREAM[source])
        if from_cursor:
            body = "tail -c +%d %s 2>/dev/null" % (int(from_cursor) + 1, path)
        else:
            body = "tail -n %d %s 2>/dev/null" % (lines, path)
        if grep:
            body += " | grep -a -i -- %s" % shlex.quote(grep)
        return '%s; echo "PCM_OFFSET:$(wc -c < %s 2>/dev/null || echo 0)"' % (body, path)

    @staticmethod
    def _parse_journal_stream(text, status, prev_cursor):
        """Separa las líneas nuevas del cursor final (``-- cursor: ...``)."""
        new_cursor = prev_cursor
        kept = []
        for line in text.splitlines():
            if line.startswith("-- cursor:"):
                new_cursor = line.split(":", 1)[1].strip()
            elif line.strip() in ("-- No entries --", ""):
                continue
            else:
                kept.append(line)
        return {"status": status, "text": "\n".join(kept), "cursor": new_cursor}

    @staticmethod
    def _parse_file_stream(text, status):
        """Separa las líneas nuevas del marcador de offset (``PCM_OFFSET:``)."""
        cursor = False
        kept = []
        for line in text.splitlines():
            if line.startswith("PCM_OFFSET:"):
                cursor = line.split(":", 1)[1].strip()
            else:
                kept.append(line)
        return {"status": status, "text": "\n".join(kept), "cursor": cursor}

    # --- Configuración del odoo.conf (Bloque B3) ---
    def _render_config_script(self, path, tokens):
        """Lee un script remoto de ``data/`` y reemplaza sus tokens ``%%...%%``."""
        with file_open(path, "r") as handle:
            script = handle.read()
        for key, value in tokens.items():
            script = script.replace("%%%%%s%%%%" % key, str(value))
        return script

    @staticmethod
    def _python_heredoc(script_body):
        """Corre un script Python por SSM como root, sin dejar archivo."""
        return "sudo python3 - <<'PCM_PYEOF'\n%s\nPCM_PYEOF" % script_body

    @staticmethod
    def _config_marker(stdout, prefix):
        """Primer valor de un marcador ``PCM_*:`` en el stdout."""
        for line in (stdout or "").splitlines():
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return ""

    @staticmethod
    def _config_markers(stdout, prefix):
        """Todos los ``PCM_*:clave=valor`` de un prefijo → dict clave→valor."""
        result = {}
        for line in (stdout or "").splitlines():
            if line.startswith(prefix):
                key, _sep, value = line[len(prefix):].partition("=")
                result[key.strip()] = value
        return result

    @staticmethod
    def _config_field_meta():
        """Metadata de los campos editables para que el front arme los widgets."""
        meta = []
        for key, (kind, spec) in CONFIG_EDITABLE.items():
            entry = {"key": key, "type": kind}
            if kind == "enum":
                entry["options"] = sorted(spec)
            elif kind == "int":
                entry["min"], entry["max"] = spec
            meta.append(entry)
        return meta

    def _parse_config_read(self, stdout):
        """Separa la salida de config_read en editables/read-only + hash."""
        editable, readonly = {}, {}
        config_hash = self._config_marker(stdout, "PCM_HASH:")
        for key, value in self._config_markers(stdout, "PCM_VAL:").items():
            if key in CONFIG_EDITABLE:
                editable[key] = value
            elif key in CONFIG_READONLY:
                readonly[key] = value
        return {"editable": editable, "readonly": readonly,
                "config_hash": config_hash}

    def fetch_config(self):
        """Lee el odoo.conf por SSM (allowlist + hash). Read-only, SÍNCRONO.

        Solo emite parámetros de la allowlist: ``db_password``/``admin_passwd``
        NUNCA se leen. Del archivo completo solo sale el hash (para el CAS de
        concurrencia), nunca el contenido.
        """
        self.ensure_one()
        if not self.provisioned_by_pcm:
            raise UserError(_(
                "La configuración solo se gestiona en instancias aprovisionadas "
                "por PCM (paths conocidos)."))
        script = self._render_config_script(CONFIG_READ_SCRIPT_PATH, {})
        output = self._get_ssm_service().run_script(
            self.aws_instance_id, self._python_heredoc(script),
            region=self.region, comment="pcm config read", timeout=45,
            agent_timeout=20)
        return self._parse_config_read(output.get("stdout") or "")

    def _validate_config(self, edits, internal_keys=frozenset()):
        """Valida/castea los edits contra la allowlist (pura, testeable).

        Devuelve ``{clave: valor_str}`` limpio o levanta ``UserError``. Un valor
        que no castea (p. ej. ``limit_time_cpu = "abc"``) NUNCA llega al archivo.
        ``internal_keys`` = claves que un flujo interno de PCM (addon-add sobre
        ``addons_path``) habilita además de la allowlist de usuario; NUNCA vienen
        del editor de usuario (``action_save_config`` no las pasa).
        """
        clean = {}
        for key, value in edits.items():
            if key in internal_keys:
                if not str(value).strip():
                    raise UserError(_("Valor vacío para %s.") % key)
                clean[key] = str(value)
                continue
            if key not in CONFIG_EDITABLE:
                raise UserError(_("Parámetro no editable: %s.") % key)
            kind, spec = CONFIG_EDITABLE[key]
            if kind == "bool":
                text = str(value)
                if isinstance(value, bool) or text in (
                        "True", "False", "true", "false", "1", "0"):
                    clean[key] = ("True" if (value is True or text in
                                  ("True", "true", "1")) else "False")
                else:
                    raise UserError(_("%s debe ser booleano.") % key)
            elif kind == "int":
                try:
                    num = int(str(value).strip())
                except (TypeError, ValueError):
                    raise UserError(_("%s debe ser un entero.") % key)
                low, high = spec
                if not low <= num <= high:
                    raise UserError(_(
                        "%(k)s fuera de rango [%(lo)s, %(hi)s].",
                        k=key, lo=low, hi=high))
                clean[key] = str(num)
            elif kind == "enum":
                if str(value) not in spec:
                    raise UserError(_("Valor inválido para %s.") % key)
                clean[key] = str(value)
        if "limit_memory_soft" in clean and "limit_memory_hard" in clean:
            if int(clean["limit_memory_hard"]) < int(clean["limit_memory_soft"]):
                raise UserError(_(
                    "limit_memory_hard debe ser ≥ limit_memory_soft."))
        return clean

    def action_save_config(self, edits, expected_hash, typed_name=None):
        """Valida, exige confirmación en prod y encola el guardado (reinicia)."""
        self.ensure_one()
        if not self.provisioned_by_pcm:
            raise UserError(_("Solo se edita la config de instancias PCM."))
        clean = self._validate_config(edits)
        if not clean:
            raise UserError(_("No hay cambios para guardar."))
        if not expected_hash:
            raise UserError(_(
                "Falta la referencia del archivo; recargá la configuración."))
        environment = self.environment_id
        if environment and environment.env_type == "production":
            if (typed_name or "").strip() != (environment.name or "").strip():
                raise UserError(_(
                    "Editar la configuración en PRODUCCIÓN reinicia Odoo (corte "
                    "de servicio). Escribí el nombre exacto del entorno para "
                    "confirmar."))
        self.with_delay(
            description=_("Config EC2: %s") % self.name
        ).job_save_config(clean, expected_hash)
        return self._notify(_(
            "Guardado de configuración encolado (reinicia Odoo unos segundos)."))

    def job_save_config(self, edits, expected_hash, internal_keys=()):
        """Job: aplica los cambios, reinicia, verifica salud y hace rollback si falla.

        ``internal_keys`` habilita claves vouched por un flujo interno de PCM
        (p. ej. ``addons_path`` en addon-add) — reusa toda la máquina de B3
        (preservación/backup/CAS/restart/health/rollback) sin duplicarla.
        """
        self.ensure_one()
        title = _("Editar config: %s") % self.name
        ssm = self._get_ssm_service()
        edits_b64 = base64.b64encode(
            json.dumps(edits).encode("utf-8")).decode("ascii")
        apply_script = self._render_config_script(
            CONFIG_APPLY_SCRIPT_PATH,
            {"EDITS_B64": edits_b64, "EXPECTED_HASH": expected_hash,
             "EXTRA_ALLOW": ",".join(internal_keys)})
        out = ssm.run_script(
            self.aws_instance_id, self._python_heredoc(apply_script),
            region=self.region, comment="pcm config apply", timeout=60,
            agent_timeout=30)
        stdout = out.get("stdout") or ""
        result = self._config_marker(stdout, "PCM_RESULT:")

        if result == "stale":
            self._log("config_edit", result="failed", name=title,
                      error_message=_("El archivo cambió externamente; no se "
                                      "aplicó nada."))
            self._notify_config_done(False, _(
                "La configuración cambió por fuera de PCM; recargá y reintentá."))
            return "stale"
        if result == "invalid":
            bad = self._config_marker(stdout, "PCM_ERROR:")
            self._log("config_edit", result="failed", name=title,
                      error_message=_("Valor inválido: %s") % bad)
            self._notify_config_done(False, _(
                "Valor inválido (%s); no se aplicó.") % bad)
            return "invalid"
        if result != "applied":
            self._log("config_edit", result="failed", name=title,
                      error_message=(stdout or "")[:500])
            self._notify_config_done(False, _(
                "No se pudo aplicar la configuración."))
            return "error"

        backup = self._config_marker(stdout, "PCM_BAK:")
        port = self._config_marker(stdout, "PCM_HTTP_PORT:") or "8069"
        # ¿Odoo servía HTTP local ANTES del cambio? (capturado por config_apply
        # con la config vieja aún corriendo). Decide degraded vs failed.
        http_was_ok = self._config_marker(stdout, "PCM_HTTP_WAS:") == "ok"
        old_values = self._config_markers(stdout, "PCM_OLD:")
        diff = self._config_diff_text(old_values, edits)

        # Reinicio + health check con timeout generoso.
        ssm.run_script(self.aws_instance_id, "sudo systemctl restart odoo",
                       region=self.region, comment="pcm config restart",
                       timeout=45, agent_timeout=20)
        health = self._config_health_check(port, http_was_ok=http_was_ok)

        if health == "failed":
            self._config_rollback(backup)
            self._log("config_edit", result="failed", name=title,
                      error_message=_(
                          "El reinicio dejó Odoo caído; se restauró la config "
                          "anterior.\nCambios intentados:\n%s") % diff)
            self._notify_config_done(False, _(
                "El cambio dejó Odoo sin arrancar; se restauró la config "
                "anterior y se reinició."))
            return "rolled_back"

        note = "" if health == "http" else _(
            " (Odoo activo; HTTP local no verificable — proxy/socket).")
        self._log("config_edit", result="success", name=title,
                  error_message=_("Cambios aplicados:\n%s") % diff)
        self._notify_config_done(True, _(
            "Configuración guardada y Odoo reiniciado.%s") % note)
        return "saved"

    def _config_health_check(self, port, http_was_ok=False, _sleep=time.sleep):
        """Sondea salud tras el restart. Devuelve 'http' | 'degraded' | 'failed'.

        - Odoo inactivo tras el timeout → 'failed' (rollback).
        - Activo + HTTP local OK → 'http'.
        - Activo pero SIN HTTP local: si ANTES del cambio servía
          (``http_was_ok``) el cambio ROMPIÓ el serving → 'failed' (rollback);
          si ya no servía (proxy_mode/socket) → 'degraded' (no rollback).

        Timeout generoso para no marcar falso-caído mientras Odoo levanta
        (``_sleep`` inyectable).
        """
        ssm = self._get_ssm_service()
        cmd = ("sudo systemctl is-active odoo | sed 's/^/PCM_ACTIVE:/'; "
               "curl -sf -m 3 http://127.0.0.1:%s/web/health >/dev/null 2>&1 "
               "&& echo PCM_HTTP:ok || echo PCM_HTTP:no") % port
        waited = 0
        last_active = False
        while True:
            out = (ssm.run_script(
                self.aws_instance_id, cmd, region=self.region,
                comment="pcm config health", timeout=20, agent_timeout=20
            ).get("stdout") or "")
            last_active = "PCM_ACTIVE:active" in out
            if last_active and "PCM_HTTP:ok" in out:
                return "http"
            if waited >= CONFIG_HEALTH_TIMEOUT:
                break
            _sleep(CONFIG_HEALTH_POLL)
            waited += CONFIG_HEALTH_POLL
        if not last_active:
            return "failed"
        # Activo pero sin HTTP: si antes servía, el cambio lo rompió → rollback.
        return "failed" if http_was_ok else "degraded"

    def _config_rollback(self, backup):
        """Restaura el backup del conf y reinicia (la instancia no queda caída)."""
        if not backup:
            return
        cmd = ("sudo mv %s /etc/odoo/odoo.conf "
               "&& sudo chown odoo:odoo /etc/odoo/odoo.conf "
               "&& sudo chmod 640 /etc/odoo/odoo.conf "
               "&& sudo systemctl restart odoo") % shlex.quote(backup)
        self._get_ssm_service().run_script(
            self.aws_instance_id, cmd, region=self.region,
            comment="pcm config rollback", timeout=45, agent_timeout=20)

    @staticmethod
    def _config_diff_text(old_values, new_values):
        """Diff legible ``clave: viejo → nuevo`` para la bitácora."""
        lines = []
        for key, new_value in new_values.items():
            old_value = old_values.get(key, "")
            lines.append("%s: %s → %s" % (
                key, old_value if old_value != "" else "(ausente)", new_value))
        return "\n".join(lines)

    def _notify_config_done(self, ok, message):
        """Avisa el resultado del guardado por el bus (job async)."""
        bus.toast(self.env, message,
                  ntype="success" if ok else "danger", sticky=not ok)
        return True

    # --- Addons de cliente (Bloque B4) ---
    def _ensure_custom_addons_path(self):
        """Garantiza el dir custom-addons + que esté en el addons_path.

        Retroactivo para instancias viejas: crea el dir si falta y, si el
        addons_path no lo incluye, lo agrega REUSANDO B3 (surgical edit + backup
        + restart + health + rollback), sin duplicar. Devuelve:
        'ready' (ya estaba / se creó) | 'restarted' (se editó addons_path y
        reinició) | 'rolled_back' (el cambio de addons_path no levantó) | 'error'.
        """
        self.ensure_one()
        ssm = self._get_ssm_service()
        ssm.run_script(
            self.aws_instance_id,
            "sudo mkdir -p %s && sudo chown odoo:odoo %s"
            % (shlex.quote(CUSTOM_ADDONS_DIR), shlex.quote(CUSTOM_ADDONS_DIR)),
            region=self.region, comment="pcm addons mkdir", timeout=45,
            agent_timeout=30)
        cfg = self.fetch_config()
        addons_path = cfg.get("readonly", {}).get("addons_path", "")
        parts = [p.strip() for p in addons_path.split(",") if p.strip()]
        if CUSTOM_ADDONS_DIR in parts:
            return "ready"
        new_path = ",".join(parts + [CUSTOM_ADDONS_DIR])
        outcome = self.job_save_config(
            {"addons_path": new_path}, cfg.get("config_hash"),
            internal_keys={"addons_path"})
        if outcome == "saved":
            return "restarted"
        if outcome == "rolled_back":
            return "rolled_back"
        return "error"

    @staticmethod
    def _build_addon_clone_script(url, path, ref, token_b64=None):
        """Script de clone por SSM. El token va por askpass temporal (NUNCA en la
        URL/git-config/argv), con trap que lo borra pase lo que pase."""
        qpath, qurl = shlex.quote(path), shlex.quote(url)
        branch = ("--branch %s " % shlex.quote(ref)) if ref else ""
        lines = ["set -e",
                 "mkdir -p %s" % shlex.quote(CUSTOM_ADDONS_DIR),
                 "rm -rf %s" % qpath]
        if token_b64:
            lines += [
                'TF=$(mktemp); AK=$(mktemp)',
                'trap \'rm -f "$TF" "$AK"\' EXIT',
                "printf '%%s' %s | base64 -d > \"$TF\"; chmod 600 \"$TF\""
                % shlex.quote(token_b64),
                'printf \'#!/bin/sh\\ncat "%s"\\n\' "$TF" > "$AK"; chmod 700 "$AK"',
                'GIT_ASKPASS="$AK" GIT_TERMINAL_PROMPT=0 git clone %s%s %s'
                % (branch, qurl, qpath),
            ]
        else:
            lines.append("GIT_TERMINAL_PROMPT=0 git clone %s%s %s"
                         % (branch, qurl, qpath))
        lines += ["chown -R odoo:odoo %s" % qpath,
                  "echo PCM_CLONE:ok"]
        return "\n".join(lines)

    def clone_addon(self, url, path, ref=None, token=None):
        """Clona un repo en la instancia (token seguro). Devuelve True si clonó."""
        self.ensure_one()
        token_b64 = (base64.b64encode(token.encode("utf-8")).decode("ascii")
                     if token else None)
        script = self._build_addon_clone_script(url, path, ref, token_b64)
        out = self._get_ssm_service().run_script(
            self.aws_instance_id, script, region=self.region,
            comment="pcm addon clone", timeout=180, agent_timeout=60)
        return "PCM_CLONE:ok" in (out.get("stdout") or "")

    def restart_odoo(self):
        """Reinicia Odoo (para cargar un addon recién clonado, sin cambio de conf)."""
        self.ensure_one()
        self._get_ssm_service().run_script(
            self.aws_instance_id, "sudo systemctl restart odoo",
            region=self.region, comment="pcm addon restart", timeout=45,
            agent_timeout=20)

    def addon_module_count(self, path):
        """Cuenta módulos (dirs con __manifest__.py) en el clone. 0 = sin módulos."""
        self.ensure_one()
        out = self._get_ssm_service().run_script(
            self.aws_instance_id,
            "ls %s/*/__manifest__.py 2>/dev/null | wc -l" % shlex.quote(path),
            region=self.region, comment="pcm addon modules", timeout=45,
            agent_timeout=30)
        try:
            return int((out.get("stdout") or "0").strip().split("\n")[0])
        except (ValueError, IndexError):
            return 0

    # --- Login as / impersonación (Bloque B5) ---
    def _impersonate_base_url(self):
        """URL base del Odoo del cliente (dominio https, o IP pública http)."""
        self.ensure_one()
        env = self.environment_id
        if env and env.main_url:
            return "https://%s" % env.main_url.rstrip("/")
        return "http://%s" % (self.public_ip or "")

    def _make_impersonate_token(self, db, uid, login, admin_ack=False):
        """Arma y FIRMA (Ed25519) el token de un solo uso, corto (60 s)."""
        self.ensure_one()
        payload = {
            "db": db, "uid": int(uid), "login": login,
            "env_id": self.environment_id.id,
            "exp": time.time() + 60,
            "nonce": uuid.uuid4().hex,
            "admin": self.env.user.login,
        }
        if admin_ack:
            payload["admin_ack"] = True
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = self.account_id.sign_impersonate_token(payload_bytes)

        def b64url(raw):
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return "%s.%s" % (b64url(payload_bytes), b64url(signature))

    def list_db_users(self, db):
        """Lista usuarios INTERNOS activos de una BD remota (SSM psql, sin creds).

        Devuelve ``[{id, login, name, is_admin}]``. El admin se marca (impersonar
        un rol de administración exige un paso extra en la UI + en el token).
        """
        self.ensure_one()
        sql = (
            "SELECT u.id, u.login, COALESCE(pp.name, u.login), "
            "EXISTS(SELECT 1 FROM res_groups_users_rel r "
            "JOIN ir_model_data d ON d.model='res.groups' AND d.res_id=r.gid "
            "AND d.module='base' AND d.name='group_system' WHERE r.uid=u.id) "
            "FROM res_users u JOIN res_partner pp ON pp.id=u.partner_id "
            "WHERE u.active AND NOT u.share AND u.login NOT IN ('__system__') "
            "ORDER BY u.login")
        out = self._get_ssm_service().run_script(
            self.aws_instance_id,
            "sudo -u postgres psql -d %s -tAF'|' -c %s"
            % (shlex.quote(db), shlex.quote(sql)),
            region=self.region, comment="pcm impersonate users", timeout=45,
            agent_timeout=20)
        users = []
        for line in (out.get("stdout") or "").splitlines():
            parts = line.split("|")
            if len(parts) >= 4 and parts[0].strip().isdigit():
                users.append({
                    "id": int(parts[0]), "login": parts[1],
                    "name": parts[2], "is_admin": parts[3].strip() == "t",
                })
        return users

    def action_login_as(self, db, uid, login, is_admin_target=False,
                        admin_ack=False, typed_name=None):
        """Genera el enlace de impersonación con toda la fricción + auditoría.

        Solo ``group_cloud_admin``. Prod → tipear el nombre del entorno. Destino
        admin → paso extra (``admin_ack``). AUDITA SIEMPRE (aunque no se complete
        el redirect). Devuelve ``{"url": ...}``.
        """
        self.ensure_one()
        if not self.env.user.has_group(
                "primate_cloud_manager.group_cloud_admin"):
            raise UserError(_("Solo un administrador cloud puede impersonar."))
        environment = self.environment_id
        is_prod = bool(environment and environment.env_type == "production")
        if is_prod and (typed_name or "").strip() != (
                environment.name or "").strip():
            raise UserError(_(
                "Impersonar en PRODUCCIÓN: escribí el nombre exacto del entorno "
                "para confirmar."))
        if is_admin_target and not admin_ack:
            raise UserError(_(
                "El usuario destino tiene rol de administración: requiere una "
                "confirmación extra explícita."))
        # Auditoría SIEMPRE (quién → a quién, BD, entorno, prod, cuándo).
        self._log("impersonate", result="success",
                  name=_("Login as %s") % login,
                  error_message=_(
                      "%(who)s → %(whom)s (uid %(uid)s)%(admin)s | BD %(db)s | "
                      "entorno %(env)s%(prod)s") % {
                      "who": self.env.user.login, "whom": login, "uid": uid,
                      "admin": _(" [ADMIN]") if is_admin_target else "",
                      "db": db, "env": environment.name if environment else "—",
                      "prod": _(" | PRODUCCIÓN") if is_prod else ""})
        url = "%s/pcm/impersonate?token=%s" % (
            self._impersonate_base_url(),
            self._make_impersonate_token(db, uid, login,
                                         admin_ack=is_admin_target and admin_ack))
        return {"url": url}

    # --- Deploy / habilitar / kill-switch del companion (Bloque B5) ---
    def _companion_tar_b64(self):
        """Empaqueta el addon companion (tar.gz base64) para enviarlo por SSM."""
        import io
        import tarfile
        from odoo.tools import file_path
        src = file_path("primate_cloud_manager/companion/pcm_impersonate")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(src, arcname="pcm_impersonate")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _odoo_shell_heredoc(self, db, script):
        """Comando SSM que corre un script Python por ``odoo-bin shell`` en una BD."""
        return ("sudo -u odoo /opt/odoo/venv/bin/python3 "
                "/opt/odoo/odoo/odoo-bin shell -c /etc/odoo/odoo.conf -d %s "
                "--no-http <<'PCM_SHELL_EOF'\n%s\nPCM_SHELL_EOF"
                % (shlex.quote(db), script))

    def deploy_impersonate(self, dbs, pcm_ip=None):
        """Despliega el companion: envía el addon, lo instala por BD, escribe la
        clave PÚBLICA y (opcional) restringe el endpoint a la IP de PCM en nginx.
        NO lo habilita (eso es un acto explícito aparte)."""
        self.ensure_one()
        ssm = self._get_ssm_service()
        self._ensure_custom_addons_path()
        quoted = shlex.quote(CUSTOM_ADDONS_DIR)
        ssm.run_script(
            self.aws_instance_id,
            "printf '%%s' '%s' | base64 -d | sudo tar xz -C %s "
            "&& sudo chown -R odoo:odoo %s/pcm_impersonate"
            % (self._companion_tar_b64(), quoted, quoted),
            region=self.region, comment="pcm impersonate deploy", timeout=120,
            agent_timeout=30)
        pubkey = self.account_id.impersonate_public_key()
        odoo_bin = ("sudo -u odoo /opt/odoo/venv/bin/python3 "
                    "/opt/odoo/odoo/odoo-bin -c /etc/odoo/odoo.conf")
        for db in dbs:
            ssm.run_script(
                self.aws_instance_id,
                "%s -d %s -i pcm_impersonate --stop-after-init"
                % (odoo_bin, shlex.quote(db)),
                region=self.region, comment="pcm impersonate install",
                timeout=300, agent_timeout=30)
            self._impersonate_set_param(db, "pcm.impersonate.public_key", pubkey)
        if pcm_ip:
            self._nginx_allowlist(pcm_ip)
        self.restart_odoo()

    def _impersonate_set_param(self, db, key, value):
        """Setea un ir.config_parameter en una BD remota (valor por base64)."""
        val_b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        script = ("import base64\n"
                  "env['ir.config_parameter'].sudo().set_param(%r, "
                  "base64.b64decode('%s').decode())\n"
                  "env.cr.commit()\n" % (key, val_b64))
        self._get_ssm_service().run_script(
            self.aws_instance_id, self._odoo_shell_heredoc(db, script),
            region=self.region, comment="pcm impersonate param", timeout=120,
            agent_timeout=30)

    def set_impersonate_enabled(self, dbs, enabled):
        """Habilita o DESHABILITA la impersonación. Al deshabilitar sube el epoch:
        eso corta también las sesiones de soporte YA abiertas (kill-switch)."""
        self.ensure_one()
        for db in dbs:
            bump = ("" if enabled else
                    "try:\n ep=int(p.get_param('pcm.impersonate.epoch','0'))\n"
                    "except Exception:\n ep=0\n"
                    "p.set_param('pcm.impersonate.epoch', str(ep+1))\n")
            script = ("p = env['ir.config_parameter'].sudo()\n"
                      "p.set_param('pcm.impersonate.enabled', %r)\n%s"
                      "env.cr.commit()\n"
                      % ("True" if enabled else "False", bump))
            self._get_ssm_service().run_script(
                self.aws_instance_id, self._odoo_shell_heredoc(db, script),
                region=self.region, comment="pcm impersonate enable",
                timeout=120, agent_timeout=30)

    def _nginx_allowlist(self, pcm_ip):
        """Restringe /pcm/impersonate a la IP de egreso de PCM (allow-list)."""
        script = (
            "import glob, subprocess\n"
            "IP = %r\n"
            "block = ('    location = /pcm/impersonate {\\n'\n"
            "         '        allow ' + IP + ';\\n        deny all;\\n'\n"
            "         '        proxy_pass http://127.0.0.1:8069;\\n'\n"
            "         '        proxy_set_header Host $host;\\n'\n"
            "         '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\\n'\n"
            "         '        proxy_set_header X-Forwarded-Proto $scheme;\\n    }\\n\\n')\n"
            "for f in glob.glob('/etc/nginx/sites-enabled/*'):\n"
            "    if f.endswith('/default'): continue\n"
            "    s = open(f).read()\n"
            "    if '/pcm/impersonate' in s: continue\n"
            "    s = s.replace('    location / {', block + '    location / {', 1)\n"
            "    open(f, 'w').write(s)\n"
            "subprocess.run(['nginx','-t'], check=True)\n"
            "subprocess.run(['systemctl','reload','nginx'], check=True)\n"
            "print('PCM_NGINX:ok')\n" % pcm_ip)
        self._get_ssm_service().run_script(
            self.aws_instance_id, self._python_heredoc(script),
            region=self.region, comment="pcm nginx allowlist", timeout=60,
            agent_timeout=30)

    def _log(self, action_type, result="success", error_message=None, aws_request_id=None, name=None):
        """Atajo para registrar en la bitácora sobre esta instancia."""
        return self.env["primate.cloud.operation.log"].log_operation(
            action_type,
            name=name,
            record=self,
            result=result,
            error_message=error_message,
            aws_request_id=aws_request_id,
        )

    # ------------------------------------------------------------------
    # Acciones de ciclo de vida (botón -> job). Toda llamada AWS va async.
    # ------------------------------------------------------------------
    def action_start(self):
        """Arranca la(s) instancia(s). Sin confirmación."""
        return self._enqueue_lifecycle("ec2_start")

    def action_stop(self):
        """Detiene (confirmación vía atributo confirm en la vista)."""
        return self._enqueue_lifecycle("ec2_stop")

    def action_restart(self):
        """Reinicia (reboot in situ)."""
        return self._enqueue_lifecycle("ec2_restart")

    def action_terminate(self):
        """Termina la(s) instancia(s). Operación destructiva: solo admin + wizard.

        Se valida el grupo aquí también (defensa en profundidad), además de la
        restricción de la vista/wizard.
        """
        if self.env.uid != SUPERUSER_ID and not self.env.user.has_group(
            "primate_cloud_manager.group_cloud_admin"
        ):
            raise UserError(_("Solo un Cloud Admin puede terminar instancias."))
        return self._enqueue_lifecycle("ec2_terminate")

    def _enqueue_lifecycle(self, action_type):
        """Patrón botón→job: NO cambia el estado; encola y avisa "por favor espere".

        El estado NO se toca acá: se actualiza recién cuando el job termina y
        consulta el estado real en AWS. Al terminar, el job notifica al navegador
        por el bus (ver job_lifecycle → _notify_user_done) para refrescar la vista
        y mostrar el resultado o el error. Mientras tanto, se muestra un cartel de
        "procesando, por favor espere".
        """
        spec = self._LIFECYCLE[action_type]
        for instance in self:
            instance.with_delay(
                description=_("%(act)s EC2: %(name)s")
                % {"act": spec["label"], "name": instance.name}
            ).job_lifecycle(action_type)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Procesando"),
                "message": _(spec["wait"]),
                "sticky": False,
            },
        }

    def action_execute_command(self):
        """Abre el wizard para ejecutar un comando vía SSM."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Ejecutar comando"),
            "res_model": "primate.cloud.ec2.command.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_instance_id": self.id},
        }

    def action_open_terminate_wizard(self):
        """Abre el wizard de terminación (doble confirmación)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Terminar instancia"),
            "res_model": "primate.cloud.ec2.terminate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_instance_id": self.id},
        }

    def action_create_staging_from_instance(self):
        """Abre el wizard de staging con esta instancia como origen (Bloque 5).

        Preselecciona el entorno, esta instancia y su BD si es única: el flujo
        "staging desde la instancia" del hub.
        """
        self.ensure_one()
        environment = self.environment_id
        if not environment:
            raise UserError(_("La instancia no pertenece a ningún entorno."))
        if environment.state != "active":
            raise UserError(_("Solo se puede crear un staging desde un "
                              "entorno activo."))
        context = {
            "default_origin_environment_id": environment.id,
            "default_origin_instance_id": self.id,
        }
        databases = (environment.database_ids.filtered(
            lambda d: d.ec2_instance_id == self) or environment.database_ids)
        if len(databases) == 1:
            context["default_origin_database_id"] = databases.id
        return {
            "type": "ir.actions.act_window",
            "name": _("Crear staging desde esta instancia"),
            "res_model": "primate.cloud.staging.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": context,
        }

    def action_sync_from_aws(self):
        """Encola la sincronización puntual de esta instancia desde AWS."""
        for instance in self:
            instance.with_delay(
                description=_("Sincronizar EC2: %s") % instance.name
            ).job_sync_from_aws()
        return self._notify(_("Sincronización encolada."))

    def action_refresh_metrics(self):
        """Encola un refresh on-demand de métricas de esta instancia."""
        self.ensure_one()
        self.with_delay(
            description=_("Métricas EC2: %s") % self.name
        ).job_snapshot_metrics()
        return self._notify(_("Actualización de métricas encolada."))

    def job_snapshot_metrics(self):
        """Job: toma una snapshot de métricas de esta instancia (CloudWatch)."""
        self.ensure_one()
        from ..services import aws_cloudwatch
        cw = aws_cloudwatch.AwsCloudWatchService(self.account_id._get_aws_service())
        self._take_metrics_snapshot(cw)
        return True

    # Sondeo de runtime (solo lectura). NO toca credenciales: el grep de workers
    # lee UNA línea del odoo.conf, jamás db_password/admin_passwd ni el archivo
    # entero. Los paths son los del layout PCM (install_odoo.sh) → exige
    # provisioned_by_pcm.
    _RUNTIME_PROBE = (
        'echo "PCM_PY:$(/opt/odoo/venv/bin/python3 --version 2>&1)"; '
        'echo "PCM_ODOO:$(sudo -u odoo /opt/odoo/venv/bin/python3 '
        '/opt/odoo/odoo/odoo-bin --version 2>&1)"; '
        "echo \"PCM_WORKERS:$(grep -E '^[[:space:]]*workers' "
        '/etc/odoo/odoo.conf 2>/dev/null | tail -1 | tr -d \'[:space:]\')"'
    )

    def action_probe_runtime(self):
        """Encola la detección de versiones (Python/Odoo) y workers por SSM."""
        self.ensure_one()
        if not self.provisioned_by_pcm:
            raise UserError(_(
                "Solo se puede detectar el runtime de instancias aprovisionadas "
                "por PCM (el layout de paths es conocido)."))
        if self.instance_state != "running":
            raise UserError(_("La instancia no está corriendo."))
        self.with_delay(
            description=_("Runtime EC2: %s") % self.name
        ).job_probe_runtime()
        return self._notify(_("Detección de runtime encolada."))

    def job_probe_runtime(self):
        """Job: lee versiones y workers por SSM y refresca el cache."""
        self.ensure_one()
        output = self._get_ssm_service().run_script(
            self.aws_instance_id, self._RUNTIME_PROBE, region=self.region,
            comment="pcm runtime probe", timeout=60, agent_timeout=30)
        self._parse_runtime(output.get("stdout") or "")
        return True

    @staticmethod
    def _runtime_marker(text, marker):
        """Devuelve el valor tras un marcador ``PCM_*:`` en el stdout."""
        for line in text.splitlines():
            if line.startswith(marker):
                return line[len(marker):].strip()
        return ""

    def _parse_runtime(self, stdout):
        """Parsea los marcadores del sondeo y escribe el cache de runtime."""
        self.ensure_one()
        py = self._runtime_marker(stdout, "PCM_PY:").replace("Python", "").strip()
        odoo = self._runtime_marker(stdout, "PCM_ODOO:")
        workers_raw = self._runtime_marker(stdout, "PCM_WORKERS:")
        # "workers=2" → "2"; sin línea workers en el conf ⇒ default de Odoo (0).
        workers = workers_raw.split("=", 1)[1] if "=" in workers_raw else "0"
        self.write({
            "runtime_python_version": py or False,
            "runtime_odoo_version": odoo or False,
            "runtime_workers": workers,
            "last_runtime_probe": fields.Datetime.now(),
        })

    def _take_metrics_snapshot(self, cw):
        """Consulta CloudWatch, crea la ``monitor.snapshot`` y refresca el cache.

        Un valor ``None`` de CloudWatch (métrica sin datos) NO se persiste como
        cero: se deja el campo sin escribir. RAM/disco NO se consultan (requieren
        agente CloudWatch; la UI las muestra como "requiere agente", no en cero).
        """
        self.ensure_one()
        if not self.aws_instance_id:
            return self.env["primate.cloud.monitor.snapshot"]
        from datetime import timedelta
        end = fields.Datetime.now()
        start = end - timedelta(hours=1)
        metrics = cw.get_ec2_metrics(self.aws_instance_id, self.region, start, end)
        # Solo se escriben las métricas con dato (None → no se persiste = NULL,
        # no cero). status_check: 0 es un valor real (chequeo OK), se escribe.
        vals = {"ec2_instance_id": self.id,
                "environment_id": self.environment_id.id}
        for key in ("cpu", "network_in", "network_out", "status_check_failed"):
            if metrics.get(key) is not None:
                vals[key] = metrics[key]
        snapshot = self.env["primate.cloud.monitor.snapshot"].sudo().create(vals)
        cache = {"last_metric_date": end}
        if metrics.get("cpu") is not None:
            cache["last_cpu"] = metrics["cpu"]
        if metrics.get("status_check_failed") is not None:
            cache["last_status_check_failed"] = metrics["status_check_failed"]
        self.write(cache)
        return snapshot

    # ------------------------------------------------------------------
    # Jobs (queue_job)
    # ------------------------------------------------------------------
    # Mapeo acción -> {método del servicio, etiqueta, carteles de espera/éxito}.
    _LIFECYCLE = {
        "ec2_start": {
            "method": "start_instance", "label": "Iniciar",
            "wait": "El servidor se está iniciando, por favor espere…",
            "done": "Servidor iniciado.",
        },
        "ec2_stop": {
            "method": "stop_instance", "label": "Detener",
            "wait": "El servidor se está deteniendo, por favor espere…",
            "done": "Servidor detenido.",
        },
        "ec2_restart": {
            "method": "reboot_instance", "label": "Reiniciar",
            "wait": "El servidor se está reiniciando, por favor espere…",
            "done": "Servidor reiniciado.",
        },
        "ec2_terminate": {
            "method": "terminate_instance", "label": "Terminar",
            "wait": "El servidor se está terminando, por favor espere…",
            "done": "Servidor terminado.",
        },
    }
    def job_lifecycle(self, action_type):
        """Job: ejecuta la acción, deja el estado real y avisa al navegador.

        Corre async (rápido: una sola llamada a AWS, sin polling). Recién al
        terminar consulta el estado real en AWS y lo escribe; luego emite un
        evento por el bus para que la vista se refresque y se muestre el resultado
        (o el motivo del error). Nunca se traga el error: queda en la bitácora.
        """
        self.ensure_one()
        spec = self._LIFECYCLE[action_type]
        try:
            service = self._get_ec2_service()
            request_id = getattr(service, spec["method"])(
                self.aws_instance_id, region=self.region
            )
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self._refresh_state_silently()
            self.message_post(
                body=_("Error al %(act)s: %(err)s")
                % {"act": spec["label"].lower(), "err": error}
            )
            self._log(action_type, result="failed", error_message=str(error),
                      name=_("%(act)s EC2: %(name)s") % {"act": spec["label"], "name": self.name})
            self._notify_user_done(action_type, ok=False, detail=str(error))
            return False

        # Reflejar el estado real que reporta AWS tras la acción.
        self._refresh_state_silently()
        self.message_post(body=_("Acción '%s' enviada a AWS correctamente.") % spec["label"])
        self._log(action_type, result="success", aws_request_id=request_id,
                  name=_("%(act)s EC2: %(name)s") % {"act": spec["label"], "name": self.name})
        self._notify_user_done(action_type, ok=True)
        return True

    def _notify_user_done(self, action_type, ok, detail=None):
        """Avisa al usuario que disparó la acción (por bus) que el job terminó.

        El front (ver static/src/js/pcm_notifier.js) muestra el toast y refresca
        la vista. En caso de error, el mensaje incluye el motivo.
        """
        spec = self._LIFECYCLE[action_type]
        if ok:
            message = _(spec["done"])
            title = _("Listo")
        else:
            message = _("Error al %(act)s «%(name)s»: %(err)s") % {
                "act": spec["label"].lower(), "name": self.name, "err": detail or "",
            }
            title = _("Error")
        bus.toast(self.env, message, title=title,
                  ntype="success" if ok else "danger", sticky=not ok, reload=True)

    def _refresh_state_silently(self):
        """Lee el estado real de la instancia en AWS y lo escribe (best-effort).

        Returns:
            bool: True si pudo consultar AWS; False si la consulta falló.
        """
        self.ensure_one()
        try:
            data = self._get_ec2_service().get_instance(
                self.aws_instance_id, region=self.region
            )
        except Exception:  # noqa: BLE001 - best-effort, no debe romper el job
            return False
        # Si AWS ya no la lista, quedó terminada.
        self.instance_state = data["instance_state"] if data else "terminated"
        return True

    def job_sync_from_aws(self):
        """Job: actualiza estado, IPs y tags de esta instancia desde AWS."""
        self.ensure_one()
        try:
            service = self._get_ec2_service()
            data = service.get_instance(self.aws_instance_id, region=self.region)
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Error al sincronizar: %s") % error)
            self._log("sync", result="failed", error_message=str(error))
            return False
        if not data:
            # La instancia ya no existe en AWS: se marca como terminada.
            self.instance_state = "terminated"
            self.message_post(body=_("La instancia ya no existe en AWS (terminada)."))
        else:
            self.write(self._aws_vals(data, fields.Datetime.now()))
        return True

    def job_execute_command(self, command):
        """Job: ejecuta un comando vía SSM y deja la salida en el chatter + log.

        El output se publica en el chatter de la instancia (no en vivo en pantalla):
        toda llamada SSM va por queue_job, así que la espera no bloquea la UI.
        """
        self.ensure_one()
        try:
            service = self._get_ssm_service()
            output = service.run_script(
                self.aws_instance_id,
                command,
                region=self.region,
                comment="pcm: %s" % (self.name or self.aws_instance_id),
            )
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Error al ejecutar comando: %s") % error)
            self._log("ssm_command", result="failed", error_message=str(error),
                      name=_("Comando SSM: %s") % self.name)
            return False

        ok = output.get("status") == "Success"
        body = _(
            "<b>Comando SSM</b> (estado: %(st)s)<br/>"
            "<b>$</b> <code>%(cmd)s</code><br/>"
            "<b>stdout:</b><pre>%(out)s</pre>"
            "%(err)s"
        ) % {
            "st": output.get("status"),
            "cmd": command,
            "out": (output.get("stdout") or "").strip() or "(vacío)",
            "err": (
                _("<b>stderr:</b><pre>%s</pre>") % output["stderr"].strip()
                if output.get("stderr")
                else ""
            ),
        }
        self.message_post(body=body)
        self._log(
            "ssm_command",
            result="success" if ok else "failed",
            error_message=None if ok else (output.get("stderr") or output.get("status")),
            aws_request_id=output.get("command_id"),
            name=_("Comando SSM: %s") % self.name,
        )
        bus.toast(
            self.env,
            _("Comando ejecutado (estado: %s). La salida quedó en el historial.")
            % output.get("status") if ok else
            _("El comando SSM falló: %s") % (output.get("stderr") or output.get("status")),
            title=_("Comando SSM: %s") % self.name,
            ntype="success" if ok else "danger", sticky=not ok, reload=True,
        )
        return ok

    # ------------------------------------------------------------------
    def _notify(self, message):
        """Notificación no bloqueante para los botones de acción."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info", "message": message, "next": {"type": "ir.actions.act_window_close"}},
        }
