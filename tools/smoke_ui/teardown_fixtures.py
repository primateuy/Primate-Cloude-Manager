"""Limpia los fixtures del smoke-test de UI (correr con `odoo shell`).

Borra el entorno de prueba y los jobs encolados que dejó el flujo (a).

Uso:
    .venv/bin/python <odoo-bin> shell -c <conf> -d <db> --no-http \
        < tools/smoke_ui/teardown_fixtures.py
"""
FIXTURE_NAME = "SMOKE UI (borrar)"

fx = env["primate.cloud.environment"].search([("name", "=", FIXTURE_NAME)])
removed = len(fx)
if fx:
    # Limpiar jobs fallidos de ese entorno (best-effort).
    try:
        jobs = env["queue.job"].search([("model_name", "=", "primate.cloud.environment")])
        jobs.filtered(lambda j: set(j.records.ids) & set(fx.ids)).unlink()
    except Exception:  # noqa: BLE001 - limpieza opcional
        pass
    fx.unlink()
env.cr.commit()
print("TEARDOWN eliminados", removed)
