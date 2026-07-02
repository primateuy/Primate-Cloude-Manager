# -*- coding: utf-8 -*-
"""Política de respaldo esperada por entorno (Fase 8).

Catálogo de políticas (spec §5.9 + §10.1). El módulo no impone una política
única: cada entorno tiene asignada una política *esperada* y el validador la
compara contra lo que efectivamente detecta en AWS. Además (delta aprobado
sobre la spec §10), una política puede marcarse como **gestionada por PCM**:
en ese caso el módulo también *ejecuta* el backup (pg_dump + filestore vía
SSM → S3), necesario en entornos con PostgreSQL local donde AWS no detecta nada.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PrimateCloudBackupPolicy(models.Model):
    """Política de respaldo: qué frecuencia y retención se espera (y ejecuta)."""

    _name = "primate.cloud.backup.policy"
    _description = "Política de Respaldo Cloud"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    policy_type = fields.Selection(
        [
            ("none", "Sin respaldo gestionado"),
            ("basic", "Básica"),
            ("standard", "Estándar"),
            ("critical", "Crítica"),
            ("custom", "Personalizada"),
        ],
        string="Tipo",
        required=True,
        default="custom",
    )
    expected_frequency = fields.Selection(
        [
            ("daily", "Diaria"),
            ("twice_daily", "Dos veces al día"),
            ("hourly", "Cada hora"),
            ("manual", "Manual"),
        ],
        string="Frecuencia esperada",
    )
    expected_retention_days = fields.Integer(string="Retención esperada (días)")
    description = fields.Text(string="Descripción")
    active = fields.Boolean(string="Activa", default=True)

    # --- Ejecución gestionada por PCM (delta aprobado sobre spec §10) ---
    managed_by_pcm = fields.Boolean(
        string="Gestionada por PCM",
        help="Si está activo, PCM ejecuta el backup (pg_dump + filestore vía SSM "
             "hacia S3) además de validar el cumplimiento.",
    )
    s3_bucket = fields.Char(
        string="Bucket S3",
        help="Destino de los dumps gestionados. La EC2 sube con su instance profile.",
    )
    s3_prefix = fields.Char(string="Prefijo S3", default="pcm-backups")
    execution_hour = fields.Float(
        string="Hora de ejecución (UTC)",
        default=3.0,
        help="Hora de la ventana diaria de ejecución para políticas gestionadas.",
    )

    environment_ids = fields.One2many(
        "primate.cloud.environment", "backup_policy_id", string="Entornos"
    )

    @api.constrains("expected_retention_days")
    def _check_retention(self):
        for policy in self:
            if policy.expected_retention_days < 0:
                raise ValidationError(_("La retención esperada no puede ser negativa."))

    @api.constrains("managed_by_pcm", "s3_bucket")
    def _check_managed_bucket(self):
        for policy in self:
            if policy.managed_by_pcm and not policy.s3_bucket:
                raise ValidationError(
                    _("Una política gestionada por PCM necesita un bucket S3 de destino.")
                )
