# -*- coding: utf-8 -*-
"""Asistente de borrado de un registro DNS (Fase 8.5, Bloque 3).

Operación peligrosa (borrar el registro equivocado tira un dominio): protección
al nivel del restore de la Fase 8. Nombre tipeado validado server-side, y para
registros productivos O SIN entorno (origen desconocido = potencialmente
producción, ``delete_needs_ack``) un acknowledge extra.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PrimateCloudDnsDeleteWizard(models.TransientModel):
    """Exige tipear el nombre (y ack reforzado si aplica) antes de borrar."""

    _name = "primate.cloud.dns.delete.wizard"
    _description = "Eliminar Registro DNS"

    record_id = fields.Many2one(
        "primate.cloud.dns.record", string="Registro", required=True,
        ondelete="cascade", readonly=True,
    )
    record_name = fields.Char(related="record_id.name", string="Nombre",
                              readonly=True)
    record_value = fields.Char(related="record_id.record_value",
                               string="Valor actual", readonly=True)
    needs_ack = fields.Boolean(related="record_id.delete_needs_ack")
    environment_name = fields.Char(related="record_id.environment_id.name",
                                   string="Entorno", readonly=True)
    confirm_name = fields.Char(
        string="Escribí el nombre del registro",
        help="Para confirmar, escribí exactamente el nombre del registro.",
    )
    acknowledge = fields.Boolean(
        string="Entiendo que esto puede tirar abajo el dominio",
    )

    def action_confirm(self):
        """Valida las salvaguardas y encola el borrado en Route 53."""
        self.ensure_one()
        record = self.record_id
        if (self.confirm_name or "").strip() != (record.name or "").strip():
            raise UserError(
                _("El nombre escrito no coincide con el del registro."))
        # Guardia reforzada: producción O sin entorno (desconocido = tratado
        # como potencialmente producción). La ausencia NO baja la guardia.
        if record.delete_needs_ack and not self.acknowledge:
            raise UserError(_(
                "Este registro es de un entorno productivo o de origen "
                "desconocido: marcá la conformidad para continuar."))
        record.with_delay(
            description=_("Eliminar DNS: %s") % record.name
        ).job_delete()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Borrado DNS encolado."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
