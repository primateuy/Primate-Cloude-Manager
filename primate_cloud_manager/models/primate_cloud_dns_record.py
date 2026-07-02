# -*- coding: utf-8 -*-
"""Registro DNS en Route 53 (inventario en Fase 2, CRUD en Fase 8.5)."""
import logging
import re
import time

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services import aws_route53

_logger = logging.getLogger(__name__)

# Propagación de Route 53: get_change nace PENDING y pasa a INSYNC (segundos a
# pocos minutos). El job espera acotado; si no llega, deja el estado honesto
# (pending) y el cron reintenta.
DNS_PROPAGATION_TIMEOUT = 180
DNS_PROPAGATION_INTERVAL = 15
# Anti-zombi (mismo criterio que los backups): un 'pending' que no llega a
# INSYNC tras estas horas es anómalo en Route 53 → error honesto, ni pending
# eterno ni falso synced.
DNS_PENDING_STUCK_HOURS = 1

# Tipos de registro que gestiona el módulo. El resto (NS, SOA, ...) se ignora.
SUPPORTED_RECORD_TYPES = {"A", "CNAME", "TXT", "MX"}

# IPv4 simple (cuatro octetos 0-255). Suficiente para validar registros A.
_IPV4_RE = re.compile(
    r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$"
)
# Hostname (para CNAME y el destino de MX): etiquetas alfanuméricas con guiones.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)+"
    r"[A-Za-z]{2,63}\.?$"
)


class PrimateCloudDnsRecord(models.Model):
    """Registro DNS alojado en una zona de Route 53."""

    _name = "primate.cloud.dns.record"
    _description = "Registro DNS"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, help="Ej.: forum.primate.cloud")
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
    hosted_zone_id = fields.Char(string="Hosted Zone ID", index=True)
    record_type = fields.Selection(
        [("A", "A"), ("CNAME", "CNAME"), ("TXT", "TXT"), ("MX", "MX")],
        string="Tipo",
        required=True,
    )
    record_value = fields.Char(string="Valor")
    ttl = fields.Integer(string="TTL", default=300)
    state = fields.Selection(
        [("draft", "Borrador"), ("active", "Activo"), ("deleted", "Eliminado")],
        string="Estado",
        default="active",
        help="Ciclo de vida del registro en PCM (no confundir con el estado de "
             "sincronización con AWS).",
    )
    # --- Sincronización con Route 53 (Fase 8.5) ---
    # Dimensión SEPARADA de `state`: acuerdo entre PCM y el registro real en AWS.
    # Reusa la semántica del `sync_state` de repositorios.
    sync_state = fields.Selection(
        [
            ("unknown", "Sin verificar"),
            ("synced", "Sincronizado"),
            ("pending", "Propagando"),
            ("divergent", "Divergente"),
            ("error", "Error"),
        ],
        string="Estado de sincronización",
        default="unknown",
        tracking=True,
        help="Acuerdo con Route 53: sincronizado, propagando (esperando INSYNC), "
             "divergente (AWS difiere de PCM) o error.",
    )
    record_value_aws = fields.Char(
        string="Valor en AWS", readonly=True,
        help="Valor real detectado en Route 53 cuando difiere del de PCM "
             "(estado divergente). Vacío si coinciden.",
    )
    last_change_id = fields.Char(
        string="Último Change ID", readonly=True,
        help="ChangeId del último cambio aplicado en Route 53 (para consultar "
             "la propagación).",
    )
    is_alias = fields.Boolean(
        string="Registro alias", readonly=True,
        help="Registro alias de Route 53 (apunta a un recurso AWS: ELB, "
             "CloudFront, S3). Forma distinta —AliasTarget, sin TTL/valores "
             "simples—: PCM no lo edita en v1 para no romperlo. La señal es la "
             "presencia de AliasTarget en AWS, NO el TTL.",
    )
    delete_needs_ack = fields.Boolean(
        string="Borrado con confirmación reforzada",
        compute="_compute_delete_needs_ack",
        help="El borrado exige acknowledge extra: entorno productivo o SIN "
             "entorno (origen desconocido = potencialmente producción).",
    )
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True)

    _dns_record_uniq = models.Constraint(
        "UNIQUE(account_id, hosted_zone_id, name, record_type)",
        "Ese registro DNS ya existe para la cuenta y zona.",
    )

    @api.depends("environment_id", "environment_id.env_type")
    def _compute_delete_needs_ack(self):
        """Guardia de borrado. La AUSENCIA de entorno NO baja la guardia: si no
        se puede determinar que el registro es seguro de borrar, se trata como
        potencialmente producción (fricción alta). Solo un entorno explícito y
        NO productivo habilita la fricción baja.
        """
        for record in self:
            env = record.environment_id
            record.delete_needs_ack = (not env) or env.env_type == "production"

    @api.model
    def _validate_dns_value(self, record_type, values):
        """Valida la forma de los valores según el tipo (antes de tocar AWS).

        Args:
            record_type (str): ``A`` / ``CNAME`` / ``TXT`` / ``MX``.
            values (list[str]): valores ya separados (sin vacíos).

        Raises:
            ValidationError: si algún valor no tiene la forma esperada.
        """
        if not values:
            raise ValidationError(_("El registro necesita al menos un valor."))
        if record_type == "A":
            for value in values:
                if not _IPV4_RE.match(value.strip()):
                    raise ValidationError(
                        _("Registro A: '%s' no es una IPv4 válida.") % value)
        elif record_type == "CNAME":
            if len(values) != 1:
                raise ValidationError(
                    _("Registro CNAME: debe tener exactamente un valor."))
            if not _HOSTNAME_RE.match(values[0].strip()):
                raise ValidationError(
                    _("Registro CNAME: '%s' no es un hostname válido.")
                    % values[0])
        elif record_type == "MX":
            for value in values:
                parts = value.strip().split()
                if len(parts) != 2 or not parts[0].isdigit() \
                        or not _HOSTNAME_RE.match(parts[1]):
                    raise ValidationError(
                        _("Registro MX: '%s' debe ser 'prioridad host' "
                          "(ej.: '10 mail.forum.cloud').") % value)
        elif record_type == "TXT":
            for value in values:
                if not value.strip():
                    raise ValidationError(
                        _("Registro TXT: los valores no pueden ser vacíos."))
        else:
            raise ValidationError(
                _("Tipo de registro no soportado: %s.") % record_type)

    def _sync_from_aws(self, account, records):
        """Crea/actualiza registros DNS desde datos normalizados de Route 53.

        Solo sincroniza los tipos soportados. Upsert por
        (cuenta, zona, nombre, tipo).

        Args:
            account (recordset): cuenta AWS de origen.
            records (list[dict]): salida de ``AwsRoute53Service.list_records``.

        Returns:
            dict: ``{"created": int, "updated": int, "skipped": int}``.
        """
        now = fields.Datetime.now()
        created = updated = skipped = 0
        for data in records:
            if data.get("record_type") not in SUPPORTED_RECORD_TYPES:
                skipped += 1
                continue
            aws_value = data.get("record_value")
            aws_ttl = data.get("ttl") or 300
            aws_is_alias = bool(data.get("is_alias"))
            # El NAME se compara NORMALIZADO (sin punto final, minúsculas): los
            # nombres DNS son case-insensitive y Route 53 los devuelve como FQDN
            # con punto final. Sin esto, un registro PCM 'Forum.X.com' no
            # matchearía el 'forum.x.com.' de AWS y se crearía un DUPLICADO
            # (mismo falso positivo cosmético que ya se evita en los valores).
            aws_name = self._normalize_dns_name(data.get("name"))
            candidates = self.search([
                ("account_id", "=", account.id),
                ("hosted_zone_id", "=", data.get("hosted_zone_id")),
                ("record_type", "=", data.get("record_type")),
            ])
            existing = candidates.filtered(
                lambda r: self._normalize_dns_name(r.name) == aws_name
            )[:1]
            if existing:
                # Fase 8.5: si el valor/TTL real de AWS difiere del de PCM,
                # NO se pisa en silencio: se marca DIVERGENTE y se guarda el
                # valor de AWS para comparar. El operador decide re-aplicar el
                # de PCM o adoptar el de AWS. Si coinciden, queda SINCRONIZADO.
                if self._dns_values_differ(existing.record_value, aws_value) \
                        or existing.ttl != aws_ttl:
                    existing.write({
                        "record_value_aws": aws_value,
                        "sync_state": "divergent",
                        "is_alias": aws_is_alias,
                        "last_sync_date": now,
                    })
                else:
                    existing.write({
                        "record_value_aws": False,
                        "sync_state": "synced",
                        "state": "active",
                        "is_alias": aws_is_alias,
                        "last_sync_date": now,
                    })
                updated += 1
            else:
                # Nuevo desde AWS: por definición coincide con AWS.
                self.create({
                    "account_id": account.id,
                    "hosted_zone_id": data.get("hosted_zone_id"),
                    "name": data.get("name"),
                    "record_type": data.get("record_type"),
                    "record_value": aws_value,
                    "ttl": aws_ttl,
                    "state": "active",
                    "sync_state": "synced",
                    "is_alias": aws_is_alias,
                    "last_sync_date": now,
                })
                created += 1
        return {"created": created, "updated": updated, "skipped": skipped}

    @staticmethod
    def _normalize_dns_name(name):
        """Forma canónica de un nombre DNS para comparar: sin punto final y en
        minúsculas (los nombres DNS son case-insensitive; Route 53 los devuelve
        como FQDN con punto final)."""
        return (name or "").strip().rstrip(".").lower()

    @staticmethod
    def _dns_values_differ(pcm_value, aws_value):
        """Compara valores DNS como CONJUNTOS (multi-valor sin importar orden).

        Route 53 no garantiza el orden de los valores múltiples (varias IPs en
        un A, varias prioridades en MX); comparar por conjunto evita marcar
        divergencia falsa por reordenamiento.
        """
        def as_set(value):
            return {v.strip() for v in (value or "").split(",") if v.strip()}
        return as_set(pcm_value) != as_set(aws_value)

    # ------------------------------------------------------------------
    # CRUD sobre Route 53 (Fase 8.5, Bloque 2): aplicar / borrar / propagación
    # ------------------------------------------------------------------
    def _value_list(self):
        """Valores del registro como lista limpia (separados por coma o línea)."""
        self.ensure_one()
        return [v.strip() for v in re.split(r"[\n,]", self.record_value or "")
                if v.strip()]

    def _service(self):
        """Adaptador Route 53 autenticado para la cuenta del registro."""
        self.ensure_one()
        return aws_route53.AwsRoute53Service(self.account_id._get_aws_service())

    def _log_dns(self, action_type, result="success", error=None, change_id=None):
        """Atajo de bitácora para operaciones DNS."""
        self.ensure_one()
        self.env["primate.cloud.operation.log"].log_operation(
            action_type,
            name=_("%(action)s DNS: %(name)s",
                   action=dict(self.env["primate.cloud.operation.log"]
                               ._fields["action_type"].selection).get(action_type),
                   name=self.name),
            record=self, result=result, error_message=error, aws_request_id=change_id,
        )

    def action_apply_change(self, vals=None, action_type="dns_update"):
        """Encola la aplicación (crear/editar) del registro en Route 53.

        Valida la forma antes de encolar; el trabajo pesado (llamada AWS +
        propagación) va en queue_job.
        """
        self.ensure_one()
        if vals:
            self.write(vals)
        self._validate_dns_value(self.record_type, self._value_list())
        self.sync_state = "pending"
        self.with_delay(
            description=_("Aplicar DNS: %s") % self.name
        ).job_apply_change(action_type=action_type)
        return True

    def job_apply_change(self, action_type="dns_update"):
        """Job: aplica el registro (UPSERT) en Route 53 y confirma propagación.

        Nunca deja el registro en ``synced`` mientras Route 53 responda
        ``PENDING``: aplica, queda ``pending`` y sólo pasa a ``synced`` (o
        ``divergent``) tras releer la realidad cuando el cambio está ``INSYNC``.
        """
        self.ensure_one()
        self._validate_dns_value(self.record_type, self._value_list())
        try:
            service = self._service()
            change_id = service.create_record(
                self.hosted_zone_id, self.name, self.record_type,
                self._value_list(), ttl=self.ttl,
                comment="pcm dns apply: %s" % self.name,
            )
        except Exception as error:  # noqa: BLE001 - se audita y no se traga
            self.write({"sync_state": "error"})
            self.message_post(body=_("Aplicación DNS fallida: %s") % error)
            self._log_dns(action_type, result="failed", error=str(error))
            return False
        self.write({"last_change_id": change_id, "sync_state": "pending",
                    "state": "active"})
        self._log_dns(action_type, change_id=change_id)
        self._poll_and_finalize(service, change_id)
        return True

    def job_delete(self):
        """Job: borra el registro en Route 53 y confirma su desaparición.

        El registro PCM se conserva con ``state='deleted'`` para auditoría
        (no se hace ``unlink``); ``sync_state`` refleja la confirmación.
        """
        self.ensure_one()
        try:
            service = self._service()
            change_id = service.delete_record(
                self.hosted_zone_id, self.name, self.record_type,
                self._value_list(), ttl=self.ttl,
                comment="pcm dns delete: %s" % self.name,
            )
        except Exception as error:  # noqa: BLE001
            self.write({"sync_state": "error"})
            self.message_post(body=_("Borrado DNS fallido: %s") % error)
            self._log_dns("dns_delete", result="failed", error=str(error))
            return False
        self.write({"state": "deleted", "last_change_id": change_id,
                    "sync_state": "pending"})
        self._log_dns("dns_delete", change_id=change_id)
        self._poll_and_finalize(service, change_id)
        return True

    def action_open_edit(self):
        """Abre el wizard de edición de este registro (en el drawer)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Editar registro DNS"),
            "res_model": "primate.cloud.dns.record.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_record_id": self.id},
        }

    def action_open_delete(self):
        """Abre el wizard de borrado (confirmación fuerte) de este registro."""
        self.ensure_one()
        if self.state == "deleted":
            raise ValidationError(_("El registro ya está eliminado."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Eliminar registro DNS"),
            "res_model": "primate.cloud.dns.delete.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_record_id": self.id},
        }

    def action_check_sync_state(self):
        """Encola la re-verificación de un registro contra Route 53 (divergencia)."""
        self.ensure_one()
        self.with_delay(
            description=_("Verificar DNS: %s") % self.name
        ).job_check_sync_state()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Verificación de DNS encolada."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_check_sync_state(self):
        """Job: relee el registro real y actualiza synced/divergent."""
        self.ensure_one()
        self._finalize_dns_sync(self._service())
        return self.sync_state

    def _poll_and_finalize(self, service, change_id, timeout=None, interval=None,
                           _sleep=time.sleep):
        """Espera acotada a que el cambio propague (INSYNC) y finaliza el estado.

        Si se agota el tiempo con el cambio aún ``PENDING``, NO miente: deja el
        registro en ``pending`` y el cron lo reintenta. Nunca ``synced`` con
        ``PENDING``.
        """
        self.ensure_one()
        timeout = DNS_PROPAGATION_TIMEOUT if timeout is None else timeout
        interval = DNS_PROPAGATION_INTERVAL if interval is None else interval
        waited = 0
        while waited < timeout:
            try:
                status = service.get_change_status(change_id)
            except Exception:  # noqa: BLE001 - transitorio: se reintenta
                status = None
            if status == "INSYNC":
                self._finalize_dns_sync(service)
                return "INSYNC"
            _sleep(interval)
            waited += interval
        self.message_post(body=_(
            "Propagación DNS demorada (aún PENDING tras %ss); se reintenta "
            "por el cron.") % timeout)
        return "PENDING"

    def _read_aws_record(self, service):
        """Relee el RRSet real de Route 53 para este registro, o None."""
        self.ensure_one()
        target = self._normalize_dns_name(self.name)
        for record in service.list_records(self.hosted_zone_id):
            if self._normalize_dns_name(record.get("name")) == target \
                    and record.get("record_type") == self.record_type:
                return record
        return None

    def _finalize_dns_sync(self, service):
        """Fija synced/divergent releyendo la REALIDAD de Route 53 (fuente de
        verdad), no asumiendo el resultado del cambio."""
        self.ensure_one()
        aws = self._read_aws_record(service)
        if self.state == "deleted":
            # Se esperaba que desapareciera.
            if aws is None:
                self.write({"sync_state": "synced", "record_value_aws": False,
                            "last_sync_date": fields.Datetime.now()})
            else:
                self.write({"sync_state": "divergent",
                            "record_value_aws": aws.get("record_value"),
                            "last_sync_date": fields.Datetime.now()})
            return self.sync_state
        if aws is None:
            # Debería existir y no está: alguien lo borró por fuera.
            self.write({"sync_state": "divergent", "record_value_aws": False,
                        "last_sync_date": fields.Datetime.now()})
        elif self._dns_values_differ(self.record_value, aws.get("record_value")) \
                or self.ttl != (aws.get("ttl") or 300):
            self.write({"sync_state": "divergent",
                        "record_value_aws": aws.get("record_value"),
                        "last_sync_date": fields.Datetime.now()})
        else:
            self.write({"sync_state": "synced", "record_value_aws": False,
                        "last_sync_date": fields.Datetime.now()})
        return self.sync_state

    @api.model
    def _cron_resync_pending(self):
        """Cron anti-zombi: reintenta los registros en ``pending``.

        Reconsulta ``get_change``: si ya está ``INSYNC``, finaliza el estado; si
        sigue ``PENDING`` más de :data:`DNS_PENDING_STUCK_HOURS` (anómalo en
        Route 53, típicamente resuelve en minutos), lo marca ``error`` — ni
        pending eterno ni falso synced.
        """
        now = fields.Datetime.now()
        pending = self.search([("sync_state", "=", "pending"),
                               ("last_change_id", "!=", False)])
        resolved = stuck = 0
        for record in pending:
            try:
                service = record._service()
                status = service.get_change_status(record.last_change_id)
            except Exception:  # noqa: BLE001 - transitorio: la próxima corrida
                continue
            if status == "INSYNC":
                record._finalize_dns_sync(service)
                resolved += 1
            elif record.write_date and (now - record.write_date).total_seconds() \
                    > DNS_PENDING_STUCK_HOURS * 3600:
                record.write({"sync_state": "error"})
                record.message_post(body=_(
                    "No se pudo confirmar la propagación DNS tras %sh; marcado "
                    "como error.") % DNS_PENDING_STUCK_HOURS)
                stuck += 1
        if pending:
            _logger.info("DNS pending resueltos: %s, marcados error: %s.",
                         resolved, stuck)
