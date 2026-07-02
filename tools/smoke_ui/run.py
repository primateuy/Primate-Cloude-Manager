#!/usr/bin/env python
"""Smoke-test de UI de la app PCM (headless, Playwright).

Ejercita montaje, sidebar, hub, drill-through, breadcrumb y el drawer de wizard,
incluyendo el ciclo error->corrección->éxito y el remontaje al salir/volver.
Captura errores de consola/JS y screenshots de evidencia.

Ver README.md. Correr con la base demo levantada en :19088.
    .venv/bin/python tools/smoke_ui/run.py
"""
import sys
import os

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = os.environ.get("PCM_BASE", "http://localhost:19088")
LOGIN = os.environ.get("PCM_LOGIN", "admin")
PWD = os.environ.get("PCM_PWD", "admin")
APP = f"{BASE}/odoo/action-primate_cloud_manager.action_pcm_app"
SETTINGS = f"{BASE}/odoo/action-base_setup.action_general_configuration"
SHOT = os.path.join(os.path.dirname(__file__), "screenshots")
FIXTURE = "SMOKE UI (borrar)"          # entorno draft con cuenta de prueba
INFRA_ENV = "Forum Producción"          # entorno con infra para drill-through

console_errors = []
results = []


def scenario(fn):
    """Registra un escenario nombrado y captura su resultado."""
    def wrap(pg):
        name = fn.__name__
        n0 = len(console_errors)
        try:
            fn(pg)
            errs = console_errors[n0:]
            if errs:
                results.append((name, "FAIL", f"{len(errs)} errores de consola"))
            else:
                results.append((name, "PASS", ""))
        except Exception as e:  # noqa: BLE001
            results.append((name, "FAIL", f"{type(e).__name__}: {e}"))
    return wrap


def open_app(pg):
    pg.goto(APP, wait_until="load")
    pg.wait_for_selector(".o_pcm_app", timeout=15000)


def open_env(pg, name):
    pg.click(".o_pcm_nav_item:has-text('Entornos')")
    pg.wait_for_selector(".o_pcm_env_card", timeout=10000)
    pg.locator(f".o_pcm_env_card:has-text('{name}')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)


def set_select_menu(pg, scope, field, search):
    """Setea un campo Selection (widget o_select_menu de Odoo 19) por texto."""
    pg.click(f"{scope} [name='{field}'] .o_select_menu")
    pg.wait_for_selector(".o-dropdown--menu, .o_select_menu_menu", timeout=5000)
    pg.locator(".o-dropdown--menu .o_select_menu_item, .o-dropdown--menu .dropdown-item") \
        .filter(has_text=search).first.click()
    pg.wait_for_timeout(200)


def h1(pg):
    e = pg.locator(".o_pcm_detalle h1").first
    return e.inner_text().strip() if e.count() else ""


def crumbs(pg):
    return [c.strip() for c in pg.locator(".o_pcm_crumb").all_inner_texts()]


# ---------------------------------------------------------------- Escenarios
@scenario
def s01_app_y_sidebar(pg):
    open_app(pg)
    pg.screenshot(path=f"{SHOT}/s01_inicio.png")
    # Colapsar/expandir un grupo del sidebar.
    pg.locator(".o_pcm_nav_group_head:has-text('Inventario')").click()
    pg.wait_for_timeout(300)
    assert pg.locator(".o_pcm_nav_item:has-text('Bases de datos')").count() == 0, \
        "el grupo Inventario no se colapsó"
    pg.locator(".o_pcm_nav_group_head:has-text('Inventario')").click()
    pg.wait_for_timeout(300)
    assert pg.locator(".o_pcm_nav_item:has-text('Bases de datos')").count() == 1


@scenario
def s02_hub_y_drill(pg):
    open_app(pg)
    open_env(pg, INFRA_ENV)
    assert pg.locator(".o_pcm_instance").count() >= 1, "hub sin instancias"
    pg.screenshot(path=f"{SHOT}/s02_hub.png")
    # servidor
    pg.locator(".o_pcm_instance_head").first.click()
    pg.wait_for_timeout(500)
    assert len(crumbs(pg)) == 3 and h1(pg), f"drill servidor falló: {crumbs(pg)}"
    pg.screenshot(path=f"{SHOT}/s02_servidor.png")
    pg.locator(".o_pcm_crumb", has_text=INFRA_ENV).first.click()
    pg.wait_for_selector(".o_pcm_instance")
    # repo
    pg.locator(".o_pcm_repo_name").first.click()
    pg.wait_for_timeout(500)
    assert len(crumbs(pg)) == 3, f"drill repo falló: {crumbs(pg)}"
    assert pg.locator(".o_pcm_mod_table").count() >= 1, "detalle repo sin tabla de módulos"
    pg.locator(".o_pcm_crumb", has_text=INFRA_ENV).first.click()
    pg.wait_for_selector(".o_pcm_instance")
    # base de datos
    pg.locator(".o_pcm_line_click:has(i.fa-database)").first.click()
    pg.wait_for_timeout(500)
    assert len(crumbs(pg)) == 3, f"drill base falló: {crumbs(pg)}"
    pg.locator(".o_pcm_crumb", has_text=INFRA_ENV).first.click()
    pg.wait_for_selector(".o_pcm_instance")
    # cuenta (chip fa-key)
    pg.locator(".o_pcm_hero_meta .o_pcm_link", has=pg.locator("i.fa-key")).first.click()
    pg.wait_for_timeout(500)
    assert len(crumbs(pg)) == 3, f"drill cuenta falló: {crumbs(pg)}"
    pg.screenshot(path=f"{SHOT}/s02_cuenta.png")


@scenario
def s03_drawer_cancelar(pg):
    open_app(pg)
    open_env(pg, INFRA_ENV)
    pg.click("button:has-text('Nueva instancia')")
    pg.wait_for_selector(".o_pcm_drawer", timeout=10000)
    assert pg.locator(".o_pcm_drawer .modal-footer button").count() >= 1, \
        "el drawer no portaleó los botones del wizard"
    pg.screenshot(path=f"{SHOT}/s03_drawer.png")
    pg.click(".o_pcm_drawer .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(".o_pcm_drawer", state="detached", timeout=8000)


def submit_provision(pg, dr):
    """Click 'Aprovisionar' + acepta la confirmación de acción facturable."""
    pg.locator(f"{dr} .modal-footer button.btn-primary").first.click()
    # El botón lleva confirm= (recursos facturables): aparece un ConfirmationDialog.
    pg.wait_for_selector(".o-overlay-container .modal .modal-footer .btn-primary", timeout=6000)
    pg.locator(".o-overlay-container .modal .modal-footer .btn-primary").first.click()
    pg.wait_for_timeout(900)


@scenario
def s04_drawer_error_correccion_exito(pg):
    """Ciclo error->corrección->éxito en el drawer de Aprovisionar (env fixture)."""
    open_app(pg)
    open_env(pg, FIXTURE)
    pg.click("button:has-text('Aprovisionar')")
    pg.wait_for_selector(".o_pcm_drawer", timeout=10000)
    dr = ".o_pcm_drawer"
    # region viene pre-cargada por onchange (default_region de la cuenta); solo
    # falta el dominio para que el form sea válido. Tab para forzar el commit
    # del campo Char (Odoo actualiza en blur, no en input).
    pg.fill(f"{dr} [name='domain'] input", "smoke.test.local")
    pg.locator(f"{dr} [name='domain'] input").press("Tab")
    # Confirmar: create_dns activo por defecto hace hosted_zone_id requerido
    # (required="create_dns" en la vista) => error de validación al confirmar.
    submit_provision(pg, dr)
    # El error se surfacea como notificación "Missing required fields".
    pg.wait_for_selector(".o_notification", timeout=8000)
    # El drawer sigue abierto y el dato ingresado se conserva.
    assert pg.locator(dr).count() == 1, "el drawer se cerró ante el error (no debía)"
    val = pg.locator(f"{dr} [name='domain'] input").input_value()
    assert val == "smoke.test.local", f"se perdió el dato ingresado: '{val}'"
    pg.screenshot(path=f"{SHOT}/s04_error.png")
    # Corregir: ir a la pestaña DNS y completar el hosted_zone_id que faltaba.
    pg.locator(f"{dr} .o_notebook .nav-link:has-text('DNS')").first.click()
    pg.wait_for_timeout(300)
    pg.fill(f"{dr} [name='hosted_zone_id'] input", "Z00000000EXAMPLE")
    pg.locator(f"{dr} [name='hosted_zone_id'] input").press("Tab")
    # Confirmar de nuevo -> éxito (encola job) -> el drawer se cierra.
    submit_provision(pg, dr)
    pg.wait_for_selector(dr, state="detached", timeout=12000)
    # El éxito encola un job (falla async con creds falsas): esperar a que el
    # overlay bloqueante de aprovisionamiento se limpie antes de seguir.
    try:
        pg.wait_for_selector(".o_pcm_overlay", state="detached", timeout=20000)
    except PWTimeout:
        pass
    # Segundo uso del drawer funciona normal (sin estado colgado).
    open_env(pg, INFRA_ENV)
    pg.click("button:has-text('Nueva instancia')")
    pg.wait_for_selector(dr, timeout=10000)
    pg.click(f"{dr} .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(dr, state="detached", timeout=8000)


@scenario
def s05_salir_y_volver(pg):
    """Salir a Ajustes y volver a Cloud Manager 2 veces: remonta limpio."""
    open_app(pg)
    # Usar el drawer una vez.
    open_env(pg, INFRA_ENV)
    pg.click("button:has-text('Nueva instancia')")
    pg.wait_for_selector(".o_pcm_drawer", timeout=10000)
    pg.click(".o_pcm_drawer .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(".o_pcm_drawer", state="detached", timeout=8000)
    for i in range(2):
        pg.goto(SETTINGS, wait_until="load")
        pg.wait_for_timeout(1200)
        open_app(pg)
        # remonta limpio: sin drawers fantasma, con acento y navegación.
        assert pg.locator(".o_pcm_drawer").count() == 0, f"drawer fantasma tras volver (iter {i})"
        assert pg.locator(".o_pcm_swatch").count() >= 5, "faltan swatches de acento tras remontar"
        assert pg.locator(".o_pcm_nav_item").count() >= 1, "sidebar vacío tras remontar"
    pg.screenshot(path=f"{SHOT}/s05_remontaje.png")


def main():
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page(viewport={"width": 1400, "height": 950})
        pg.on("pageerror", lambda e: console_errors.append(f"PAGEERROR: {e}"))
        pg.on("console",
              lambda m: console_errors.append(f"CONSOLE.error: {m.text}")
              if m.type == "error" else None)
        # Login.
        pg.goto(f"{BASE}/web/login", wait_until="load")
        pg.fill("input[name='login']", LOGIN)
        pg.fill("input[name='password']", PWD)
        pg.click("button[type='submit']")
        pg.wait_for_timeout(1500)
        for fn in (s01_app_y_sidebar, s02_hub_y_drill, s03_drawer_cancelar,
                   s04_drawer_error_correccion_exito, s05_salir_y_volver):
            fn(pg)
        br.close()

    print("\n=== RESULTADOS SMOKE-TEST UI ===")
    for name, status, detail in results:
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    if console_errors:
        print("\n--- errores de consola capturados ---")
        for e in console_errors:
            print(e)
    print(f"\n{'TODOS OK' if not n_fail else str(n_fail) + ' ESCENARIOS FALLIDOS'}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
