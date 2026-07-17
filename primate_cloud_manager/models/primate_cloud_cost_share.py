# -*- coding: utf-8 -*-
"""Reparto del costo CRUDO de un servidor entre sus instancias (R5).

El crudo (``cost.entry``) es POR SERVIDOR: la EC2 es indivisible, AWS no da
costo por Odoo. ``cost.share`` es la capa DERIVADA que reparte ese crudo entre
las instancias hospedadas (eje cliente). Propiedades:

- **Siempre derivada y regenerable**: se recalcula desde el crudo + las
  instancias del servidor. Nunca es fuente de verdad.
- **Inmutable desde la UI**: ``write``/``unlink`` bloqueados (patrón
  ``operation.log``/``backup``), CON un escape de sistema (context
  ``pcm_cost_regen``) que usa SOLO el motor de regeneración para reemplazarlas.
- **Esquema durable de 3 datos** (igual que ``cost.entry``): ``*_ref`` (clave
  estable), ``*_name`` (snapshot al cómputo) y el M2o vivo (``set null``), para
  que una instancia archivada/borrada siga mostrando su reparto histórico.

Invariante (con test, en B2): Σ shares de un (servidor, mes) == crudo del
servidor AL CENTAVO, con el residuo de división asignado determinísticamente.
El cuadre GLOBAL sigue siendo contra el total de Cost Explorer (``cost.entry``),
no contra la suma de shares.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Context que habilita reescribir/borrar shares: SOLO el motor de regeneración
# lo setea. Sin él, el modelo es inmutable desde la UI.
REGEN_CONTEXT_KEY = "pcm_cost_regen"

UNATTRIBUTED_INSTANCE_LABEL = "Sin instancias (servidor sin atribuir)"


class PrimateCloudCostShare(models.Model):
    """Porción del costo crudo de un servidor atribuida a una instancia."""

    _name = "primate.cloud.cost.share"
    _description = "Reparto de Costo Cloud"
    _order = "period_start desc, environment_name, amount desc"

    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True,
        ondelete="cascade", index=True,
    )
    # --- Período (mensual: la unidad de facturación al cliente) ---
    period_start = fields.Date(string="Desde", required=True, index=True)
    period_end = fields.Date(string="Hasta (exclusive)", required=True)
    granularity = fields.Selection(
        [("monthly", "Mensual")], string="Granularidad",
        required=True, default="monthly",
    )

    # --- Servidor origen del crudo (esquema durable) ---
    environment_ref = fields.Char(string="Ref de servidor", index=True)
    environment_name = fields.Char(string="Servidor")
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Servidor (vínculo)",
        ondelete="set null", index=True,
    )
    # --- Instancia atribuida (False = share no atribuida del servidor) ---
    instance_ref = fields.Char(string="Ref de instancia", index=True)
    instance_name = fields.Char(string="Instancia")
    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia (vínculo)",
        ondelete="set null", index=True,
    )
    # --- Cliente (mismo esquema) ---
    client_ref = fields.Char(string="Ref de cliente", index=True)
    client_name = fields.Char(string="Cliente")
    project_id = fields.Many2one(
        "primate.cloud.project", string="Proyecto (vínculo)",
        ondelete="set null",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Cliente (vínculo)", ondelete="set null",
    )
    unattributed = fields.Boolean(
        string="Sin atribuir", default=False, index=True,
        help="Share del crudo de un servidor que no pudo atribuirse a ninguna "
             "instancia (p. ej. servidor sin instancias vivas en el período).",
    )

    # --- Reparto ---
    method = fields.Selection(
        [("equal", "Partes iguales"),
         ("weight", "Por peso"),
         ("usage", "Por uso (deshabilitado)")],
        string="Método", required=True, default="equal",
    )
    amount = fields.Float(string="Monto atribuido")
    currency = fields.Char(string="Moneda", default="USD")
    computed_at = fields.Datetime(string="Calculado el", readonly=True)

    _cost_share_uniq = models.Constraint(
        "UNIQUE(account_id, period_start, granularity, environment_ref, "
        "instance_ref)",
        "Ya existe un reparto para esa cuenta/período/servidor/instancia.",
    )

    @api.depends("instance_name", "client_name", "amount", "currency")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s · %s · %.2f %s" % (
                rec.instance_name or _(UNATTRIBUTED_INSTANCE_LABEL),
                rec.client_name or "", rec.amount, rec.currency or "")

    # --- Inmutabilidad con escape de sistema para la regeneración -----------
    def write(self, vals):
        """Inmutable desde la UI: solo el motor de regeneración (context
        ``pcm_cost_regen``) puede reescribir shares."""
        if not self.env.context.get(REGEN_CONTEXT_KEY):
            raise UserError(_(
                "El reparto de costos es derivado e inmutable: no se edita a "
                "mano. Se regenera desde el costo crudo (recalcular)."))
        return super().write(vals)

    def unlink(self):
        """Inmutable desde la UI: solo la regeneración borra shares (para
        reemplazarlas por el recálculo del período)."""
        if not self.env.context.get(REGEN_CONTEXT_KEY):
            raise UserError(_(
                "El reparto de costos es derivado e inmutable: no se elimina a "
                "mano. Se regenera desde el costo crudo (recalcular)."))
        return super().unlink()

    # --- Splitter PURO al centavo con residuo determinístico ------------
    @api.model
    def _split_cents(self, total_amount, weights):
        """Reparte ``total_amount`` (USD) entre N pesos, al CENTAVO exacto.

        Devuelve una lista de montos (USD) que suman EXACTAMENTE
        ``round(total_amount*100)/100`` — el invariante. Trabaja en centavos
        enteros: cada porción es el ``floor`` de su cuota, y los centavos
        sobrantes (``total - Σfloors``, siempre ``< N``) se reparten por el
        método de mayor resto (Hamilton) con desempate DETERMINÍSTICO por el
        orden recibido (el llamador ordena por ``pcm_ref``). Así ``equal`` (todos
        los restos iguales) los asigna a los primeros por pcm_ref — reproducible.

        Es puro (sin ORM, sin efectos): testeable en aislamiento.
        """
        total_cents = round((total_amount or 0.0) * 100)
        wsum = sum(weights)
        if not weights or wsum <= 0:
            return []   # sin destino: el llamador arma la share "sin atribuir"
        raw = [total_cents * w / wsum for w in weights]
        floors = [int(x) for x in raw]     # x >= 0 → floor
        remainder = total_cents - sum(floors)
        # Mayor resto primero; empate → menor índice (orden pcm_ref del llamador).
        order = sorted(range(len(weights)),
                       key=lambda i: (raw[i] - floors[i], -i), reverse=True)
        for k in range(remainder):
            floors[order[k]] += 1
        return [c / 100.0 for c in floors]
