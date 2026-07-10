# -*- coding: utf-8 -*-
"""Migración R1 (D6): environment viejo → servidor + su instancia primaria.

Solo para BASES DE PRUEBA (no hay producción). Se corre vía odoo shell en DOS
fases, controladas por la variable de entorno ``PCM_R1_MODE``:

1. ``snapshot`` — ANTES del primer ``-u`` con el código R1. Lee por SQL crudo
   las columnas viejas del entorno (env_type, odoo_version, ...) y las guarda
   en un JSON. Es imprescindible hacerlo antes: al convertirse esos campos en
   related-store, el ``-u`` los RECOMPUTA desde la instancia primaria (vacía
   aún) y pisa los valores.
2. ``apply`` — DESPUÉS del ``-u``. Crea la instancia primaria de cada entorno
   desde el snapshot y puebla ``instance_id`` en los satélites.

**Idempotente y re-ejecutable**: un entorno que ya tiene instancias se
saltea; los vínculos solo se pueblan si están VACÍOS (mismo principio que el
backfill del pcm_ref). Correrlo dos veces no duplica ni pisa.

**Sin huérfanos silenciosos**: si algún entorno del snapshot no tiene
proyecto, sus instancias van a un proyecto de fallback **visible y marcado**
("⚠ SIN CLIENTE (asignar)") — nunca a un placeholder que parezca cliente
real — y se listan al final para que el operador los reasigne. La instancia
exige project_id: el fallback evita el limbo sin inventar un cliente.

Uso (desde la raíz del repo):
    PCM_R1_MODE=snapshot .venv/bin/python <odoo-bin> shell -c <conf> -d <db> \
        --no-http < tools/migracion/migrate_r1.py       # antes del -u
    <odoo-bin> -c <conf> -d <db> -u primate_cloud_manager --stop-after-init
    PCM_R1_MODE=apply .venv/bin/python <odoo-bin> shell -c <conf> -d <db> \
        --no-http < tools/migracion/migrate_r1.py       # después del -u

El JSON vive junto a la BD que se migra: ``/tmp/pcm_r1_snapshot_<db>.json``
(o la ruta de ``PCM_R1_JSON``).
"""
import json
import os
import traceback

MODE = os.environ.get("PCM_R1_MODE", "")
JSON_PATH = os.environ.get(
    "PCM_R1_JSON", "/tmp/pcm_r1_snapshot_%s.json" % env.cr.dbname)

SNAPSHOT_COLUMNS = [
    "id", "name", "project_id", "state", "env_type", "odoo_version",
    "odoo_edition", "main_url", "backup_policy_id", "backup_compliance",
    "backup_compliance_detail", "last_backup_check", "origin_environment_id",
]

# Proyecto de fallback para entornos sin cliente: nombre INCONFUNDIBLE (el
# ⚠ y el imperativo evitan que parezca un cliente real en listas/kanban).
FALLBACK_PROJECT_NAME = "⚠ SIN CLIENTE (asignar) — migración R1"

# environment.state (máquina/flujo) → instance.state (el Odoo)
STATE_MAP = {
    "draft": "draft", "provisioning": "installing", "active": "active",
    "error": "error", "archived": "archived",
}


def do_snapshot():
    """Fase 1: volcado SQL crudo de las columnas viejas (pre -u)."""
    env.cr.execute("SELECT %s FROM primate_cloud_environment" %
                   ", ".join(SNAPSHOT_COLUMNS))
    rows = [dict(zip(SNAPSHOT_COLUMNS, row)) for row in env.cr.fetchall()]
    with open(JSON_PATH, "w") as handle:
        json.dump(rows, handle, default=str, ensure_ascii=False, indent=1)
    print("SNAPSHOT_OK: %s entornos → %s" % (len(rows), JSON_PATH))


def do_apply():
    """Fase 2: crea instancias desde el snapshot y puebla los vínculos."""
    with open(JSON_PATH) as handle:
        rows = json.load(handle)

    Environment = env["primate.cloud.environment"]
    Instance = env["primate.cloud.instance"]

    # Huérfanos (D6: instance.project_id es required): van a un proyecto de
    # fallback VISIBLE y marcado — se ve claro "necesitan cliente" en vez de
    # pasar desapercibidos. Los entornos borrados entre snapshot y apply se
    # ignoran.
    rows = [r for r in rows if Environment.browse(r["id"]).exists()]
    orphans = [r for r in rows if not r["project_id"]]
    if orphans:
        Project = env["primate.cloud.project"]
        fallback = Project.search(
            [("name", "=", FALLBACK_PROJECT_NAME)], limit=1)
        if not fallback:
            account = Environment.browse(orphans[0]["id"]).account_id
            fallback = Project.create({
                "name": FALLBACK_PROJECT_NAME,
                "account_id": account.id if account else False,
            })
        for row in orphans:
            row["project_id"] = fallback.id
        print("ATENCION — %s entorno(s) SIN proyecto: sus instancias van al "
              "proyecto de fallback %r. REASIGNALES cliente:" % (
                  len(orphans), FALLBACK_PROJECT_NAME))
        for row in orphans:
            print("  - id=%s %r" % (row["id"], row["name"]))

    created = skipped = 0
    by_env_instance = {}
    for row in rows:
        environment = Environment.browse(row["id"])
        if environment.instance_ids:
            # Idempotencia: ya migrado (o nacido post-R1) → se saltea.
            by_env_instance[row["id"]] = environment.primary_instance_id
            skipped += 1
            continue
        instance = Instance.create({
            "name": row["name"],
            "project_id": row["project_id"],
            "environment_id": row["id"],
            "state": STATE_MAP.get(row["state"], "draft"),
            "env_type": row["env_type"] or "production",
            "odoo_version": row["odoo_version"] or False,
            "odoo_edition": row["odoo_edition"] or False,
            "main_url": row["main_url"] or False,
            "backup_policy_id": row["backup_policy_id"] or False,
            "backup_compliance": row["backup_compliance"] or "no_policy",
            "backup_compliance_detail": row["backup_compliance_detail"] or False,
            "last_backup_check": row["last_backup_check"] or False,
        })
        by_env_instance[row["id"]] = instance
        created += 1

    # 2º pase: staging → instancia origen (necesita todas las instancias creadas).
    for row in rows:
        origin_env_id = row.get("origin_environment_id")
        instance = by_env_instance.get(row["id"])
        if origin_env_id and instance and not instance.origin_instance_id:
            origin = by_env_instance.get(origin_env_id) or Environment.browse(
                origin_env_id).primary_instance_id
            if origin:
                instance.origin_instance_id = origin.id

    # Satélites: poblar instance_id SOLO si está vacío (re-ejecutable).
    filled = {}
    for model in ("primate.cloud.repository", "primate.cloud.deployment",
                  "primate.cloud.dns.record", "primate.cloud.database"):
        records = env[model].search(
            [("instance_id", "=", False), ("environment_id", "!=", False)])
        count = 0
        for record in records:
            primary = record.environment_id.primary_instance_id
            if primary:
                record.instance_id = primary.id
                count += 1
        filled[model] = count

    # Backups: write() está bloqueado por inmutabilidad (por diseño) → SQL
    # directo, solo filas con instance_id NULL (mismo criterio que el resto).
    # FLUSH antes: primary_instance_id es un compute almacenado que hasta acá
    # vive en la caché ORM; el SQL crudo solo ve lo persistido (sin esto, la
    # 1ª corrida poblaba 0 backups y recién la 2ª los agarraba).
    env.flush_all()
    env.cr.execute("""
        UPDATE primate_cloud_backup b
           SET instance_id = e.primary_instance_id
          FROM primate_cloud_environment e
         WHERE b.environment_id = e.id
           AND b.instance_id IS NULL
           AND e.primary_instance_id IS NOT NULL
    """)
    filled["primate.cloud.backup (SQL)"] = env.cr.rowcount

    # Máquina 1:1 del servidor: la primera no terminada, solo si está vacía.
    linked_machines = 0
    for row in rows:
        environment = Environment.browse(row["id"])
        if environment.ec2_instance_id:
            continue
        machine = environment.ec2_instance_ids.filtered(
            lambda m: m.instance_state != "terminated")[:1]
        if machine:
            environment.ec2_instance_id = machine.id
            linked_machines += 1

    env.cr.commit()
    print("APPLY_OK: instancias creadas=%s, salteadas(ya migradas)=%s, "
          "máquinas vinculadas=%s" % (created, skipped, linked_machines))
    for model, count in filled.items():
        print("  vínculos poblados %s: %s" % (model, count))


try:
    if MODE == "snapshot":
        do_snapshot()
    elif MODE == "apply":
        do_apply()
    else:
        print("Definí PCM_R1_MODE=snapshot|apply")
except Exception:
    traceback.print_exc()
    print("MIGRACION_R1_FAIL")
