# -*- coding: utf-8 -*-
"""Wizard genérico de destrucción en 3 pantallas (regla dura 2026-10-22).

UNA sola pieza de UI para TODAS las acciones destructivas (terminar EC2,
desmontar instancia Odoo, borrar registro DNS remoto, restore que pisa
datos, borrar backup con su S3). Cada transición de pantalla valida contra
los tokens del SERVIDOR (primate.cloud.destruction.ticket): la UI es solo
la cara — saltearla por RPC no sirve de nada porque el método destructivo
final exige el token consumido igual.

Contexto esperado al abrir:
    dest_model / dest_res_id   el recurso a destruir
    dest_action                la clave de la acción (ec2_terminate, ...)
    dest_callback              método del recurso a invocar al completar
    dest_callback_params       (opcional) JSON de kwargs para el callback
"""
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PrimateCloudDestructionWizard(models.TransientModel):
    _name = "primate.cloud.destruction.wizard"
    _description = "Confirmación destructiva en 3 pasos"

    res_model = fields.Char(required=True, readonly=True)
    res_id = fields.Integer(required=True, readonly=True)
    action = fields.Char(required=True, readonly=True)
    callback = fields.Char(required=True, readonly=True)
    callback_params = fields.Char(readonly=True)
    ticket_id = fields.Integer(readonly=True)
    token = fields.Char(readonly=True)
    step = fields.Selection(
        [("impact", "1 · Impacto"), ("name", "2 · Nombre exacto"),
         ("reauth", "3 · Re-autenticación")],
        default="impact", required=True, readonly=True)
    impact_text = fields.Text(string="Qué se va a destruir", readonly=True)
    resource_name = fields.Char(readonly=True)
    ttl_minutes = fields.Integer(readonly=True)
    typed_name = fields.Char(string="Escribí el nombre EXACTO del recurso")
    password = fields.Char(string="Tu contraseña")
    totp_code = fields.Char(string="Código 2FA (si tenés TOTP)")

    @api.model
    def default_get(self, fields_list):
        """Abre el PASO 1 pidiendo el ticket al servidor (grupo + impacto)."""
        values = super().default_get(fields_list)
        ctx = self.env.context
        model = ctx.get("dest_model")
        res_id = ctx.get("dest_res_id")
        action = ctx.get("dest_action")
        callback = ctx.get("dest_callback")
        if not (model and res_id and action and callback):
            raise UserError(_("Falta el contexto de la acción destructiva."))
        start = self.env["primate.cloud.destruction.ticket"].destruction_start(
            model, res_id, action)
        impact = start.get("impact") or {}
        values.update({
            "res_model": model, "res_id": res_id, "action": action,
            "callback": callback,
            "callback_params": ctx.get("dest_callback_params") or "{}",
            "ticket_id": start["ticket_id"], "token": start["token"],
            "resource_name": impact.get("resource_name") or "",
            "ttl_minutes": start.get("ttl_minutes") or 5,
            "impact_text": impact.get("text") or _("(sin detalle de impacto)"),
        })
        return values

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Operación destructiva — paso %s de 3",
                      {"impact": 1, "name": 2, "reauth": 3}[self.step]),
            "res_model": self._name, "res_id": self.id,
            "view_mode": "form", "target": "new",
        }

    def action_next_from_impact(self):
        self.ensure_one()
        self.step = "name"
        return self._reopen()

    def action_confirm_name(self):
        """Paso 2: el nombre viaja al servidor; el match es server-side."""
        self.ensure_one()
        result = self.env["primate.cloud.destruction.ticket"]\
            .destruction_confirm_name(self.ticket_id, self.token,
                                      self.typed_name)
        self.write({"token": result["token"], "step": "reauth",
                    "typed_name": False})
        return self._reopen()

    def action_execute(self):
        """Paso 3: re-autenticación + ejecución del callback con el token."""
        self.ensure_one()
        result = self.env["primate.cloud.destruction.ticket"].destruction_reauth(
            self.ticket_id, self.token, self.password, self.totp_code)
        self.write({"password": False, "totp_code": False})
        record = self.env[self.res_model].browse(self.res_id).exists()
        if not record:
            raise UserError(_("El recurso ya no existe."))
        params = json.loads(self.callback_params or "{}")
        method = getattr(record.with_context(
            destruction_ticket_id=result["ticket_id"],
            destruction_token=result["token"]), self.callback)
        return method(**params) or {"type": "ir.actions.act_window_close"}

    def action_cancel(self):
        self.ensure_one()
        if self.ticket_id:
            self.env["primate.cloud.destruction.ticket"].destruction_cancel(
                self.ticket_id)
        return {"type": "ir.actions.act_window_close"}
