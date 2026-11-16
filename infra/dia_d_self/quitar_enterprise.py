# -*- coding: utf-8 -*-
"""Desinstala los 13 módulos Enterprise EN LA COPIA pcm_mudanza (día D, F2).

Se corre por stdin de `odoo-bin shell -d pcm_mudanza` (lo hace el driver).
Dos pasadas (la 1ª cascadea ai_fields/ai_server_actions). Espejo del ensayo
del 2026-11-10 y de MUDANZA_PCM_SELF.md §0.c. Termina imprimiendo
PROBE_LISTA solo si quedó 0 OEEL.
"""
PASADA_1 = ["web_enterprise", "web_mobile", "mail_mobile", "mail_enterprise",
            "web_cohort", "web_gantt", "web_grid", "web_map", "ai",
            "iap_extract"]
PASADA_2 = ["ai_auto_install"]

assert env.cr.dbname == "pcm_mudanza", \
    "¡Este script es SOLO para la copia! Base actual: %s" % env.cr.dbname

Mod = env["ir.module.module"]
Mod.update_list()
for pasada in (PASADA_1, PASADA_2):
    mods = Mod.search([("name", "in", pasada), ("state", "=", "installed")])
    if mods:
        print("desinstalando: %s" % ", ".join(mods.mapped("name")))
        mods.button_immediate_uninstall()

resto = Mod.search([("state", "=", "installed"), ("license", "like", "OEEL%")])
assert not resto, "siguen instalados OEEL: %s" % resto.mapped("name")
cuenta = Mod.search_count([("state", "=", "installed")])
pcm = Mod.search([("name", "=", "primate_cloud_manager")])
assert pcm.state == "installed", "¡primate_cloud_manager no quedó instalado!"
env.cr.commit()
print("instalados tras la limpieza: %d · 0 OEEL" % cuenta)
print("PROBE_LISTA")
