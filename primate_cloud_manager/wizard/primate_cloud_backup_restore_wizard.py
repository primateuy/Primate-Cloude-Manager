# -*- coding: utf-8 -*-
"""Asistente de restauración de un backup (Fase 8, Bloque 4).

La operación más destructiva del módulo: reemplaza la base (y filestore) del
destino. Salvaguardas del diseño aprobado:

- El destino NUNCA defaultea a producción (ni siquiera como "vuelta al origen").
- Restaurar sobre producción exige tipear el nombre EXACTO del entorno,
  validado server-side, además del diálogo de confirmación del botón.
- Pre-backup del destino activado por default; si falla, el job aborta.
"""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Nombre de base PostgreSQL admitido (también viaja dentro de un SQL de
# pg_terminate_backend: nada de comillas ni caracteres de shell).
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


class PrimateCloudBackupRestoreWizard(models.TransientModel):
    """Wizard: elegir destino y salvaguardas, y encolar el restore."""

    _name = "primate.cloud.backup.restore.wizard"
    _description = "Asistente de Restauración de Backup"

    backup_id = fields.Many2one(
        "primate.cloud.backup",
        string="Backup",
        required=True,
        readonly=True,
        ondelete="cascade",
        # Sin filtro por tipo/nombre: un "Pre-restore …" también es un origen
        # válido (es el camino de recuperación de un restore fallido).
        domain=[("state", "=", "completed")],
    )
    backup_environment_id = fields.Many2one(
        related="backup_id.environment_id", string="Entorno de origen"
    )
    backup_date = fields.Datetime(related="backup_id.backup_date")
    backup_s3_key = fields.Char(related="backup_id.s3_key")
    backup_size_mb = fields.Float(related="backup_id.size_mb")

    target_environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno destino",
        required=True,
        ondelete="cascade",
        domain=[("state", "in", ("active", "error"))],
    )
    target_is_production = fields.Boolean(
        compute="_compute_target_is_production"
    )
    target_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance",
        string="Instancia destino",
        required=True,
        ondelete="cascade",
    )
    target_db_name = fields.Char(string="Base de datos destino", required=True)
    pre_backup = fields.Boolean(
        string="Respaldar el destino antes",
        default=True,
        help="Red de seguridad: respalda la base destino antes de "
             "reemplazarla. Si el pre-backup falla, el restore se aborta.",
    )
    confirm_environment_name = fields.Char(
        string="Confirmá el nombre del entorno",
        help="Escribí el nombre EXACTO del entorno de producción destino.",
    )

    @api.model
    def default_get(self, fields_list):
        """Nunca producción por default: si el backup viene de producción,
        el destino queda vacío y el operador elige a conciencia."""
        res = super().default_get(fields_list)
        backup = self.env["primate.cloud.backup"].browse(
            res.get("backup_id") or 0
        ).exists()
        if backup:
            origin = backup.environment_id
            if origin.env_type != "production":
                res.setdefault("target_environment_id", origin.id)
            if backup.database_id:
                res.setdefault("target_db_name", backup.database_id.name)
        return res

    @api.depends("target_environment_id")
    def _compute_target_is_production(self):
        for wizard in self:
            wizard.target_is_production = (
                wizard.target_environment_id.env_type == "production"
            )

    @api.onchange("target_environment_id")
    def _onchange_target_environment(self):
        # Sugerencia de UI (el operador puede cambiarla): la primera instancia
        # corriendo del entorno destino.
        self.target_instance_id = self.target_environment_id.ec2_instance_ids \
            .filtered(lambda i: i.instance_state == "running")[:1]

    def action_restore(self):
        """Valida las salvaguardas y encola el job sobre el entorno destino."""
        self.ensure_one()
        backup = self.backup_id
        target = self.target_environment_id
        if backup.state != "completed":
            raise UserError(_("El backup ya no está disponible (estado: %s).")
                            % backup.state)
        if (not self.target_instance_id
                or self.target_instance_id.environment_id != target):
            raise UserError(_("Elegí una instancia EC2 del entorno destino."))
        if not DB_NAME_RE.match(self.target_db_name or ""):
            raise UserError(_(
                "Nombre de base inválido: solo letras, números, guion y "
                "guion bajo (sin espacios)."))
        if (self.target_is_production
                and (self.confirm_environment_name or "").strip() != target.name):
            raise UserError(_(
                "Para restaurar sobre PRODUCCIÓN escribí el nombre exacto "
                "del entorno destino ('%s').") % target.name)
        target.with_delay(
            description=_("Restaurar backup en %s") % target.name
        ).job_restore_backup({
            "backup_id": backup.id,
            "db_name": self.target_db_name,
            "instance_id": self.target_instance_id.id,
            "pre_backup": self.pre_backup,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Restore encolado. Seguilo en el chatter "
                                    "del entorno destino y en la bitácora."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
