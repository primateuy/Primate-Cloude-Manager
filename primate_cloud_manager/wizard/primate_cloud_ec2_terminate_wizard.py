# -*- coding: utf-8 -*-
"""Wizard de terminación de EC2 con doble confirmación (operación destructiva).

R4-B6.3: terminar un SERVIDOR mata la máquina y con ella TODAS las instancias
que hospeda — de varios clientes. La confirmación deja de ser el "¿seguro?"
genérico: lista a quiénes se lleva puestos, realza si hay producción, y —
cuando hospeda proyectos AJENOS al operador— exige ``group_cloud_admin``
(D-B6.5: la fricción de UX no es un control de autorización).
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
    # R4-B6.3: radio de daño multi-cliente, calculado del servidor.
    affected_text = fields.Text(
        string="Instancias afectadas", compute="_compute_affected", readonly=True,
    )
    affected_clients = fields.Char(
        string="Clientes afectados", compute="_compute_affected", readonly=True,
    )
    hosts_production = fields.Boolean(
        compute="_compute_affected", readonly=True,
    )
    is_cross_client = fields.Boolean(
        string="Afecta a otros clientes", compute="_compute_affected", readonly=True,
    )
    ack_clients = fields.Boolean(
        string="Reconozco que afecta a los clientes listados arriba",
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
            # Cross-cliente = hospeda un proyecto que NO es del operador.
            wizard.is_cross_client = wizard.instance_id._terminate_is_cross_client()

    def action_confirm(self):
        """Valida la confirmación (fricción + gobierno) y encola la terminación."""
        self.ensure_one()
        if not self.acknowledge:
            raise UserError(_("Debés marcar la conformidad para continuar."))
        if (self.confirm_name or "").strip() != (self.instance_id.name or "").strip():
            raise UserError(
                _("El nombre escrito no coincide con el del servidor.")
            )
        # Radio multi-cliente: ack explícito de los clientes afectados.
        if self.is_cross_client and not self.ack_clients:
            raise UserError(_(
                "Este servidor hospeda instancias de otros clientes (%s): "
                "reconocé explícitamente el impacto para continuar."
            ) % self.affected_clients)
        # action_terminate revalida el gobierno (admin-only cross-cliente,
        # server-side) y encola el job.
        return self.instance_id.action_terminate()
