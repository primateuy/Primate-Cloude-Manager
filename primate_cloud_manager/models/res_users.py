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
    ("indigo", "Índigo"),
]

# Temas visuales: capa ORTOGONAL al acento. Cambian densidad/tipografía/forma vía
# tokens CSS, NUNCA la paleta ni el acento. El JS mapea cada tema a sus tokens.
PCM_THEMES = [
    ("a", "Consola técnica"),
    ("b", "Panel de operaciones"),
    ("c", "Editorial cálido"),
]


class ResUsers(models.Model):
    _inherit = "res.users"

    pcm_accent = fields.Selection(
        PCM_ACCENTS, string="Acento PCM", default="indigo",
        help="Color de acento de la interfaz de Primate Cloud Manager. Por "
             "defecto índigo (el acento del diseño de marca).",
    )
    pcm_accent_custom = fields.Char(
        string="Acento personalizado (hex)",
        help="Color hex libre (ej.: #3366FF). Si se define, tiene prioridad sobre el preset.",
    )
    pcm_theme = fields.Selection(
        PCM_THEMES, string="Tema PCM", default="b",
        help="Estilo visual (densidad, tipografía, forma). No cambia los "
             "colores; el acento y los estados son independientes. Por defecto "
             "'Panel de operaciones' (el look de marca).",
    )
    pcm_appearance = fields.Selection(
        [("light", "Claro"), ("dark", "Oscuro")],
        string="Apariencia PCM", default="light",
        help="Modo claro u oscuro. 3ª capa ortogonal: cambia solo la paleta base "
             "de color, no el tema ni el acento. Por defecto claro.",
    )

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + [
            "pcm_accent", "pcm_accent_custom", "pcm_theme", "pcm_appearance"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + [
            "pcm_accent", "pcm_accent_custom", "pcm_theme", "pcm_appearance"]
