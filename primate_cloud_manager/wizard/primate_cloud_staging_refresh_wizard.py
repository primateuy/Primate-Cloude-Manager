# -*- coding: utf-8 -*-
"""Asistente de refresco de staging (Fase 8, Bloque 5 — spec §14.5).

Opciones al refrescar un staging desde su origen: base sola o base+repos,
re-neutralizar (default sí) y usar el último backup del origen en vez de
dumpear producción de nuevo.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PrimateCloudStagingRefreshWizard(models.TransientModel):
    """Wizard: opciones de refresco y encolado del job."""

    _name = "primate.cloud.staging.refresh.wizard"
    _description = "Asistente de Refresco de Staging"

    staging_id = fields.Many2one(
        "primate.cloud.environment",
        string="Staging",
        required=True,
        readonly=True,
        ondelete="cascade",
        domain=[("env_type", "=", "staging")],
    )
    origin_environment_id = fields.Many2one(
        related="staging_id.origin_environment_id", string="Entorno origen"
    )
    refresh_repos = fields.Boolean(
        string="Refrescar también los repositorios",
        help="Además de la base, actualiza los repos del staging al commit "
             "actual del origen (fetch + checkout).",
    )
    re_neutralize = fields.Boolean(
        string="Ejecutar neutralización nuevamente",
        default=True,
        help="Recomendado: re-aplica el script de neutralización sobre la "
             "base recién restaurada. Desactivalo solo a conciencia.",
    )
    use_last_backup = fields.Boolean(
        string="Usar el último backup del origen",
        help="Restaura el último backup completado en vez de dumpear "
             "producción de nuevo.",
    )

    def action_refresh(self):
        """Valida y encola el refresco con las opciones elegidas."""
        self.ensure_one()
        staging = self.staging_id
        if staging.env_type != "staging":
            raise UserError(_("Refrescar solo aplica a entornos de tipo staging."))
        if not staging.origin_environment_id:
            raise UserError(_("El staging no tiene entorno origen registrado."))
        staging.with_delay(
            description=_("Refrescar staging: %s") % staging.name
        ).job_refresh_staging({
            "refresh_repos": self.refresh_repos,
            "neutralize": self.re_neutralize,
            "use_last_backup": self.use_last_backup,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Refresco de staging encolado."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
