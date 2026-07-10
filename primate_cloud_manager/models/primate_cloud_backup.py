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
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Umbral (horas) para dar por muerto un backup ``in_progress`` cuyo job no
# cerró el ciclo (OOM, reinicio del server, kill). Superado, el cron lo marca
# ``failed`` y la ventana vuelve a quedar descubierta para el reintento
# natural. Ningún backup gestionado debería tardar más que esto.
STUCK_IN_PROGRESS_HOURS = 2

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
    _inherit = ["primate.cloud.instance.linked"]
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
    purpose = fields.Selection(
        [
            ("scheduled", "Programado"),
            ("manual", "Manual"),
            ("staging", "Fuente de staging"),
            ("pre_restore", "Pre-restore"),
        ],
        string="Propósito",
        required=True,
        readonly=True,
        # Default "manual": los caminos reales SIEMPRE pasan el propósito
        # explícito, así que esto solo aplica a registros previos al campo
        # (backfill del upgrade) y a creates crudos — "Manual" es lo honesto
        # ahí, y el chip nunca renderiza vacío.
        default="manual",
        help="Para qué se ejecutó: la lista de respaldos cuenta la historia "
             "real. Todos cuentan igual para el validador (un backup es un "
             "backup).",
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
    s3_bucket = fields.Char(
        string="Bucket S3", readonly=True,
        help="Se fija al ejecutar: si la política cambia de bucket después, "
             "este backup sigue siendo localizable.",
    )
    s3_key = fields.Char(string="Key S3 (dump)")
    s3_filestore_key = fields.Char(string="Key S3 (filestore)")
    size_mb = fields.Float(string="Tamaño (MB)")
    expiry_date = fields.Datetime(
        string="Expira",
        help="Fecha de backup + retención de la política al momento de ejecutarlo.",
    )
    error_message = fields.Text(string="Mensaje de error")

    def _resolve_bucket(self):
        """Bucket S3 del backup, con fallback EXPLÍCITO para registros creados
        antes de que existiera el campo ``s3_bucket``: el bucket de la política
        del entorno de origen. Si ninguno está definido devuelve False — el
        llamador falla con mensaje claro, nunca se adivina un bucket.
        """
        self.ensure_one()
        return (self.s3_bucket
                or self.environment_id.backup_policy_id.s3_bucket
                or False)

    def action_restore(self):
        """Abre el asistente de restauración de este backup.

        Sin filtro por tipo/nombre: un registro "Pre-restore …" es un backup
        como cualquiera y puede ser el origen de una recuperación.
        """
        self.ensure_one()
        if self.state != "completed":
            raise UserError(
                _("Solo se puede restaurar un backup completado (estado: %s).")
                % self.state
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Restaurar backup"),
            "res_model": "primate.cloud.backup.restore.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_backup_id": self.id},
        }

    @api.model
    def _mark_stuck_failed(self, now=None):
        """Marca ``failed`` los ``in_progress`` zombis (job muerto sin cerrar).

        Sin esto, un registro colgado contaría como "ventana cubierta" para el
        cron y bloquearía el reintento. Se avisa en el chatter del entorno.

        Args:
            now (datetime, optional): inyectable para testear el umbral.

        Returns:
            recordset: los backups marcados.
        """
        now = now or fields.Datetime.now()
        stuck = self.search([
            ("state", "=", "in_progress"),
            ("backup_date", "<", now - timedelta(hours=STUCK_IN_PROGRESS_HOURS)),
        ])
        for backup in stuck:
            backup.write({
                "state": "failed",
                "error_message": _(
                    "Interrumpido: el job no cerró el registro (más de %s h "
                    "en progreso). Se reintenta en la próxima corrida del cron."
                ) % STUCK_IN_PROGRESS_HOURS,
            })
            backup.environment_id.message_post(
                body=_("Backup '%s' interrumpido (job muerto sin cerrar el "
                       "registro): marcado como fallido.") % backup.name)
        if stuck:
            _logger.warning("Backups in_progress zombis marcados failed: %s.",
                            len(stuck))
        return stuck

    @api.model
    def _cron_mark_expired(self):
        """Cron: marca ``expired`` los backups vencidos.

        El objeto en S3 ya lo borró (o borrará) la regla de lifecycle que PCM
        asegura al ejecutar; acá solo se refleja en el registro para que el
        validador y la UI no cuenten backups que ya no existen.
        """
        expired = self.search([
            ("state", "=", "completed"),
            ("expiry_date", "!=", False),
            ("expiry_date", "<", fields.Datetime.now()),
        ])
        if expired:
            expired.write({"state": "expired"})
            _logger.info("Backups marcados como expirados: %s.", len(expired))

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
