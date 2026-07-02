# -*- coding: utf-8 -*-
"""Commit de un repositorio sincronizado desde GitHub (Fase 5)."""
from odoo import api, fields, models


class PrimateCloudCommit(models.Model):
    """Entrada del historial de commits de un repositorio."""

    _name = "primate.cloud.commit"
    _description = "Commit de Repositorio"
    _order = "commit_date desc, id desc"

    repository_id = fields.Many2one(
        "primate.cloud.repository",
        string="Repositorio",
        required=True,
        ondelete="cascade",
        index=True,
    )
    commit_hash = fields.Char(string="Hash", required=True, index=True)
    message = fields.Text(string="Mensaje")
    author = fields.Char(string="Autor")
    commit_date = fields.Datetime(string="Fecha")
    branch = fields.Char(string="Rama")
    # Marca el commit que está actualmente desplegado en el servidor.
    is_current = fields.Boolean(string="Actual en el servidor", index=True)

    _commit_uniq = models.Constraint(
        "UNIQUE(repository_id, commit_hash)",
        "Ese commit ya está registrado para el repositorio.",
    )

    @api.depends("commit_hash", "message")
    def _compute_display_name(self):
        """Nombre legible: hash corto + primera línea del mensaje."""
        for commit in self:
            short = (commit.commit_hash or "")[:8]
            title = (commit.message or "").splitlines()[0] if commit.message else ""
            commit.display_name = ("%s %s" % (short, title)).strip() or short
