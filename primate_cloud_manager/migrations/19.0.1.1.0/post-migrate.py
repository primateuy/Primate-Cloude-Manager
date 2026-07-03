# -*- coding: utf-8 -*-
"""Backfill del identificador estable pcm_ref (Fase 9, Opción A).

Rellena el `pcm_ref` de los entornos existentes y de los partners que son
cliente de un proyecto PCM. Corre después de que el ORM creó las columnas.

Reglas (confirmadas en el diseño):
- **Solo puebla vacíos.** NUNCA pisa un ref existente: re-ejecutar la migración
  o reinstalar el módulo no cambia la clave de un recurso ya taggeado (lo
  huérfanaría). El ``WHERE pcm_ref IS NULL OR pcm_ref = ''`` lo garantiza.
- **Partners: solo los vinculados a un proyecto PCM** (lazy — no se le genera
  ref a cada contacto de Odoo).
- **uuid en Python**, no en SQL: DB-agnóstico y con el mismo formato que
  ``_new_pcm_ref`` / ``_ensure_pcm_ref``.
"""
import uuid


def migrate(cr, version):
    # Entornos sin ref -> pcm_env_<uuid>.
    cr.execute(
        "SELECT id FROM primate_cloud_environment "
        "WHERE pcm_ref IS NULL OR pcm_ref = ''"
    )
    for (env_id,) in cr.fetchall():
        cr.execute(
            "UPDATE primate_cloud_environment SET pcm_ref = %s WHERE id = %s",
            ("pcm_env_" + uuid.uuid4().hex, env_id),
        )

    # Partners que son cliente de algún proyecto PCM y no tienen ref.
    cr.execute(
        "SELECT p.id FROM res_partner p "
        "WHERE (p.pcm_ref IS NULL OR p.pcm_ref = '') "
        "AND EXISTS (SELECT 1 FROM primate_cloud_project pr "
        "            WHERE pr.partner_id = p.id)"
    )
    for (partner_id,) in cr.fetchall():
        cr.execute(
            "UPDATE res_partner SET pcm_ref = %s WHERE id = %s",
            ("pcm_cli_" + uuid.uuid4().hex, partner_id),
        )
