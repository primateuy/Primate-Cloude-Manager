# -*- coding: utf-8 -*-
"""Enforcement por request de las sesiones de impersonación (kill-switch).

Corre en el path de request del Odoo del CLIENTE, así que es DEFENSIVO: solo mira
sesiones marcadas como impersonación PCM (``pcm_imp`` en la sesión); jamás toca
una sesión normal. Si el addon se deshabilita, se sube el epoch o la sesión
expira, la próxima request de esa sesión de soporte queda deslogueada.
"""
import time

from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _pcm_enforce(cls):
        """Corta la sesión de impersonación si ya no debe seguir viva."""
        if not request or not getattr(request, "session", None):
            return
        marker = request.session.get("pcm_imp")
        if not marker:
            return  # sesión normal → nunca se toca
        Log = request.env["pcm.impersonation.log"].sudo()
        reason = None
        try:
            if not Log._is_enabled():
                reason = "disabled"
            elif int(marker.get("epoch", -1)) != Log._epoch():
                reason = "epoch"
            elif (time.time() - float(marker.get("created", 0))
                    > Log._max_hours() * 3600):
                reason = "expired"
        except Exception:  # noqa: BLE001 - ante la duda, cortar (fail-closed)
            reason = "error"
        if reason:
            try:
                log_id = marker.get("log_id")
                if log_id:
                    Log.browse(log_id).sudo().write({"ended_reason": reason})
            except Exception:  # noqa: BLE001
                pass
            request.session.logout(keep_db=True)

    @classmethod
    def _authenticate(cls, endpoint):
        result = super()._authenticate(endpoint)
        try:
            cls._pcm_enforce()
        except Exception:  # noqa: BLE001 - nunca romper la request del cliente
            pass
        return result

    def session_info(self):
        """Expone flags para el gancho del banner (off por default)."""
        info = super().session_info()
        try:
            marker = request.session.get("pcm_imp") if request else None
            Log = self.env["pcm.impersonation.log"].sudo()
            info["pcm_impersonation"] = bool(marker)
            info["pcm_impersonation_banner"] = bool(marker) and Log._banner_on()
        except Exception:  # noqa: BLE001
            info["pcm_impersonation"] = False
            info["pcm_impersonation_banner"] = False
        return info
