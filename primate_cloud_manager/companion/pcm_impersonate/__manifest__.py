# -*- coding: utf-8 -*-
{
    "name": "PCM Impersonate (soporte)",
    "version": "19.0.2.0.0",
    "summary": "Endpoint de impersonación controlado por Primate Cloud Manager",
    "description": """
Addon companion desplegado por Primate Cloud Manager (PCM) en la instancia del
cliente. Expone UN endpoint (/pcm/impersonate) que, ante un token firmado por PCM
(Ed25519, la privada NUNCA está acá), abre una sesión como el usuario indicado
SIN leer ni tocar su contraseña.

Auditable por el cliente: este addon aparece instalado, el cliente ve el registro
de impersonaciones desde su propio Odoo y puede DESACTIVARLO (kill-switch), lo que
además corta las sesiones de soporte YA abiertas.
""",
    "author": "Primate",
    "license": "AGPL-3",
    "category": "Tools",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter.xml",
        "views/pcm_impersonate_views.xml",
    ],
    "installable": True,
    "application": False,
}
