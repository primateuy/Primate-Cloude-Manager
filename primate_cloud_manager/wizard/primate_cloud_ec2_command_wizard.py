# -*- coding: utf-8 -*-
"""Wizard para ejecutar un comando vía SSM sobre una instancia EC2."""
from odoo import _, fields, models
from odoo.exceptions import UserError


class PrimateCloudEc2CommandWizard(models.TransientModel):
    """Captura el comando a ejecutar y lo encola como job SSM."""

    _name = "primate.cloud.ec2.command.wizard"
    _description = "Ejecutar comando en EC2"

    instance_id = fields.Many2one(
        "primate.cloud.ec2.instance",
        string="Instancia",
        required=True,
        ondelete="cascade",
    )
    command = fields.Text(
        string="Comando",
        required=True,
        help="Comando de shell a ejecutar en la instancia vía SSM.",
    )

    def action_run(self):
        """Encola la ejecución del comando. La salida aparece en el chatter."""
        self.ensure_one()
        if not (self.command or "").strip():
            raise UserError(_("Ingresá un comando para ejecutar."))
        self.instance_id.with_delay(
            description=_("Comando SSM: %s") % self.instance_id.name
        ).job_execute_command(self.command)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _("Comando encolado. La salida quedará en el historial de la instancia."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
