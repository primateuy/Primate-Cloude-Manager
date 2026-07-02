# -*- coding: utf-8 -*-
"""Asistente de creación/edición de registros DNS (Fase 8.5, Bloque 3).

Escritura gestionada en Route 53 desde el drawer, sin modales stock. Valida la
forma por tipo antes de encolar y BLOQUEA la edición de registros alias de
Route 53 (tienen forma distinta —AliasTarget, sin TTL— y reescribirlos como
registro simple los rompería).
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PrimateCloudDnsRecordWizard(models.TransientModel):
    """Crear un registro nuevo o editar uno existente (valor/TTL)."""

    _name = "primate.cloud.dns.record.wizard"
    _description = "Asistente de Registro DNS"

    # Si viene, es EDICIÓN de ese registro; si no, CREACIÓN.
    record_id = fields.Many2one(
        "primate.cloud.dns.record", string="Registro", ondelete="cascade",
    )
    is_edit = fields.Boolean(compute="_compute_is_edit")

    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno", ondelete="cascade",
    )
    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True,
        ondelete="cascade",
    )
    hosted_zone_id = fields.Char(string="Hosted Zone ID", required=True)
    name = fields.Char(string="Nombre", required=True,
                       help="Ej.: forum.primate.cloud")
    record_type = fields.Selection(
        [("A", "A"), ("CNAME", "CNAME"), ("TXT", "TXT"), ("MX", "MX")],
        string="Tipo", required=True, default="A",
    )
    record_value = fields.Text(
        string="Valor(es)", required=True,
        help="Uno por línea para múltiples valores. A: IPv4. CNAME: un "
             "hostname. MX: 'prioridad host'. TXT: texto.",
    )
    ttl = fields.Integer(string="TTL", default=300)

    @api.depends("record_id")
    def _compute_is_edit(self):
        for wizard in self:
            wizard.is_edit = bool(wizard.record_id)

    @api.model
    def default_get(self, fields_list):
        """En edición precarga los datos del registro; bloquea alias."""
        res = super().default_get(fields_list)
        record = self.env["primate.cloud.dns.record"].browse(
            res.get("record_id") or self.env.context.get("default_record_id") or 0
        ).exists()
        if record:
            # Registro alias de Route 53 (forma distinta: AliasTarget, sin TTL).
            # Se usa el marcador EXPLÍCITO que guarda el sync (is_alias), no un
            # proxy por TTL: un alias no trae TTL y un registro simple con TTL 0
            # es válido — el proxy confundiría a ambos.
            if record.is_alias:
                raise UserError(_(
                    "'%s' es un registro alias de Route 53 (apunta a un "
                    "recurso AWS). Gestionalo en la consola de AWS; PCM no lo "
                    "edita en v1 para no romperlo.") % record.name)
            res.update({
                "record_id": record.id,
                "environment_id": record.environment_id.id,
                "account_id": record.account_id.id,
                "hosted_zone_id": record.hosted_zone_id,
                "name": record.name,
                "record_type": record.record_type,
                "record_value": (record.record_value or "").replace(", ", "\n"),
                "ttl": record.ttl,
            })
        return res

    def action_confirm(self):
        """Valida y encola la aplicación en Route 53 (crear o editar)."""
        self.ensure_one()
        Dns = self.env["primate.cloud.dns.record"]
        values = [v.strip() for v in (self.record_value or "").splitlines()
                  if v.strip()]
        # Valida la forma ANTES de crear/editar nada (reusa la regla del modelo).
        Dns._validate_dns_value(self.record_type, values)
        joined = ", ".join(values)
        if self.record_id:
            record = self.record_id
            record.action_apply_change(
                {"record_value": joined, "ttl": self.ttl},
                action_type="dns_update",
            )
        else:
            record = Dns.create({
                "name": self.name,
                "account_id": self.account_id.id,
                "environment_id": self.environment_id.id or False,
                "hosted_zone_id": self.hosted_zone_id,
                "record_type": self.record_type,
                "record_value": joined,
                "ttl": self.ttl,
                "state": "draft",
            })
            record.action_apply_change(action_type="dns_create")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Cambio DNS encolado (aplicando en "
                                    "Route 53)."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
