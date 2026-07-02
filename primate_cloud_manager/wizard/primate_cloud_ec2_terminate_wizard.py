# -*- coding: utf-8 -*-
"""Wizard de terminación de EC2 con doble confirmación (operación destructiva)."""
from odoo import _, fields, models
from odoo.exceptions import UserError


class PrimateCloudEc2TerminateWizard(models.TransientModel):
    """Exige escribir el nombre y marcar la conformidad antes de terminar."""

    _name = "primate.cloud.ec2.terminate.wizard"
    _description = "Terminar instancia EC2"

    instance_id = fields.Many2one(
        "primate.cloud.ec2.instance",
        string="Instancia",
        required=True,
        ondelete="cascade",
        readonly=True,
    )
    instance_name = fields.Char(related="instance_id.name", string="Nombre", readonly=True)
    confirm_name = fields.Char(
        string="Escribí el nombre de la instancia",
        help="Para confirmar, escribí exactamente el nombre de la instancia.",
    )
    acknowledge = fields.Boolean(
        string="Entiendo que esta acción es permanente e irreversible",
    )

    def action_confirm(self):
        """Valida la doble confirmación y encola la terminación."""
        self.ensure_one()
        if not self.acknowledge:
            raise UserError(_("Debés marcar la conformidad para continuar."))
        if (self.confirm_name or "").strip() != (self.instance_id.name or "").strip():
            raise UserError(
                _("El nombre escrito no coincide con el de la instancia.")
            )
        # action_terminate revalida el grupo cloud_admin y encola el job.
        return self.instance_id.action_terminate()
