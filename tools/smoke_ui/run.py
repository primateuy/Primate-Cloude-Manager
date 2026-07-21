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


def set_theme(pg, label):
    """Cambia el Estilo (tema a/b/c) desde el menú del avatar.

    En el shell el picker de Estilo dejó el header (que ahora coincide con el
    handoff) y vive en el dropdown del avatar. Abre el menú, elige el tema y
    cierra por el scrim (el menú no se autocierra al elegir tema, para permitir
    comparar A/B/C en vivo).
    """
    pg.click(".o_pcm_avatar")
    pg.wait_for_selector(".o_pcm_user_menu", timeout=4000)
    pg.locator(".o_pcm_user_menu .o_pcm_menu_item").filter(has_text=label).first.click()
    pg.click(".o_pcm_menu_scrim")
    pg.wait_for_timeout(150)


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


def ensure_region_cache_fresh(pg):
    """Asegura el caché de región 'ok' y FRESCO (dentro del TTL) para el fixture.

    s04 aprovisiona con creds AWS FALSAS: el job async falla a propósito, pero el
    submit primero pasa por la verificación de región. Esa verificación reusa el
    caché de ``primate.cloud.region.setup`` solo si es 'ok' Y reciente (TTL); si
    venció (p. ej. tras una sesión larga), re-verifica EN VIVO y falla con las
    creds falsas (InvalidClientTokenId), bloqueando el cierre del drawer. Como el
    test no puede verificar en vivo, su precondición es un caché fresco: se
    restaura acá (es una DB de test)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pg.evaluate("""async (now) => {
        const call = (m, me, a) => fetch('/web/dataset/call_kw', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({jsonrpc:'2.0',method:'call',
                params:{model:m,method:me,args:a,kwargs:{}}})}).then(r=>r.json());
        const ids = (await call('primate.cloud.region.setup','search',[[]])).result;
        if (ids.length) {
            await call('primate.cloud.region.setup','write',
                [ids, {status:'ok', last_discovered_at: now}]);
        }
    }""", now)


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
    """El drawer abre y cancela. "Nueva instancia" ahora abre el form OWL de
    Crear instancia EC2 (migrado); se cierra con Cancelar del kit, no el nativo."""
    open_app(pg)
    open_env(pg, INFRA_ENV)
    pg.click("button:has-text('Nueva instancia')")
    pg.wait_for_selector(".o_pcm_drawer .o_pcm_form", timeout=10000)
    assert pg.locator(".o_pcm_drawer .o_pcm_form_foot .o_pcm_btn").count() >= 1, \
        "el form OWL no montó su footer"
    pg.screenshot(path=f"{SHOT}/s03_drawer.png")
    pg.click(".o_pcm_drawer .o_pcm_form_foot .o_pcm_btn:has-text('Cancelar')")
    pg.wait_for_selector(".o_pcm_drawer", state="detached", timeout=8000)


def submit_env_form(pg, dr):
    """Click 'Aprovisionar' en el form OWL + acepta la confirmación facturable.

    Si la validación de forma pasa, aparece el ConfirmationDialog ('Sí'); se
    acepta. (Si falla la validación, NO hay diálogo: el error va inline.)
    """
    pg.locator(f"{dr} .o_pcm_btn_accent").first.click()
    pg.wait_for_selector(".o-overlay-container .modal .modal-footer .btn-primary", timeout=6000)
    pg.locator(".o-overlay-container .modal .modal-footer .btn-primary").first.click()
    pg.wait_for_timeout(900)


@scenario
def s04_drawer_error_correccion_exito(pg):
    """B3: ciclo error->corrección->éxito en el FORM OWL de Crear entorno.

    Confirmar con campos faltantes muestra errores INLINE accionables (no el
    "Missing required fields" nativo) y conserva lo ingresado; corregir y
    confirmar (con la confirmación de recursos facturables) encola el
    aprovisionamiento y cierra el drawer. Segundo uso del drawer sin colgarse."""
    open_app(pg)
    open_env(pg, FIXTURE)
    pg.click("button:has-text('Aprovisionar')")
    pg.wait_for_selector(".o_pcm_form", timeout=10000)
    dr = ".o_pcm_drawer"
    pg.wait_for_timeout(400)
    dom = f"{dr} input[placeholder='forum.primate.cloud']"
    # Garantizar la precondición del ciclo: dominio VACÍO, independiente de que el
    # entorno arrastre un main_url de un intento de aprovisionamiento previo (el
    # form lo precarga vía _onchange_environment_id). El test controla su propio
    # estado en vez de asumir un fixture prístino → idempotente entre corridas.
    pg.fill(dom, "")
    # Confirmar con dominio vacío (y create_dns on por defecto => hosted_zone_id
    # requerido) => errores INLINE, sin diálogo de confirmación, drawer abierto.
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el drawer se cerró ante el error (no debía)"
    assert pg.locator(f"{dr} .o_pcm_field_err").count() >= 1, \
        "no aparecieron los errores inline accionables"
    pg.screenshot(path=f"{SHOT}/s04_error.png")
    # Corregir: dominio + hosted_zone_id (sección DNS, ya visible por create_dns).
    pg.fill(dom, "smoke.test.local")
    pg.fill(f"{dr} input[placeholder^='Z0']", "Z00000000EXAMPLE")
    # El dato ingresado se conserva (input controlado, no se pierde al re-render).
    assert pg.locator(dom).input_value() == "smoke.test.local", \
        "se perdió el dominio ingresado"
    # Confirmar de nuevo -> validación OK -> confirmación facturable -> éxito.
    submit_env_form(pg, dr)
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
    # Segundo uso del drawer funciona normal (sin estado colgado). "Nueva
    # instancia" ahora abre el form OWL de Crear instancia EC2 (migrado).
    open_env(pg, INFRA_ENV)
    for attempt in range(3):
        try:
            pg.click("button:has-text('Nueva instancia')")
            pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
            pg.wait_for_timeout(400)
            pg.click(f"{dr} .o_pcm_form_foot .o_pcm_btn:has-text('Cancelar')", timeout=8000)
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
    """Fase 8.5 + B2: sección DNS del hub con el FORM OWL nuevo. Crear abre el
    formulario propio (no el wizard nativo) y valida con errores inline
    accionables (no "Missing required fields"); borrar un registro de producción
    exige fricción alta (banner de peligro + conformidad + tipear el nombre)."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    open_env(pg, DNS_ENV)
    # La sección DNS lista el registro sembrado.
    pg.wait_for_selector(".o_pcm_section_head:has-text('Registros DNS')",
                         timeout=10000)
    assert pg.locator(".o_pcm_line:has-text('prod.smoke.local')").count() >= 1, \
        "falta el registro DNS sembrado en el hub"

    # 1) Crear: abre el FORM OWL (no el wizard nativo). Sus campos son inputs
    # propios (o_pcm_input / o_pcm_select / o_pcm_textarea), no [name=...].
    pg.click("button:has-text('Agregar registro')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    for sel in ("input.o_pcm_input", ".o_pcm_select", "textarea.o_pcm_textarea"):
        assert pg.locator(f"{dr} {sel}").count() >= 1, \
            f"el form OWL de DNS no montó su control {sel}"
    # Validación accionable: confirmar con el valor vacío NO cierra el drawer y
    # muestra el error inline (no el genérico nativo en inglés).
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, \
        "el form se cerró pese al error de validación (debía quedar abierto)"
    pg.screenshot(path=f"{SHOT}/s07_dns_create.png")
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=8000)

    # 2) Borrar el registro de producción: FORM OWL con fricción alta.
    pg.click(".o_pcm_line:has-text('prod.smoke.local') button[title='Eliminar']")
    pg.wait_for_selector(f"{dr} .o_pcm_form_banner_danger", timeout=10000)
    # Producción => conformidad requerida (ack) + tipear el nombre.
    assert pg.locator(f"{dr} .o_pcm_form_check").count() >= 1, \
        "borrar en producción debe pedir la conformidad (ack)"
    delete_btn = pg.locator(f"{dr} .o_pcm_btn_danger")
    assert delete_btn.is_disabled(), \
        "el botón de borrar debe arrancar deshabilitado (falta tipear el nombre)"
    # Nombre incorrecto => sigue deshabilitado.
    pg.fill(f"{dr} input.o_pcm_input", "no-es-el-nombre")
    pg.wait_for_timeout(150)
    assert delete_btn.is_disabled(), \
        "con el nombre equivocado el botón NO debe habilitarse"
    # Nombre correcto + conformidad => habilita (no se confirma: se preserva).
    pg.fill(f"{dr} input.o_pcm_input", "prod.smoke.local")
    pg.check(f"{dr} .o_pcm_form_check input[type='checkbox']")
    pg.wait_for_timeout(150)
    assert not delete_btn.is_disabled(), \
        "con el nombre exacto y la conformidad el botón debe habilitarse"
    pg.screenshot(path=f"{SHOT}/s07_dns_delete_prod.png")
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=8000)


@scenario
def s05_salir_y_volver(pg):
    """Salir a Ajustes y volver a Cloud Manager 2 veces: remonta limpio."""
    open_app(pg)
    # Usar el drawer una vez.
    open_env(pg, INFRA_ENV)
    pg.click("button:has-text('Nueva instancia')")
    pg.wait_for_selector(".o_pcm_drawer .o_pcm_form", timeout=10000)
    pg.click(".o_pcm_drawer .o_pcm_form_foot .o_pcm_btn:has-text('Cancelar')")
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
    padding de la pantalla (space-screen) difiere entre A (Consola, denso 16px) y
    B (Panel, aireado 24px): caza el bug silencioso de §6.4 (un literal sin
    tokenizar no cambiaría). Se usa el padding y no el radio porque el diseño de
    marca afinó Panel a 8px, igualando el radio de Consola— y (2) que las
    pantallas core renderizan en el tema más distinto (C), sin errores de consola."""
    open_app(pg)

    def screen_pad():
        return pg.evaluate("""() => {
            const s = document.querySelector('.o_pcm_screen');
            return s ? getComputedStyle(s).paddingLeft : '';
        }""")

    set_theme(pg, "Consola")
    pg.wait_for_timeout(400)
    pad_a = screen_pad()
    set_theme(pg, "Panel")
    pg.wait_for_timeout(400)
    pad_b = screen_pad()
    assert pad_a and pad_b and pad_a != pad_b, \
        f"el tema no cambió el padding de la pantalla (¿literal sin tokenizar?): " \
        f"A={pad_a} B={pad_b}"

    # Recorrer las pantallas core en Tema C (el más distinto) sin romper.
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    for nav in ("Entornos", "Proyectos", "Costos"):
        pg.click(f".o_pcm_nav_item:has-text('{nav}')")
        pg.wait_for_selector(
            ".o_pcm_screen .o_pcm_card, .o_pcm_env_card, .o_pcm_card_click",
            timeout=8000)
    pg.screenshot(path=f"{SHOT}/s11_tema_c.png")

    # (3) El panel del drawer debe ser OPACO en los 3 temas — guarda contra un
    # tokenizado futuro que lo vuelva transparente sin que nadie lo note (los
    # formularios quedarían ilegibles). Se miden LOS DOS vectores de translucidez:
    # el alpha del background Y la propiedad `opacity` (un `opacity:.5` no baja el
    # alpha del color; hay que mirar ambos). Se espera a que termine el slide-in.
    def drawer_opacity():
        return pg.evaluate(r"""() => {
            const d = document.querySelector('.o_pcm_drawer');
            if (!d) return null;
            const cs = getComputedStyle(d);
            const bg = cs.backgroundColor;
            let bgAlpha = 1;
            if (bg === 'transparent') {
                bgAlpha = 0;
            } else {
                const m = bg.match(/rgba?\(([^)]+)\)/);
                if (m) {
                    const parts = m[1].split(',').map((s) => s.trim());
                    bgAlpha = parts.length === 4 ? parseFloat(parts[3]) : 1;
                }
            }
            return { bgAlpha, opacity: parseFloat(cs.opacity) };
        }""")
    for theme in ("Consola", "Panel", "Editorial"):
        set_theme(pg, theme)
        pg.wait_for_timeout(250)
        pg.click(".o_pcm_nav_item:has-text('Crear instancia EC2')")
        pg.wait_for_selector(".o_pcm_drawer", timeout=8000)
        pg.wait_for_timeout(500)  # dejar terminar el slide-in (0.16s) + margen
        op = drawer_opacity()
        assert op and op["bgAlpha"] >= 0.99 and op["opacity"] >= 0.99, \
            f"el panel del drawer NO es opaco en Tema {theme} ({op}) — los " \
            f"formularios quedan ilegibles"
        pg.click(".o_pcm_drawer_head .o_pcm_icon_btn")
        pg.wait_for_selector(".o_pcm_drawer", state="detached", timeout=6000)

    # Dejar al usuario en Tema A (default) para no contaminar corridas siguientes.
    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s12_repo_form(pg):
    """B4: el form OWL de repositorio (crear/editar) sobre el tema EDITORIAL
    (no-default, para pescar literales sin tokenizar). Verifica validación inline
    accionable y —clave— que el token de GitHub NUNCA se precarga en edición
    (input vacío, sin máscara ni secreto)."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    # Tema no-default para estresar el tokenizado.
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    open_env(pg, INFRA_ENV)

    # Crear: form OWL con campos propios + token como password.
    pg.click("button:has-text('Agregar repositorio')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    assert pg.locator(f"{dr} input[type='password']").count() == 1, \
        "el token de GitHub debe ser un campo password"
    assert pg.locator(f"{dr} .o_pcm_select").count() >= 1, "falta el select de tipo"
    # Validación accionable: confirmar sin nombre deja el drawer abierto con error.
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    pg.screenshot(path=f"{SHOT}/s12_repo_crear.png")
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    # Editar un repo EXISTENTE: el token no debe precargarse (secreto no filtrado).
    pg.locator(".o_pcm_repo_name").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    pg.click("button:has-text('Editar')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(300)
    tok = pg.locator(f"{dr} input[type='password']").input_value()
    assert tok == "", f"el token NO debe precargarse en edición (llegó: {tok!r})"
    pg.screenshot(path=f"{SHOT}/s12_repo_editar.png")
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    # Reset a Consola para no contaminar corridas siguientes.
    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s13_db_form(pg):
    """B4: el form OWL de base de datos (editar) sobre el tema EDITORIAL
    (no-default). Verifica el condicional por MODALIDAD: RDS muestra el bloque
    informativo (sincronizado de AWS) y oculta la instancia; PostgreSQL local
    muestra el select de instancia y oculta RDS. Más validación inline."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    open_env(pg, INFRA_ENV)

    # Abrir una base (RDS suelta) → su detalle → Editar.
    pg.locator('.o_pcm_line:has-text("forum-db")').first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    pg.click("button:has-text('Editar')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(400)

    # RDS (default): bloque informativo visible, instancia oculta.
    assert pg.get_by_text("sincronizado de AWS").count() >= 1, \
        "en RDS debe mostrarse el bloque informativo de AWS"
    assert pg.get_by_text("Instancia (servidor)").count() == 0, \
        "en RDS no debe mostrarse el select de instancia"
    # Cambiar a PostgreSQL local → aparece la instancia, se oculta RDS.
    pg.select_option(f"{dr} select.o_pcm_select >> nth=0", "local_pg")
    pg.wait_for_timeout(300)
    assert pg.get_by_text("Instancia (servidor)").count() >= 1, \
        "en local debe mostrarse el select de instancia"
    assert pg.get_by_text("sincronizado de AWS").count() == 0, \
        "en local no debe mostrarse el bloque RDS"
    pg.screenshot(path=f"{SHOT}/s13_db_local.png")

    # Validación accionable: borrar el nombre y confirmar deja el drawer abierto.
    pg.fill(f"{dr} input.o_pcm_input >> nth=0", "")
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s14_deploy_form(pg):
    """B4: el form OWL de "nuevo despliegue" sobre el tema EDITORIAL (no-default).
    Verifica (1) que el DESTINO (instancia) es explícito en el form —un deploy al
    servidor equivocado es un problema—, (2) el condicional por tipo (git→repo,
    checkout de rama/commit→su campo, module_update→módulos) y (3) la validación
    inline accionable."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    open_env(pg, INFRA_ENV)

    pg.click("button:has-text('Nuevo despliegue')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(400)
    # Los chequeos se SCOPEAN al drawer (el hub detrás tiene su propia sección
    # "Repositorios" que un get_by_text global matchearía por error).
    def lbl(text):
        return pg.locator(f"{dr} .o_pcm_field_label").filter(has_text=text).count()
    # (1) El destino es explícito en el form.
    assert lbl("Instancia destino") >= 1, \
        "el form debe dejar explícita la instancia destino"

    # (2) Condicional por tipo. El 2º select es el tipo (0=instancia, 1=tipo).
    def set_type(v):
        pg.select_option(f"{dr} select.o_pcm_select >> nth=1", v)
        pg.wait_for_timeout(250)
    set_type("checkout_branch")
    assert lbl("Rama destino") >= 1, "checkout_branch debe pedir rama"
    set_type("checkout_commit")
    assert lbl("Commit destino") >= 1, "checkout_commit debe pedir commit"
    set_type("module_update")
    assert lbl("Módulos") >= 1, "module_update debe pedir módulos"
    assert lbl("Repositorio") == 0, \
        "module_update no debe mostrar el select de repositorio"
    pg.screenshot(path=f"{SHOT}/s14_deploy.png")

    # (3) Validación accionable: pull sin repositorio deja el drawer abierto.
    set_type("pull")
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s15_instance_form(pg):
    """B4: el form OWL de "Agregar Odoo" (otro Odoo en un servidor existente)
    sobre el tema EDITORIAL (no-default). Verifica lo que pidió el diseño R3: el
    preview de puertos con su nota "se asignará al confirmar" y la advertencia
    ADVISORIA de RAM (SMOKE MULTI: 2 Odoo en t3.small la disparan). Más el
    condicional de DNS, el password de admin y la validación inline."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    open_env(pg, MULTI_ENV)

    pg.click("button:has-text('Agregar Odoo (instancia)')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(400)
    # Preview de puertos tentativo (siempre presente) y RAM advisoria (multi).
    assert pg.get_by_text("se asignará al confirmar").count() >= 1, \
        "falta el preview de puertos tentativo"
    assert pg.get_by_text("instancia por GB").count() >= 1, \
        "falta la advertencia advisoria de RAM en el servidor multi-Odoo"
    # La contraseña admin es un campo password.
    assert pg.locator(f"{dr} input[type='password']").count() == 1, \
        "la contraseña admin debe ser un campo password"
    pg.screenshot(path=f"{SHOT}/s15_instance.png")

    # Validación accionable: confirmar vacío deja el drawer abierto con errores.
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    # Condicional de DNS.
    pg.check(f"{dr} .o_pcm_form_check input[type='checkbox']")
    pg.wait_for_timeout(250)
    assert pg.locator(f"{dr} .o_pcm_field_label").filter(
        has_text="Hosted Zone ID").count() >= 1, "al tildar DNS debe pedir la zona"
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s16_account_form(pg):
    """B4: el form OWL de cuenta AWS (la pantalla más sensible) sobre el tema
    EDITORIAL (no-default). Verifica el manejo del SECRETO: en edición el input
    del Secret arranca VACÍO con placeholder "guardado" (nunca precarga el secreto
    ni la máscara). Más el Access Key ID en claro y la validación inline."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    set_theme(pg, "Editorial")
    pg.wait_for_timeout(300)
    pg.click(".o_pcm_nav_item:has-text('Cuentas AWS')")
    pg.wait_for_timeout(1500)
    pg.locator("td:has-text('Primate Producción')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000)
    pg.click("button:has-text('Editar')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(500)

    # SECRETO: input vacío + placeholder "guardado" (nunca se precarga).
    sec = pg.locator(f"{dr} input[type='password']")
    assert sec.count() == 1, "falta el campo Secret (password)"
    assert sec.input_value() == "", \
        f"el Secret NO debe precargarse (llegó: {sec.input_value()!r})"
    ph = sec.get_attribute("placeholder") or ""
    assert "guardado" in ph, f"falta el placeholder 'guardado' del Secret: {ph!r}"

    # Validación accionable: borrar el nombre y confirmar deja el drawer abierto.
    pg.fill(f"{dr} input.o_pcm_input >> nth=0", "")
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    pg.screenshot(path=f"{SHOT}/s16_account.png")
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    open_app(pg)
    set_theme(pg, "Consola")
    pg.wait_for_timeout(300)


@scenario
def s17_dark_mode(pg):
    """Modo oscuro (3ª capa, ortogonal a tema y acento). Verifica (1) que los
    tokens de color REALMENTE cambian al modo oscuro (--pcm-bg pasa a oscuro),
    (2) el bug §6.4: NINGÚN panel queda blanco en dark (superficie sin tokenizar),
    y (3) que el drawer sigue OPACO en dark (el bug que arrastramos). Vuelve a
    claro al final."""
    open_app(pg)
    dr = ".o_pcm_drawer"

    def bg_token():
        return pg.evaluate(
            "() => getComputedStyle(document.querySelector('.o_pcm_app'))"
            ".getPropertyValue('--pcm-bg').trim().toUpperCase()")

    # Toggle a oscuro.
    pg.click(".o_pcm_appearance_toggle button[title='Oscuro']")
    pg.wait_for_timeout(400)
    dark_bg = bg_token()
    assert dark_bg and dark_bg not in ("#F4F5F8", "#FFFFFF"), \
        f"el modo oscuro no cambió el fondo (--pcm-bg={dark_bg})"
    assert pg.locator(".o_pcm_app[data-pcm-appearance='dark']").count() == 1, \
        "no se marcó data-pcm-appearance=dark"

    # (2) §6.4: ningún panel casi-BLANCO (R,G,B > 245) en dark. El accent-tint
    # (236,234,254) queda excluido por el canal R; un panel sin tokenizar (blanco
    # #fff = 255) saltaría.
    whites = pg.evaluate(r"""() => {
        const bad = [];
        for (const el of document.querySelectorAll('.o_pcm_app *')) {
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 15) continue;
            const m = getComputedStyle(el).backgroundColor
                .match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?/);
            if (!m) continue;
            const a = m[4] === undefined ? 1 : parseFloat(m[4]);
            if (a > 0.5 && +m[1] > 245 && +m[2] > 245 && +m[3] > 245) {
                bad.push(el.className.toString().slice(0, 50));
            }
        }
        return [...new Set(bad)];
    }""")
    assert not whites, f"paneles blancos sin tokenizar en dark (§6.4): {whites}"

    # (3) Drawer OPACO en dark (bg-alpha Y opacity, post-slide-in).
    pg.click(".o_pcm_nav_item:has-text('Crear instancia EC2')")
    pg.wait_for_selector(dr, timeout=8000)
    pg.wait_for_timeout(500)
    op = pg.evaluate(r"""() => {
        const d = document.querySelector('.o_pcm_drawer');
        const cs = getComputedStyle(d);
        let a = 1;
        const m = cs.backgroundColor.match(/rgba?\(([^)]+)\)/);
        if (m) { const p = m[1].split(',').map(s => s.trim()); a = p.length === 4 ? parseFloat(p[3]) : 1; }
        return { a, opacity: parseFloat(cs.opacity) };
    }""")
    assert op["a"] >= 0.99 and op["opacity"] >= 0.99, \
        f"el drawer NO es opaco en dark ({op})"
    pg.click(".o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    # Volver a claro (default).
    pg.click(".o_pcm_appearance_toggle button[title='Claro']")
    pg.wait_for_timeout(300)
    assert bg_token() in ("#F4F5F8",), \
        "no volvió a claro tras el toggle"


@scenario
def s18_ec2_form(pg):
    """B4 (residual migrado): "Crear instancia EC2" (lanza una máquina) ahora es
    un form OWL, no el wizard nativo. Se prueba en DARK —la razón de migrarlo: un
    form nativo no conoce los tokens del modo oscuro y se veía roto—. Verifica que
    NO hay form nativo, que están la cuenta/semáforo/red-admin, y la validación."""
    open_app(pg)
    dr = ".o_pcm_drawer"
    pg.click(".o_pcm_appearance_toggle button[title='Oscuro']")
    pg.wait_for_timeout(400)

    pg.click(".o_pcm_nav_item:has-text('Crear instancia EC2')")
    pg.wait_for_selector(f"{dr} .o_pcm_form", timeout=10000)
    pg.wait_for_timeout(400)
    # NO es el form nativo (sin header morado / footer portaleado).
    assert pg.locator(f"{dr} .o_form_view").count() == 0 \
        and pg.locator(f"{dr} .modal-footer").count() == 0, \
        "quedó el form nativo (debía ser el form OWL)"
    # Están los campos clave (reuso de Crear entorno): cuenta + semáforo + AMI.
    assert pg.locator(f"{dr} .o_pcm_field_label").filter(
        has_text="Cuenta AWS").count() >= 1, "falta el select de cuenta"
    assert pg.locator(f"{dr} .o_pcm_semaphore").count() >= 1, "falta el semáforo de región"
    pg.screenshot(path=f"{SHOT}/s18_ec2_dark.png")

    # Validación accionable: confirmar sin nombre/cuenta deja el drawer abierto.
    pg.click(f"{dr} .o_pcm_btn_accent")
    pg.wait_for_selector(f"{dr} .o_pcm_field_err", timeout=5000)
    assert pg.locator(dr).count() == 1, "el form se cerró pese al error (no debía)"
    pg.click(f"{dr} .o_pcm_drawer_head .o_pcm_icon_btn")
    pg.wait_for_selector(dr, state="detached", timeout=6000)

    # Volver a claro.
    pg.click(".o_pcm_appearance_toggle button[title='Claro']")
    pg.wait_for_timeout(300)


# JS que devuelve las superficies con fondo CLARO (R,G,B>200) visibles: en dark,
# CUALQUIERA es el bug (una card/superficie que no se tokenizó al modo oscuro).
_LIGHT_SCAN = r"""() => {
    const out = [];
    const sel = '.o_pcm_card, .o_pcm_env_card, .o_pcm_kpi, .o_pcm_drawer,'
        + ' .o_pcm_form_summary, .o_form_view, .o_list_view';
    for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width < 40 || r.height < 15) continue;
        const m = getComputedStyle(el).backgroundColor
            .match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?/);
        if (!m) continue;
        const a = m[4] === undefined ? 1 : parseFloat(m[4]);
        if (a > 0.5 && +m[1] > 200 && +m[2] > 200 && +m[3] > 200) {
            out.push(el.className.toString().slice(0, 45));
        }
    }
    return [...new Set(out)];
}"""


# §6.4 de TÍTULOS: a diferencia de _LIGHT_SCAN (que mira SUPERFICIES claras en
# dark), este mira el TEXTO de los headings. Odoo fija un color oscuro explícito
# en h1..h6 del backend que ganaba a la herencia → títulos negros sobre fondo
# oscuro. Falla si CUALQUIER h1/h2/h3 visible computa a luminancia baja (texto
# oscuro sobre superficie oscura = ilegible).
_HEADING_SCAN = r"""() => {
    const out = [];
    for (const el of document.querySelectorAll('.o_pcm_app h1, .o_pcm_app h2, .o_pcm_app h3')) {
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 8) continue;
        const txt = (el.innerText || '').trim();
        if (!txt) continue;
        const m = getComputedStyle(el).color.match(/rgba?\((\d+), (\d+), (\d+)/);
        if (!m) continue;
        const lum = 0.2126*+m[1] + 0.7152*+m[2] + 0.0722*+m[3];
        if (lum < 110) {
            out.push(el.tagName + ' "' + txt.slice(0, 24) + '" lum=' + Math.round(lum));
        }
    }
    return [...new Set(out)];
}"""


@scenario
def s19_dark_exhaustivo(pg):
    """El dark en TODAS las pantallas de detalle (no una muestra). Recorre inicio,
    costos, y los detalles de base/repo/cuenta + hub/servidor/instancia en modo
    oscuro; FALLA si CUALQUIER card/superficie computa a un color claro (una
    superficie sin tokenizar al dark — el bug de la card blanca)."""
    open_app(pg)
    pg.click(".o_pcm_appearance_toggle button[title='Oscuro']")
    pg.wait_for_timeout(400)

    def check(name):
        bad = pg.evaluate(_LIGHT_SCAN)
        assert not bad, f"superficie clara en dark en '{name}': {bad}"

    pg.click(".o_pcm_nav_item:has-text('Inicio')"); pg.wait_for_timeout(500); check("inicio")
    pg.click(".o_pcm_nav_item:has-text('Costos')"); pg.wait_for_timeout(600); check("costos")
    # Bases de datos → detalle OWL (el que tenía la card blanca en el reporte).
    pg.click(".o_pcm_nav_item:has-text('Bases de datos')"); pg.wait_for_timeout(1500)
    pg.locator("td:has-text('forum-db')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000); pg.wait_for_timeout(400)
    check("base de datos (detalle)")
    # Repositorios → detalle.
    pg.click(".o_pcm_nav_item:has-text('Repositorios')"); pg.wait_for_timeout(1500)
    pg.locator("td:has-text('WMS')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000); pg.wait_for_timeout(400)
    check("repositorio (detalle)")
    # Cuentas → detalle.
    pg.click(".o_pcm_nav_item:has-text('Cuentas AWS')"); pg.wait_for_timeout(1500)
    pg.locator("td:has-text('Primate Producción')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000); pg.wait_for_timeout(400)
    check("cuenta (detalle)")
    # Entorno hub → servidor → instancia (con tabs).
    open_env(pg, MULTI_ENV); check("entorno hub")
    pg.locator(".o_pcm_instance_head").first.click()
    pg.wait_for_selector(".o_pcm_line_click:has-text('Odoo Beta')", timeout=10000)
    pg.wait_for_timeout(300); check("servidor")
    pg.locator(".o_pcm_line_click:has-text('Odoo Beta')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_tabs", timeout=10000)
    pg.wait_for_timeout(400); check("instancia")

    pg.click(".o_pcm_appearance_toggle button[title='Claro']")
    pg.wait_for_timeout(300)


@scenario
def s20_headings_dark(pg):
    """§6.4 de TÍTULOS: ningún h1/h2/h3 puede quedar oscuro sobre fondo oscuro.
    Recorre las pantallas donde el bug se veía (inicio, costos, detalles con
    secciones y tabs) en dark y FALLA si algún heading computa a luminancia baja
    (el bug: color de heading del backend de Odoo ganándole al token de texto)."""
    open_app(pg)
    pg.click(".o_pcm_appearance_toggle button[title='Oscuro']")
    pg.wait_for_timeout(400)

    def check(name):
        bad = pg.evaluate(_HEADING_SCAN)
        assert not bad, f"título oscuro sobre fondo oscuro en '{name}': {bad}"

    pg.click(".o_pcm_nav_item:has-text('Inicio')"); pg.wait_for_timeout(500); check("inicio")
    pg.click(".o_pcm_nav_item:has-text('Costos')"); pg.wait_for_timeout(600); check("costos")
    pg.click(".o_pcm_nav_item:has-text('Bases de datos')"); pg.wait_for_timeout(1500)
    pg.locator("td:has-text('forum-db')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=10000); pg.wait_for_timeout(400)
    check("base de datos (detalle)")
    # Hub → servidor → instancia (con tabs): los títulos de sección que el
    # reporte marcó ilegibles (AWS, Red y cuenta, Métricas, Instancias hospedadas).
    open_env(pg, MULTI_ENV); check("entorno hub")
    pg.locator(".o_pcm_instance_head").first.click()
    pg.wait_for_selector(".o_pcm_line_click:has-text('Odoo Beta')", timeout=10000)
    pg.wait_for_timeout(300); check("servidor")
    pg.locator(".o_pcm_line_click:has-text('Odoo Beta')").first.click()
    pg.wait_for_selector(".o_pcm_detalle .o_pcm_tabs", timeout=10000)
    pg.wait_for_timeout(400); check("instancia")

    pg.click(".o_pcm_appearance_toggle button[title='Claro']")
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
        # Precondición de s04: caché de región fresco (el test usa creds falsas).
        ensure_region_cache_fresh(pg)
        for fn in (s01_app_y_sidebar, s02_hub_y_drill, s03_drawer_cancelar,
                   s04_drawer_error_correccion_exito,
                   s06_respaldos_y_wizards_fase8, s07_dns_crud, s08_costos,
                   s09_servidor_vs_instancia, s10_proyecto_eje_cliente,
                   s11_temas, s12_repo_form, s13_db_form, s14_deploy_form,
                   s15_instance_form, s16_account_form, s17_dark_mode,
                   s18_ec2_form, s19_dark_exhaustivo, s20_headings_dark,
                   s05_salir_y_volver):
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
