"""Prepara/limpia fixtures para el smoke-test de UI (correr con `odoo shell`).

Crea (idempotente) un entorno DRAFT atado a una cuenta AWS de PRUEBA (credenciales
falsas), para ejercitar el ciclo error->corrección->éxito del drawer sin crear
recursos reales: el "éxito" encola un job que falla con AuthFailure y no factura.

Uso:
    .venv/bin/python <odoo-bin> shell -c <conf> -d <db> --no-http \
        < tools/smoke_ui/setup_fixtures.py
"""
FIXTURE_NAME = "SMOKE UI (borrar)"

Env = env["primate.cloud.environment"]
Account = env["primate.cloud.account"]

# Cuenta de prueba (credenciales falsas): la seed "Primate Producción".
fake = Account.search([("aws_account_id", "=", "123456789012")], limit=1) \
    or Account.search([], limit=1)
# Un proyecto de esa cuenta (o cualquiera).
project = env["primate.cloud.project"].search(
    [("account_id", "=", fake.id)], limit=1
) or env["primate.cloud.project"].search([], limit=1)

fx = Env.search([("name", "=", FIXTURE_NAME)], limit=1)
if fx:
    fx.write({"state": "draft"})
else:
    fx = Env.create({
        "name": FIXTURE_NAME,
        "project_id": project.id,
        "account_id": fake.id,
        "env_type": "development",
        "state": "draft",
        "odoo_version": "19",
        "odoo_edition": "community",
    })
# Backup COMPLETADO en el fixture: habilita la sección Respaldos del hub y el
# botón "Restaurar…" (que abre el wizard con salvaguardas en el drawer).
Backup = env["primate.cloud.backup"]
bk = Backup.search([("environment_id", "=", fx.id)], limit=1)
if not bk:
    bk = Backup.create({
        "name": "Backup smoke (borrar)",
        "environment_id": fx.id,
        "backup_type": "pcm_dump",
        "purpose": "manual",
        "s3_bucket": "pcm-smoke-bucket",
        "s3_key": "pcm-backups/smoke/fixture.dump",
    })
    bk.write({"state": "completed", "size_mb": 12.3})

# Entorno de PRODUCCIÓN con un registro DNS: ejercita el CRUD de DNS del hub y,
# al ser producción, la fricción ALTA del borrado (alerta roja + acknowledge).
DNS_ENV_NAME = "SMOKE DNS (borrar)"
dns_env = Env.search([("name", "=", DNS_ENV_NAME)], limit=1)
if not dns_env:
    dns_env = Env.create({
        "name": DNS_ENV_NAME, "project_id": project.id, "account_id": fake.id,
        "env_type": "production", "state": "active",
        "odoo_version": "19", "odoo_edition": "community",
    })
Dns = env["primate.cloud.dns.record"]
dns_rec = Dns.search([("environment_id", "=", dns_env.id)], limit=1)
if not dns_rec:
    Dns.create({
        "name": "prod.smoke.local", "account_id": fake.id,
        "environment_id": dns_env.id, "hosted_zone_id": "Z-SMOKE",
        "record_type": "A", "record_value": "1.2.3.4", "ttl": 300,
        "state": "active", "sync_state": "synced",
    })

# region.setup verde pre-sembrado (auto-discovery, Bloque B4): así el gate de
# región del wizard PASA con la cuenta falsa y el provisioning falla downstream
# (AuthFailure), que es lo que ejercita el ciclo error→corrección→éxito del smoke.
from odoo import fields
region = fake.default_region or "us-east-1"
Setup = env["primate.cloud.region.setup"]
setup_vals = {
    "account_id": fake.id, "region": region, "status": "ok",
    "vpc_id": "vpc-smoke", "subnet_id": "subnet-smoke",
    "security_group_id": "sg-smoke", "profile_ok": True, "has_igw": True,
    "detail": "Todo listo (fixture smoke).",
    # SIEMPRE fresco: el gate re-verifica un caché vencido (TTL 1h, por
    # diseño) y con la cuenta falsa fallaría contra AWS real. El seed viejo
    # create-only dejaba el timestamp de la corrida anterior → s04 rompía
    # en cualquier corrida >1h después de la primera.
    "last_discovered_at": fields.Datetime.now(),
}
existing_setup = Setup.search(
    [("account_id", "=", fake.id), ("region", "=", region)], limit=1)
if existing_setup:
    existing_setup.write(setup_vals)
else:
    Setup.create(setup_vals)

env.cr.commit()
print("FIXTURE_ENV_ID", fx.id, "cuenta", fx.account_id.name, "estado", fx.state)
print("FIXTURE_DNS_ENV_ID", dns_env.id)
