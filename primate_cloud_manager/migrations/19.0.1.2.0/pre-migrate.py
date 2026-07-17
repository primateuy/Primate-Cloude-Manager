# -*- coding: utf-8 -*-
"""Prerequisito de R5: ``project.partner_id`` pasa a ``required=True``.

Corre ANTES de que el ORM aplique el ``NOT NULL`` (por eso es *pre*-migrate: en
*post* el schema ya habría fallado sobre los NULL existentes). Backfillea los
proyectos SIN cliente con un partner CENTINELA "⚠ SIN CLIENTE (asignar)":
visible y marcado, coherente con el proyecto de fallback de R1 (que a propósito
NO tenía cliente, para no inventar uno). El id del centinela se guarda en el
system param ``pcm.unassigned_partner_id`` (a prueba de renombre) para que el
resto del código (re-tag dedicado, lectura de shares) lo reconozca sin depender
del texto del nombre.

Nota de mecánica: en el stage 'pre' los modelos de ESTE módulo todavía NO están
en el registry (``primate.cloud.project`` daría KeyError) — por eso la tabla
propia se toca por SQL crudo. ``res.partner`` e ``ir.config_parameter`` son de
``base`` (ya cargado): esos sí por ORM. Idempotente.
"""
from odoo import SUPERUSER_ID, api

SENTINEL_NAME = "⚠ SIN CLIENTE (asignar)"
SENTINEL_PARAM = "pcm.unassigned_partner_id"


def migrate(cr, version):
    # La tabla existe; el MODELO aún no está en el registry (stage 'pre').
    cr.execute(
        "SELECT id FROM primate_cloud_project WHERE partner_id IS NULL")
    orphan_ids = [row[0] for row in cr.fetchall()]
    if not orphan_ids:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    Params = env["ir.config_parameter"].sudo()
    Partner = env["res.partner"].sudo()

    # Find-or-create del centinela: primero por el param (rename-proof), luego
    # por nombre (por si el param se perdió), y recién ahí se crea.
    sentinel = Partner.browse(
        int(Params.get_param(SENTINEL_PARAM) or 0)).exists()
    if not sentinel:
        sentinel = Partner.search([("name", "=", SENTINEL_NAME)], limit=1)
    if not sentinel:
        sentinel = Partner.create({
            "name": SENTINEL_NAME,
            "is_company": True,
            "comment": "Partner centinela de primate_cloud_manager: marca "
                       "proyectos SIN cliente asignado (R5). NO es un cliente "
                       "real — reasignar el cliente verdadero cuando exista.",
        })
    Params.set_param(SENTINEL_PARAM, str(sentinel.id))

    cr.execute(
        "UPDATE primate_cloud_project SET partner_id = %s "
        "WHERE partner_id IS NULL",
        (sentinel.id,))
