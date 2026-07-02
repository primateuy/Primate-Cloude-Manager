# -*- coding: utf-8 -*-
"""Preferencia de acento de color del usuario para la app PCM.

Aditivo y de solo preferencia: no toca lógica de negocio. El navy de la app es
fijo; solo cambia el acento (navegación/marca/acciones). Los badges de estado
tienen su propia paleta y NO dependen del acento.
"""
from odoo import fields, models

# Presets de acento (el JS de la app deriva accent/ink/tint de cada uno).
PCM_ACCENTS = [
    ("teal", "Teal"),
    ("naranja", "Naranja"),
    ("azul", "Azul"),
    ("violeta", "Violeta"),
    ("verde", "Verde"),
]


class ResUsers(models.Model):
    _inherit = "res.users"

    pcm_accent = fields.Selection(
        PCM_ACCENTS, string="Acento PCM", default="teal",
        help="Color de acento de la interfaz de Primate Cloud Manager.",
    )
    pcm_accent_custom = fields.Char(
        string="Acento personalizado (hex)",
        help="Color hex libre (ej.: #3366FF). Si se define, tiene prioridad sobre el preset.",
    )

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["pcm_accent", "pcm_accent_custom"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ["pcm_accent", "pcm_accent_custom"]
