# -*- coding: utf-8 -*-
"""Wizard de terminación de EC2 con doble confirmación (operación destructiva).

R4-B6.3: terminar un SERVIDOR mata la máquina y con ella TODAS las instancias
que hospeda — de varios clientes. La confirmación deja de ser el "¿seguro?"
genérico: lista a quiénes se lleva puestos y realza si hay producción. La
AUTORIZACIÓN (D-B6.5 corregido) es admin-only declarativo (ACL) — no hay en el
modelo una asignación operador→cliente para decidirlo en runtime; el radio de
daño se muestra igual porque le sirve al admin.
"""
from odoo import _, api, fields, models
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
        string="Escribí el nombre del servidor",
        help="Para confirmar, escribí exactamente el nombre del servidor.",
    )
    acknowledge = fields.Boolean(
        string="Entiendo que esta acción es permanente e irreversible",
    )
    # R4-B6.3: radio de daño (a quiénes se lleva puestos) — útil para el admin.
    affected_text = fields.Text(
        string="Instancias afectadas", compute="_compute_affected", readonly=True,
    )
    affected_clients = fields.Char(
        string="Clientes afectados", compute="_compute_affected", readonly=True,
    )
    hosts_production = fields.Boolean(
        compute="_compute_affected", readonly=True,
    )

    @api.depends("instance_id")
    def _compute_affected(self):
        """Instancias/clientes que la terminación se lleva puestos + producción."""
        for wizard in self:
            env = wizard.instance_id.environment_id
            hosted = env.instance_ids.filtered(
                lambda i: i.state != "archived") if env else self.env[
                "primate.cloud.instance"].browse()
            lines = []
            clients = hosted.project_id
            for inst in hosted:
                mark = " ⚠ PRODUCCIÓN" if inst.env_type == "production" else ""
                lines.append("• %s (%s)%s" % (
                    inst.name, inst.project_id.display_name or "sin cliente", mark))
            wizard.affected_text = "\n".join(lines) or _("(sin instancias)")
            wizard.affected_clients = ", ".join(
                sorted(c.display_name for c in clients)) or "—"
            wizard.hosts_production = any(
                i.env_type == "production" for i in hosted)

    def action_confirm(self):
        """Valida la confirmación y encola la terminación (admin-only en el job)."""
        self.ensure_one()
        if not self.acknowledge:
            raise UserError(_("Debés marcar la conformidad para continuar."))
        if (self.confirm_name or "").strip() != (self.instance_id.name or "").strip():
            raise UserError(
                _("El nombre escrito no coincide con el del servidor.")
            )
        # action_terminate valida el grupo (admin-only, server-side) y encola.
        return self.instance_id.action_terminate()
