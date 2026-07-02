# -*- coding: utf-8 -*-
"""Registro DNS en Route 53 (inventario, solo lectura en Fase 2)."""
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Tipos de registro que gestiona el módulo. El resto (NS, SOA, ...) se ignora.
SUPPORTED_RECORD_TYPES = {"A", "CNAME", "TXT", "MX"}


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
    )
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True)

    _dns_record_uniq = models.Constraint(
        "UNIQUE(account_id, hosted_zone_id, name, record_type)",
        "Ese registro DNS ya existe para la cuenta y zona.",
    )

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
            vals = {
                "record_value": data.get("record_value"),
                "ttl": data.get("ttl") or 300,
                "state": "active",
                "last_sync_date": now,
            }
            existing = self.search(
                [
                    ("account_id", "=", account.id),
                    ("hosted_zone_id", "=", data.get("hosted_zone_id")),
                    ("name", "=", data.get("name")),
                    ("record_type", "=", data.get("record_type")),
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
                        "hosted_zone_id": data.get("hosted_zone_id"),
                        "name": data.get("name"),
                        "record_type": data.get("record_type"),
                    }
                )
                self.create(vals)
                created += 1
        return {"created": created, "updated": updated, "skipped": skipped}
