# -*- coding: utf-8 -*-
"""Costo persistido de Cost Explorer, atribuido por identificador estable (Fase 9).

Cada fila es el costo de un (cuenta, período, entorno/cliente, servicio). La
atribución guarda TRES datos de más a menos durable, para sobrevivir al borrado
del entorno o del partner (que pueden tener costo histórico):
- ``*_ref`` (Char): el valor CRUDO del tag de Cost Explorer (clave inmutable).
- ``*_name`` (Char): snapshot denormalizado del nombre AL momento del pull.
- ``environment_id``/``partner_id`` (M2o, ondelete=set null): vínculo vivo solo
  para drill-through cuando el registro todavía existe.

La UI lee siempre ``*_name`` (nunca depende de que el registro viva).
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Etiquetas de los buckets no atribuibles / registro borrado.
UNATTRIBUTED_LABEL = "Sin atribuir"
DELETED_LABEL = "Entorno eliminado"
DELETED_CLIENT_LABEL = "Cliente eliminado"


class PrimateCloudCostEntry(models.Model):
    """Costo de un recurso/grupo obtenido de Cost Explorer (persistido)."""

    _name = "primate.cloud.cost.entry"
    _description = "Costo Cloud"
    _order = "period_start desc, amount desc"

    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True,
        ondelete="cascade", index=True,
    )
    # --- Período ---
    period_start = fields.Date(string="Desde", required=True, index=True)
    period_end = fields.Date(string="Hasta (exclusive)", required=True)
    granularity = fields.Selection(
        [("daily", "Diaria"), ("monthly", "Mensual")],
        string="Granularidad", required=True, default="monthly",
    )
    is_forecast = fields.Boolean(string="Proyección", default=False)

    # --- Atribución de ENTORNO (esquema de 3 campos durables) ---
    environment_ref = fields.Char(
        string="Ref de entorno", index=True,
        help="Valor crudo del tag primate:environment_id (clave inmutable). "
             "Vacío = sin atribuir.",
    )
    environment_name = fields.Char(
        string="Entorno",
        help="Nombre del entorno al momento del pull (snapshot: sobrevive al "
             "borrado del entorno).",
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno (vínculo)",
        ondelete="set null", index=True,
        help="Vínculo vivo para drill-through; se pone a False si el entorno "
             "se borra (el costo se conserva por ref/nombre).",
    )
    # --- Atribución de CLIENTE (mismo esquema) ---
    client_ref = fields.Char(string="Ref de cliente", index=True)
    client_name = fields.Char(string="Cliente")
    partner_id = fields.Many2one(
        "res.partner", string="Cliente (vínculo)", ondelete="set null",
    )

    # --- Costo ---
    service = fields.Char(string="Servicio AWS", index=True)
    amount = fields.Float(string="Monto")
    currency = fields.Char(string="Moneda", default="USD")
    pulled_at = fields.Datetime(string="Consultado el", readonly=True)

    _cost_entry_uniq = models.Constraint(
        "UNIQUE(account_id, period_start, granularity, is_forecast, "
        "environment_ref, service)",
        "Ya existe un costo para esa cuenta/período/entorno/servicio.",
    )

    @api.depends("environment_name", "service", "amount", "currency")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s · %s · %.2f %s" % (
                rec.environment_name or _(UNATTRIBUTED_LABEL),
                rec.service or "", rec.amount, rec.currency or "")
