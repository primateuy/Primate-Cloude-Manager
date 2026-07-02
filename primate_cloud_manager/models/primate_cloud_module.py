# -*- coding: utf-8 -*-
"""Módulo Odoo detectado en un repositorio vs. instalado en la base (Fase 5)."""
from odoo import fields, models


class PrimateCloudModule(models.Model):
    """Módulo Odoo presente en el repositorio, cruzado contra la base remota."""

    _name = "primate.cloud.module"
    _description = "Módulo Odoo"
    _order = "technical_name"

    repository_id = fields.Many2one(
        "primate.cloud.repository",
        string="Repositorio",
        required=True,
        ondelete="cascade",
        index=True,
    )
    environment_id = fields.Many2one(
        related="repository_id.environment_id",
        string="Entorno",
        store=True,
        index=True,
    )
    technical_name = fields.Char(string="Nombre técnico", required=True, index=True)
    functional_name = fields.Char(string="Nombre funcional")
    author = fields.Char(string="Autor")
    category = fields.Char(string="Categoría")
    version = fields.Char(string="Versión (manifest)")
    depends = fields.Char(string="Dependencias", help="Lista de 'depends' del manifest.")
    # Estado del módulo en la base de datos remota (cruce vía SSM contra
    # ir_module_module). 'not_found' = presente en el repo pero ausente en la base.
    db_state = fields.Selection(
        [
            ("installed", "Instalado"),
            ("uninstalled", "No instalado"),
            ("to_upgrade", "Pendiente de actualización"),
            ("to_install", "Pendiente de instalación"),
            ("to_remove", "Pendiente de eliminación"),
            ("not_found", "No está en la base"),
        ],
        string="Estado en base",
        default="not_found",
    )
    db_version = fields.Char(string="Versión instalada", help="latest_version en la base.")
    last_sync_date = fields.Datetime(string="Última detección", readonly=True)

    _module_uniq = models.Constraint(
        "UNIQUE(repository_id, technical_name)",
        "Ese módulo ya está registrado para el repositorio.",
    )
