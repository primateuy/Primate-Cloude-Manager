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
    # Reset a draft + LIMPIAR la config de provision guardada: la feature "el
    # wizard recuerda lo ingresado" precarga el hosted_zone_id de corridas
    # anteriores, y entonces el primer submit de s04 ya no falla por campo
    # faltante (encola directo) → rompe la premisa "error → drawer abierto".
    # Sin esto, el fixture se contamina entre corridas.
    fx.write({"state": "draft", "provision_config_encrypted": False})
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

# ---------------------------------------------------------------------------
# Servidor MULTI-Odoo (R4-B6): una máquina que hospeda instancias de DOS
# clientes distintos, con env_type DISTINTO (prod + staging), para ejercitar
# la navegación servidor→lista→instancia y el resumen "1 producción · 1
# staging" (D-B6.1) SIN mentir sobre el server mitad-y-mitad. Partners =
# empresas cliente reales (no fichas personales — lección del test de
# Terminate: fixture que exista en la realidad).
MULTI_NAME = "SMOKE MULTI (borrar)"
Partner = env["res.partner"]
Project = env["primate.cloud.project"]
Ec2 = env["primate.cloud.ec2.instance"]
Instance = env["primate.cloud.instance"]

partner_a = Partner.search([("name", "=", "Cliente Alfa SA (smoke)")], limit=1) \
    or Partner.create({"name": "Cliente Alfa SA (smoke)", "is_company": True})
partner_b = Partner.search([("name", "=", "Cliente Beta SRL (smoke)")], limit=1) \
    or Partner.create({"name": "Cliente Beta SRL (smoke)", "is_company": True})
proj_a = Project.search([("name", "=", "Proyecto Alfa (smoke)")], limit=1) \
    or Project.create({"name": "Proyecto Alfa (smoke)", "account_id": fake.id,
                       "partner_id": partner_a.id})
proj_b = Project.search([("name", "=", "Proyecto Beta (smoke)")], limit=1) \
    or Project.create({"name": "Proyecto Beta (smoke)", "account_id": fake.id,
                       "partner_id": partner_b.id})

multi = Env.search([("name", "=", MULTI_NAME)], limit=1)
if not multi:
    multi = Env.create({
        "name": MULTI_NAME, "project_id": proj_a.id, "account_id": fake.id,
        "env_type": "production", "state": "active",
        "odoo_version": "19", "odoo_edition": "community",
    })
    machine = Ec2.create({
        "name": MULTI_NAME, "account_id": fake.id, "aws_instance_id": "i-smokemulti",
        "instance_state": "running", "region": region, "public_ip": "203.0.113.9",
        "instance_type": "t3.small", "provisioned_by_pcm": True,
        "environment_id": multi.id,
    })
    multi.ec2_instance_id = machine
    # Instancia A = producción de Alfa (la primaria), materializada al slug.
    inst_a = multi.primary_instance_id
    inst_a.write({"name": "Odoo Alfa", "project_id": proj_a.id, "slug": "cliente-a",
                  "state": "active", "env_type": "production",
                  "main_url": "alfa.smoke.local"})
    multi._materialize_multiodoo_layout(inst_a, {"db_mode": "local_pg"})
    inst_a.database_id = env["primate.cloud.database"].create({
        "name": "alfa_db", "account_id": fake.id, "environment_id": multi.id,
        "db_type": "local_pg", "instance_id": inst_a.id,
        "ec2_instance_id": machine.id})
    # Instancia B = staging de Beta (otro cliente), materializada al slug.
    inst_b = multi._create_instance_with_ports({
        "name": "Odoo Beta", "project_id": proj_b.id, "slug": "cliente-b",
        "odoo_version": "19", "odoo_edition": "community", "state": "active",
        "env_type": "staging", "main_url": "beta.smoke.local"})
    multi._materialize_multiodoo_layout(inst_b, {"db_mode": "local_pg"})
    inst_b.database_id = env["primate.cloud.database"].create({
        "name": "beta_db", "account_id": fake.id, "environment_id": multi.id,
        "db_type": "local_pg", "instance_id": inst_b.id,
        "ec2_instance_id": machine.id})

# 2º servidor para el EJE PROYECTO cross-server (R6): otra instancia del cliente
# Alfa en OTRA máquina → el proyecto Alfa tiene instancias en 2 servidores.
MULTI_B_NAME = "SMOKE MULTI B (borrar)"
multi_b = Env.search([("name", "=", MULTI_B_NAME)], limit=1)
if not multi_b:
    multi_b = Env.create({
        "name": MULTI_B_NAME, "project_id": proj_a.id, "account_id": fake.id,
        "env_type": "production", "state": "active",
        "odoo_version": "19", "odoo_edition": "community",
    })
    machine_b = Ec2.create({
        "name": MULTI_B_NAME, "account_id": fake.id,
        "aws_instance_id": "i-smokemultib", "instance_state": "running",
        "region": region, "public_ip": "203.0.113.10",
        "instance_type": "t3.micro", "provisioned_by_pcm": True,
        "environment_id": multi_b.id,
    })
    multi_b.ec2_instance_id = machine_b
    inst_a2 = multi_b.primary_instance_id
    inst_a2.write({"name": "Odoo Alfa 2", "project_id": proj_a.id,
                   "slug": "cliente-a2", "state": "active",
                   "env_type": "production", "main_url": "alfa2.smoke.local"})
    multi_b._materialize_multiodoo_layout(inst_a2, {"db_mode": "local_pg"})

# Costos sembrados: crudo (cost.entry) + reparto (cost.share) del mes en curso,
# para que los bloques de costo (proyecto/servidor/instancia/costos) rendericen
# CON datos y con "datos al" — el smoke ejercita la honestidad de R5/R6.
Entry = env["primate.cloud.cost.entry"]
Share = env["primate.cloud.cost.share"].with_context(pcm_cost_regen=True)
# inst_a/inst_b viven en la rama de creación del multi; se re-resuelven acá por
# slug para que la siembra funcione aunque el multi ya existiera.
inst_a = multi.instance_ids.filtered(lambda i: i.slug == "cliente-a")[:1]
inst_b = multi.instance_ids.filtered(lambda i: i.slug == "cliente-b")[:1]
_today = fields.Date.context_today(fake)
_m0 = _today.replace(day=1)
if inst_a and inst_b and not Entry.search_count(
        [("account_id", "=", fake.id), ("period_start", "=", _m0),
         ("environment_ref", "=", multi.pcm_ref)]):
    Entry.create({
        "account_id": fake.id, "period_start": _m0, "period_end": _m0,
        "granularity": "monthly", "environment_ref": multi.pcm_ref,
        "environment_name": MULTI_NAME, "service": "AmazonEC2", "amount": 20.0})
    # Servidor compartido (Alfa + Beta) → reparto 50/50.
    for _inst, _proj, _partner, _amt in (
            (inst_a, proj_a, partner_a, 10.0), (inst_b, proj_b, partner_b, 10.0)):
        Share.create({
            "account_id": fake.id, "period_start": _m0, "period_end": _m0,
            "granularity": "monthly", "environment_id": multi.id,
            "environment_ref": multi.pcm_ref, "environment_name": MULTI_NAME,
            "instance_id": _inst.id, "instance_ref": _inst.pcm_ref,
            "instance_name": _inst.name, "project_id": _proj.id,
            "partner_id": _partner.id, "client_name": _partner.name,
            "method": "equal", "amount": _amt})
    fake.cost_pulled_at = fields.Datetime.now()

env.cr.commit()
print("FIXTURE_ENV_ID", fx.id, "cuenta", fx.account_id.name, "estado", fx.state)
print("FIXTURE_DNS_ENV_ID", dns_env.id)
print("FIXTURE_MULTI_ID", multi.id, "machine", multi.ec2_instance_id.id)
