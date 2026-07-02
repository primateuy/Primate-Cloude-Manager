# -*- coding: utf-8 -*-
"""Registro de backups efectivos (Fase 8).

Modelo nuevo (delta aprobado sobre la spec): cada backup real —ejecutado por
PCM o detectado en AWS— queda asentado acá. Es la evidencia que consume el
validador de cumplimiento y el punto de partida del restore.

Inmutable estilo bitácora: los registros los crean y cierran los jobs; desde
la interfaz no se editan ni se eliminan. Solo se permiten los writes de cierre
de ciclo (estado, tamaño, keys, error) que hace el propio código.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Campos que el código puede actualizar al cerrar el ciclo de un backup
# (in_progress → completed/failed, expiración). Todo lo demás es inmutable.
UPDATABLE_FIELDS = {
    "state",
    "size_mb",
    "s3_key",
    "s3_filestore_key",
    "error_message",
    "expiry_date",
}


class PrimateCloudBackup(models.Model):
    """Un backup concreto de una base de un entorno (ejecutado o detectado)."""

    _name = "primate.cloud.backup"
    _description = "Backup Cloud"
    _order = "backup_date desc, id desc"

    name = fields.Char(string="Backup", required=True, readonly=True)
    environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno",
        required=True,
        readonly=True,
        ondelete="restrict",
        index=True,
    )
    database_id = fields.Many2one(
        "primate.cloud.database",
        string="Base de datos",
        readonly=True,
        ondelete="set null",
        index=True,
    )
    backup_type = fields.Selection(
        [
            ("pcm_dump", "Dump gestionado por PCM"),
            ("rds_automated", "Backup automático RDS"),
            ("rds_snapshot", "Snapshot manual RDS"),
        ],
        string="Tipo",
        required=True,
        readonly=True,
        default="pcm_dump",
    )
    source = fields.Selection(
        [("executed", "Ejecutado por PCM"), ("detected", "Detectado en AWS")],
        string="Origen",
        required=True,
        readonly=True,
        default="executed",
    )
    backup_date = fields.Datetime(
        string="Fecha",
        required=True,
        readonly=True,
        default=fields.Datetime.now,
        index=True,
    )
    state = fields.Selection(
        [
            ("in_progress", "En progreso"),
            ("completed", "Completado"),
            ("failed", "Fallido"),
            ("expired", "Expirado"),
        ],
        string="Estado",
        required=True,
        default="in_progress",
        index=True,
    )
    s3_key = fields.Char(string="Key S3 (dump)")
    s3_filestore_key = fields.Char(string="Key S3 (filestore)")
    size_mb = fields.Float(string="Tamaño (MB)")
    expiry_date = fields.Datetime(
        string="Expira",
        help="Fecha de backup + retención de la política al momento de ejecutarlo.",
    )
    error_message = fields.Text(string="Mensaje de error")

    def write(self, vals):
        """Solo permite los writes de cierre de ciclo que hace el código."""
        forbidden = set(vals) - UPDATABLE_FIELDS
        if forbidden:
            raise UserError(
                _("El registro de backups es inmutable: no puede editarse (%s).")
                % ", ".join(sorted(forbidden))
            )
        return super().write(vals)

    def unlink(self):
        """Bloquea toda eliminación: el registro de backups es evidencia."""
        raise UserError(_("El registro de backups es inmutable: no puede eliminarse."))
