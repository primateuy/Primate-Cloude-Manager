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
env.cr.commit()
print("FIXTURE_ENV_ID", fx.id, "cuenta", fx.account_id.name, "estado", fx.state)
