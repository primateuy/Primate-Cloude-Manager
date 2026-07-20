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
DNS_ENV = "SMOKE DNS (borrar)"          # entorno producción con registro DNS
MULTI_ENV = "SMOKE MULTI (borrar)"      # servidor multi-Odoo (prod + staging, R4-B6)

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
    # El éxito encola un job que falla RÁPIDO (creds falsas => AuthFailure en
    # ~1s): el overlay de progreso queda en su estado final con botón Cerrar
    # (UX deliberada). Cerrarlo como un usuario real; si el jobrunner no
    # procesó el job (overlay ausente), seguir sin más.
    try:
        pg.wait_for_selector(
            ".o_pcm_overlay .o_pcm_footer button:has-text('Cerrar')",
            timeout=25000,
        )
        pg.click(".o_pcm_overlay .o_pcm_footer button:has-text('Cerrar')")
        pg.wait_for_selector(".o_pcm_overlay", state="detached", timeout=8000)
    except PWTimeout:
        pass
    # Los eventos de bus del job async del fixture (que falla a propósito con
    # AuthFailure) re-renderizan la app y pueden desestabilizar el click
    # siguiente: margen de asentamiento + reintento (fricción de harness, no
    # bug de la app: el drawer aislado abre/cancela limpio N veces).
    pg.wait_for_timeout(2000)
    # Segundo uso del drawer funciona normal (sin estado colgado).
    open_env(pg, INFRA_ENV)
    for attempt in range(3):
        try:
            pg.click("button:has-text('Nueva instancia')")
            pg.wait_for_selector(dr, timeout=10000)
            pg.wait_for_timeout(400)
            pg.click(f"{dr} .modal-footer button:has-text('Cancelar')", timeout=8000)
            pg.wait_for_selector(dr, state="detached", timeout=8000)
            break
        except Exception:
            if attempt == 2:
                raise
            # Un evento del job re-montó el drawer a mitad del click: cerrar
            # lo que haya quedado y reintentar.
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(1500)


@scenario
def s06_respaldos_y_wizards_fase8(pg):
    """Fase 8: sección Respaldos del hub (chip de propósito), wizard de
    restore en el drawer (cancelar) y staging desde el detalle del servidor."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    # 1) Hub del fixture: sección Respaldos con el backup sembrado y su chip.
    open_env(pg, FIXTURE)
    pg.wait_for_selector(".o_pcm_section_head:has-text('Respaldos')", timeout=10000)
    assert pg.locator(".o_pcm_line:has-text('Manual')").count() >= 1, \
        "falta el chip de propósito del backup sembrado"
    # 2) Restaurar…: abre el wizard REAL (con salvaguardas) en el drawer.
    pg.click("button:has-text('Restaurar…')")
    pg.wait_for_selector(dr, timeout=10000)
    pg.wait_for_selector(f"{dr} [name='target_environment_id']", timeout=8000)
    pg.screenshot(path=f"{SHOT}/s06_restore_drawer.png")
    pg.click(f"{dr} .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(dr, state="detached", timeout=8000)
    # 3) Crear staging: R4-B6 lo ofrece el HUB del entorno (ya no la pantalla de
    # servidor). El wizard trae el servidor destino (D-R4.6) en el drawer.
    open_env(pg, INFRA_ENV)
    pg.wait_for_selector("button:has-text('Crear staging')", timeout=10000)
    pg.click("button:has-text('Crear staging')")
    pg.wait_for_selector(dr, timeout=10000)
    pg.wait_for_selector(f"{dr} [name='origin_instance_id']", timeout=8000)
    pg.screenshot(path=f"{SHOT}/s06_staging.png")
    pg.click(f"{dr} .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(dr, state="detached", timeout=8000)


@scenario
def s08_costos(pg):
    """Fase 9: pantalla de Costos renderiza sus KPIs."""
    open_app(pg)
    pg.click(".o_pcm_nav_item:has-text('Costos')")
    pg.wait_for_selector(".o_pcm_costos", timeout=10000)
    assert pg.locator(".o_pcm_costos h1:has-text('Costos')").count() == 1, \
        "la pantalla de costos no renderizó"
    assert pg.locator(".o_pcm_costos .o_pcm_kpi_value").count() >= 3, \
        "faltan los KPIs de costos"
    pg.screenshot(path=f"{SHOT}/s08_costos.png")


@scenario
def s09_servidor_vs_instancia(pg):
    """R4-B6: la reescritura de UI más grande del proyecto. Verifica el corte
    servidor/instancia: el SERVIDOR es máquina-only (sin config/logs/addons/
    login-as), lista sus instancias con resumen env_type honesto, y el panel
    Odoo (con sus tabs y rutas del SLUG) vive en la pantalla de INSTANCIA."""
    open_app(pg)
    open_env(pg, MULTI_ENV)
    # Drill al SERVIDOR (la máquina) desde el hub del entorno.
    pg.locator(".o_pcm_instance_head").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    pg.screenshot(path=f"{SHOT}/s09_servidor.png")
    body = pg.locator(".o_pcm_detalle").inner_text()

    # (1) El SERVIDOR NO expone acciones por-instancia (assert de AUSENCIA en
    # la pantalla REAL, no solo en el JS): sin tabs de panel, sin Login-as,
    # sin "Traer logs", sin "Agregar addon", sin editor de Config.
    assert pg.locator(".o_pcm_detalle .o_pcm_tabs").count() == 0, \
        "el servidor NO debe tener las tabs del panel de instancia"
    for prohibido in ("Iniciar sesión como", "Traer logs", "Agregar addon",
                      "Guardar y reiniciar"):
        assert prohibido not in body, \
            f"el servidor NO debe ofrecer '{prohibido}' (es de la instancia)"

    # (2) Resumen env_type honesto: el server mitad-y-mitad NO dice "producción"
    # a secas — lista "producción" Y "staging".
    assert "Hospeda:" in body, "falta el resumen de instancias hospedadas"
    low = body.lower()
    assert "producción" in low and "staging" in low, \
        f"el resumen env_type no refleja el server mitad-y-mitad: {body[:200]}"

    # (3) Lista de instancias hospedadas con su cliente → click a la de BETA
    # (staging, cliente-b) para verificar las rutas del slug.
    assert pg.locator(".o_pcm_line_click:has-text('Odoo Beta')").count() >= 1, \
        "el servidor no lista sus instancias hospedadas con nombre"
    pg.locator(".o_pcm_line_click:has-text('Odoo Beta')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_tabs", timeout=10000)
    pg.screenshot(path=f"{SHOT}/s09_instancia.png")
    inst_body = pg.locator(".o_pcm_detalle").inner_text()

    # (4) Las RUTAS son las del SLUG (backend), no las legacy hardcodeadas de
    # PCM_PATHS: /etc/odoo/cliente-b.conf y la unit odoo-cliente-b.
    assert "/etc/odoo/cliente-b.conf" in inst_body, \
        f"la instancia no muestra el conf del slug (¿PCM_PATHS legacy?): revisar"
    assert "odoo-cliente-b" in inst_body, \
        "la instancia no muestra la unit del slug"
    assert "/opt/odoo/odoo.conf" not in inst_body and \
        "/etc/odoo/odoo.conf" not in inst_body, \
        "aparecen rutas legacy hardcodeadas en la pantalla de instancia"

    # (5) Los tabs de la INSTANCIA abren (Logs / Config).
    pg.click(".o_pcm_tab:has-text('Logs')")
    pg.wait_for_selector("button:has-text('Traer logs')", timeout=8000)
    assert pg.locator("button:has-text('Traer logs')").count() == 1, \
        "la tab Logs de la instancia no montó su visor"
    pg.click(".o_pcm_tab:has-text('Config')")
    pg.wait_for_selector(".o_pcm_tab_active:has-text('Config')", timeout=8000)
    assert pg.locator(".o_pcm_detalle .o_pcm_card").count() >= 1, \
        "la tab Config de la instancia no renderizó contenido"
    pg.screenshot(path=f"{SHOT}/s09_instancia_config.png")


@scenario
def s07_dns_crud(pg):
    """Fase 8.5: sección DNS del hub — crear registro (wizard en drawer) y la
    fricción ALTA al intentar borrar un registro de producción (alerta roja)."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    open_env(pg, DNS_ENV)
    # La sección DNS lista el registro sembrado.
    pg.wait_for_selector(".o_pcm_section_head:has-text('Registros DNS')",
                         timeout=10000)
    assert pg.locator(".o_pcm_line:has-text('prod.smoke.local')").count() >= 1, \
        "falta el registro DNS sembrado en el hub"
    # 1) Crear registro: abre el wizard REAL en el drawer.
    pg.click("button:has-text('Agregar registro')")
    pg.wait_for_selector(dr, timeout=10000)
    pg.wait_for_selector(f"{dr} [name='name']", timeout=8000)
    pg.screenshot(path=f"{SHOT}/s07_dns_create.png")
    pg.click(f"{dr} .modal-footer button:has-text('Cancelar')")
    pg.wait_for_selector(dr, state="detached", timeout=8000)
    # 2) Borrar el registro de producción: wizard con fricción alta (alerta roja).
    pg.click(".o_pcm_line:has-text('prod.smoke.local') button[title='Eliminar']")
    pg.wait_for_selector(dr, timeout=10000)
    pg.wait_for_selector(f"{dr} [name='confirm_name']", timeout=8000)
    alert = pg.locator(f"{dr} .alert-danger:has-text('PRODUCCIÓN')")
    assert alert.count() >= 1, "no apareció la fricción alta de borrado en prod"
    pg.screenshot(path=f"{SHOT}/s07_dns_delete_prod.png")
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


@scenario
def s10_proyecto_eje_cliente(pg):
    """R6: el EJE CLIENTE — el que Daryl pidió cuando encontró el error
    conceptual. Entrar por Proyectos → un cliente → sus instancias (en
    servidores DISTINTOS) → click → la instancia. Y el reparto visible con su
    contexto (método + datos al)."""
    open_app(pg)
    pg.click(".o_pcm_nav_item:has-text('Proyectos')")
    pg.wait_for_selector(".o_pcm_card_click", timeout=10000)
    pg.screenshot(path=f"{SHOT}/s10_proyectos.png")
    # Abrir el cliente Alfa (tiene instancias en 2 servidores).
    pg.locator(".o_pcm_card_click:has-text('Proyecto Alfa (smoke)')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    body = pg.locator(".o_pcm_detalle").inner_text()
    # (1) Sus instancias EN SERVIDORES DISTINTOS (el cruce por el eje cliente).
    lineas = pg.locator(".o_pcm_line_click")
    assert lineas.count() >= 2, \
        f"el proyecto no lista sus instancias en varios servidores: {lineas.count()}"
    assert MULTI_ENV in body and "SMOKE MULTI B" in body, \
        "las instancias del cliente no muestran servidores distintos"
    # (2) El reparto con su contexto (método + datos al) — honestidad R6.
    assert "Costo del mes" in body, "falta el bloque de costo del proyecto"
    assert "datos al" in body, "el costo del proyecto no muestra 'datos al'"
    pg.screenshot(path=f"{SHOT}/s10_proyecto.png")
    # (3) Click a una instancia → su pantalla (el cruce cierra en la instancia).
    lineas.first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    assert len(crumbs(pg)) >= 2, f"drill proyecto→instancia falló: {crumbs(pg)}"
    pg.screenshot(path=f"{SHOT}/s10_instancia.png")


@scenario
def s11_temas(pg):
    """B1 del rediseño: los 3 temas andan sobre las MISMAS pantallas. Cambia
    A->B->C con el selector y verifica (1) que los tokens REALMENTE cambian —el
    radio de una card difiere entre A y B (caza el bug silencioso de §6.4: un
    literal sin tokenizar no cambiaría)— y (2) que las pantallas core renderizan
    en el tema más distinto (C), sin errores de consola."""
    open_app(pg)

    def card_radius():
        return pg.evaluate("""() => {
            const c = document.querySelector('.o_pcm_card, .o_pcm_kpi');
            return c ? getComputedStyle(c).borderTopLeftRadius : '';
        }""")

    pg.click(".o_pcm_theme_opt:has-text('Consola')")
    pg.wait_for_timeout(400)
    radius_a = card_radius()
    pg.click(".o_pcm_theme_opt:has-text('Panel')")
    pg.wait_for_timeout(400)
    radius_b = card_radius()
    assert radius_a and radius_b and radius_a != radius_b, \
        f"el tema no cambió el radio de las cards (¿literal sin tokenizar?): " \
        f"A={radius_a} B={radius_b}"

    # Recorrer las pantallas core en Tema C (el más distinto) sin romper.
    pg.click(".o_pcm_theme_opt:has-text('Editorial')")
    pg.wait_for_timeout(300)
    for nav in ("Entornos", "Proyectos", "Costos"):
        pg.click(f".o_pcm_nav_item:has-text('{nav}')")
        pg.wait_for_selector(
            ".o_pcm_screen .o_pcm_card, .o_pcm_env_card, .o_pcm_card_click",
            timeout=8000)
    pg.screenshot(path=f"{SHOT}/s11_tema_c.png")

    # (3) El panel del drawer debe ser OPACO en los 3 temas — guarda contra un
    # tokenizado futuro que lo vuelva transparente sin que nadie lo note (los
    # formularios quedarían ilegibles). Abre el drawer por tema y mide el alpha.
    def drawer_alpha():
        return pg.evaluate(r"""() => {
            const d = document.querySelector('.o_pcm_drawer');
            if (!d) return null;
            const bg = getComputedStyle(d).backgroundColor;
            if (bg === 'transparent') return 0;
            const m = bg.match(/rgba?\(([^)]+)\)/);
            if (!m) return 1;
            const parts = m[1].split(',').map(s => s.trim());
            return parts.length === 4 ? parseFloat(parts[3]) : 1;
        }""")
    for theme in ("Consola", "Panel", "Editorial"):
        pg.click(f".o_pcm_theme_opt:has-text('{theme}')")
        pg.wait_for_timeout(250)
        pg.click(".o_pcm_nav_item:has-text('Crear instancia EC2')")
        pg.wait_for_selector(".o_pcm_drawer", timeout=8000)
        pg.wait_for_timeout(300)
        alpha = drawer_alpha()
        assert alpha is not None and alpha >= 0.99, \
            f"el panel del drawer NO es opaco en Tema {theme} " \
            f"(alpha={alpha}) — los formularios quedan ilegibles"
        pg.click(".o_pcm_drawer_head .o_pcm_icon_btn")
        pg.wait_for_selector(".o_pcm_drawer", state="detached", timeout=6000)

    # Dejar al usuario en Tema A (default) para no contaminar corridas siguientes.
    open_app(pg)
    pg.click(".o_pcm_theme_opt:has-text('Consola')")
    pg.wait_for_timeout(300)


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
                   s04_drawer_error_correccion_exito,
                   s06_respaldos_y_wizards_fase8, s07_dns_crud, s08_costos,
                   s09_servidor_vs_instancia, s10_proyecto_eje_cliente,
                   s11_temas, s05_salir_y_volver):
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
