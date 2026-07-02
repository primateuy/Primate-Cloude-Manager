# -*- coding: utf-8 -*-
"""Registro DNS en Route 53 (inventario en Fase 2, CRUD en Fase 8.5)."""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

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
                        "last_sync_date": now,
                    })
                else:
                    existing.write({
                        "record_value_aws": False,
                        "sync_state": "synced",
                        "state": "active",
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
