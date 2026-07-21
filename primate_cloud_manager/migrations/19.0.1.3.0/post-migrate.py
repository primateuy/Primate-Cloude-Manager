# -*- coding: utf-8 -*-
"""Reconciliación del set de acentos contra el handoff de diseño.

El set de 5 acentos del handoff (violet/blue/teal/emerald/amber) reemplaza al set
anterior (teal/naranja/azul/violeta/verde/indigo). Se mapea la preferencia
guardada de cada usuario al nuevo valor equivalente; lo que no mapea cae en el
default (violet). Es solo preferencia de UI (capa de presentación).
"""
import logging

_logger = logging.getLogger(__name__)

# Acento viejo -> acento nuevo del handoff.
_ACCENT_MAP = {
    "indigo": "violet",
    "violeta": "violet",
    "naranja": "amber",
    "azul": "blue",
    "verde": "emerald",
    "teal": "teal",  # se conserva (aunque cambió su hex)
}
_VALID = {"violet", "blue", "teal", "emerald", "amber"}


def migrate(cr, version):
    """Mapea los acentos viejos guardados en res.users al set nuevo."""
    cr.execute("SELECT id, pcm_accent FROM res_users WHERE pcm_accent IS NOT NULL")
    for uid, old in cr.fetchall():
        if old in _VALID:
            continue
        new = _ACCENT_MAP.get(old, "violet")
        cr.execute("UPDATE res_users SET pcm_accent = %s WHERE id = %s", (new, uid))
    _logger.info("Acentos PCM reconciliados al set del handoff (5 acentos).")
