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
    # El registro de backups es inmutable por diseño (unlink bloqueado en el
    # modelo) y ondelete=restrict bloquearía el borrado del entorno: para el
    # fixture del smoke se limpia por SQL directo (excepción justificada).
    env.cr.execute(
        "DELETE FROM primate_cloud_backup WHERE environment_id IN %s",
        [tuple(fx.ids)],
    )
    # Limpiar jobs fallidos de ese entorno (best-effort).
    try:
        jobs = env["queue.job"].search([("model_name", "=", "primate.cloud.environment")])
        jobs.filtered(lambda j: set(j.records.ids) & set(fx.ids)).unlink()
    except Exception:  # noqa: BLE001 - limpieza opcional
        pass
    # R1 (D6): las instancias restringen el borrado del entorno → van primero.
    fx.instance_ids.unlink()
    fx.unlink()

# Entorno DNS de prueba: borrar sus registros DNS, instancias y el entorno.
dns_env = env["primate.cloud.environment"].search(
    [("name", "=", "SMOKE DNS (borrar)")])
if dns_env:
    dns_env.dns_record_ids.unlink()
    dns_env.instance_ids.unlink()
    dns_env.unlink()
env.cr.commit()
print("TEARDOWN eliminados", removed)
