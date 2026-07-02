# -*- coding: utf-8 -*-
"""Proyecto: agrupación lógica de entornos por cliente/iniciativa."""
from odoo import fields, models


class PrimateCloudProject(models.Model):
    """Agrupa entornos bajo un cliente o iniciativa (ej.: Forum, Abastecimientos)."""

    _name = "primate.cloud.project"
    _description = "Proyecto Cloud"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS principal",
        ondelete="restrict",
        help="Cuenta AWS usada por defecto para los entornos de este proyecto.",
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Cliente",
        ondelete="restrict",
    )
    environment_ids = fields.One2many(
        "primate.cloud.environment",
        "project_id",
        string="Entornos",
    )
    environment_count = fields.Integer(
        string="N.º de entornos",
        compute="_compute_environment_count",
    )
    active = fields.Boolean(string="Activo", default=True)
    notes = fields.Text(string="Notas")

    def _compute_environment_count(self):
        """Cuenta los entornos del proyecto para el botón estadístico."""
        data = self.env["primate.cloud.environment"]._read_group(
            [("project_id", "in", self.ids)],
            groupby=["project_id"],
            aggregates=["__count"],
        )
        mapped = {project.id: count for project, count in data}
        for project in self:
            project.environment_count = mapped.get(project.id, 0)

    def action_view_environments(self):
        """Abre los entornos del proyecto."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": "primate.cloud.environment",
            "view_mode": "list,form",
            "domain": [("project_id", "=", self.id)],
            "context": {"default_project_id": self.id},
        }
