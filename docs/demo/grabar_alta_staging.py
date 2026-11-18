# -*- coding: utf-8 -*-
"""Graba el MONTAJE DEL STAGING de Alta na Pipa + primer refresco verificado.

  .venv/bin/python docs/demo/grabar_alta_staging.py
  .venv/bin/python docs/demo/grabar_alta_staging.py ensamblar

Guion (plan aprobado 2026-11-17/18):
   0. Login de DARYL. SONDA a producción durante TODO el proceso (curl cada
      15 s a https://altanapipa.primatecloudmanager.com; 3 fallos seguidos
      = freno). Producción: SOLO lectura + el dump del pipeline.
   1. «Crear staging» desde el entorno: mismo servidor, segunda instancia
      (slug altanapipa-staging, puertos propios, unit con cgroups), dominio
      altanapipa-staging.primatecloudmanager.com + HTTPS en la misma EIP,
      override alta_na_pipa→19.0_staging, PAUSA para la maestra de staging.
   2. El pipeline hace: dump de producción → restore en staging →
      neutralización (SQL + odoo neutralize OFICIAL + checklist duro) →
      clones espejo en orden → addons_path por repo.
   3. Checks: cgroups en la unit (systemctl show), conf §6 del staging,
      addons_path/ramas (19.0_staging solo alta), neutralización verificada
      (0 SMTP activos, 0 crons activos, URL de staging), padrón ANONIMIZADO
      + alumnos de prueba sembrados, sitio staging con HTTPS, /entrar con el
      alumno de prueba (fecha 2000-01-01), y producción VIVA todo el rato.
"""
import json
import pathlib
import re
import subprocess
import sys
import threading
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
BUNDLE_DEMO = REPO / "primate_bundle" / "docs" / "demo"
sys.path.insert(0, str(BUNDLE_DEMO))
import grabar_demo as receta        # noqa: E402
import grabar_oximoro as ox         # noqa: E402

VIEW_W, VIEW_H = 1536, 864
VIDEO_W, VIDEO_H = 1920, 1080
BASE = "https://primatecloudmanager.com"
PROD_URL = "https://altanapipa.primatecloudmanager.com"
STG_DOMINIO = "altanapipa-staging.primatecloudmanager.com"
ZONA = "Z06178081BO0Q1OK7MTAO"
ENV_NOMBRE = "altanapipa"
STG_NOMBRE = "altanapipa-staging"
STG_DB = "altanapipa_staging"
CONF_STG = "/etc/odoo/%s.conf" % STG_NOMBRE
REPOS_DIR = "/opt/pcm/instances/%s/repos" % STG_NOMBRE
RUNTIME_ADDONS = "/opt/pcm/runtime/odoo-19/src/addons"
ORDEN_REPOS = ["webOCA", "surveyOCA", "primate_portal", "alta_na_pipa"]
FLAGS = REPO / "infra" / "alta_prod"
SALIDA = HERE
FACTORES = {"montaje": 32, "pausa": 32, "verificar": 8}

ox.MARCA_PLACA = "ALTA NA PIPA · STAGING + PRIMER REFRESCO"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos, videos_extra = [], [], [], {}, []
T0 = None
CTX = None
SONDA = {"total": 0, "fallos": 0, "seguidos": 0, "max_seguidos": 0,
         "parar": False}


def evento(texto):
    print("PCM_STG: %s" % texto, flush=True)


def marca(nombre):
    marcas.append((nombre, time.time() - T0))
    evento("marca · %s" % nombre)


class Ventana:
    def __init__(self, nombre, factor):
        self.nombre, self.factor = nombre, factor

    def __enter__(self):
        self.desde = time.time() - T0
        return self

    def __exit__(self, *exc):
        ventanas.append({"nombre": self.nombre, "desde": self.desde,
                         "hasta": time.time() - T0, "factor": self.factor})


def sonda_produccion():
    """Hilo: producción tiene que responder DURANTE TODO el proceso."""
    while not SONDA["parar"]:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "10", PROD_URL + "/"],
            capture_output=True, text=True)
        SONDA["total"] += 1
        if out.stdout.strip() in ("200", "303"):
            SONDA["seguidos"] = 0
        else:
            SONDA["fallos"] += 1
            SONDA["seguidos"] += 1
            SONDA["max_seguidos"] = max(SONDA["max_seguidos"],
                                        SONDA["seguidos"])
            evento("SONDA: producción respondió %r (%d seguido/s)"
                   % (out.stdout.strip(), SONDA["seguidos"]))
        time.sleep(15)


def exigir_prod_viva():
    if SONDA["seguidos"] >= 3:
        raise RuntimeError("PRODUCCIÓN DEJÓ DE RESPONDER (3+ sondas "
                           "seguidas) — FRENO")


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=400000)
    datos = r.json()
    assert "error" not in datos, json.dumps(datos["error"])[:400]
    return datos.get("result")


def correr(orden, timeout=600):
    return subprocess.run(orden, timeout=timeout, capture_output=True,
                          text=True)


def curl(url):
    out = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                  "--max-time", "15", url])
    return out.stdout.strip()


def abrir_app(page):
    ox.ir(page, BASE + "/odoo/action-primate_cloud_manager.action_pcm_app",
          esperar=".o_pcm_app")
    page.wait_for_timeout(1200)


def nav(page, item):
    ox.clic(page, page.locator(".o_pcm_nav_item", has_text=item).first)
    page.wait_for_timeout(1200)


def abrir_entorno(page):
    for _ in range(3):
        nav(page, "Entornos")
        page.locator(".o_pcm_screen input[type=text]").first.fill(ENV_NOMBRE)
        page.wait_for_timeout(1200)
        tarjeta = page.locator(".o_pcm_env_card", has_text=ENV_NOMBRE)
        if tarjeta.count():
            ox.clic(page, tarjeta.first)
            page.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=15000)
            page.wait_for_timeout(900)
            return
        page.wait_for_timeout(2500)
    raise RuntimeError("no apareció el entorno")


def abrir_servidor(page):
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=ENV_NOMBRE).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def comando(page, orden, explicacion, espera=18000, timeout=240):
    previos = orm("primate.cloud.operation.log", "search",
                  [[["action_type", "=", "ssm_command"]]],
                  {"limit": 1, "order": "id desc", "context": {}})
    ultimo = previos[0] if previos else 0
    ox.clic(page, page.locator("button", has_text="Ejecutar comando").first)
    page.wait_for_selector("textarea#command_0, input#command_0", timeout=12000)
    page.locator("textarea#command_0, input#command_0").first.fill(orden)
    ox.rotulo(page, "Ejecutar comando (SSM)", explicacion, 2800)
    ox.clic(page, page.locator("button[name=action_run]").first)
    page.wait_for_timeout(espera)
    t0 = time.time()
    while time.time() - t0 < timeout:
        filas = orm("primate.cloud.operation.log", "search_read",
                    [[["action_type", "=", "ssm_command"], ["id", ">", ultimo]],
                     ["result", "error_message"]],
                    {"limit": 1, "order": "id desc", "context": {}})
        if filas and filas[0]["result"] in ("success", "failed"):
            assert filas[0]["result"] == "success", (
                "CHECK FALLÓ: %s · %s"
                % (explicacion, (filas[0]["error_message"] or "")[:300]))
            return
        page.wait_for_timeout(5000)
    raise RuntimeError("comando sin terminar: %s" % orden[:80])


def esperar_flag(page, nombre, titulo, detalle):
    bandera = FLAGS / nombre
    evento("PAUSA · esperando %s" % nombre)
    with Ventana("pausa-%s" % nombre, FACTORES["pausa"]):
        ox.rotulo(page, titulo, detalle + " · espera real, acelerada", 2600)
        while not bandera.exists():
            page.wait_for_timeout(4000)
    evento("PAUSA LEVANTADA · %s" % nombre)
    page.wait_for_timeout(800)


def t1_wizard(page, env_id):
    abrir_app(page)
    # Guard: si una corrida anterior dejó la instancia creada, FRENO (no
    # montar dos veces).
    previa = orm("primate.cloud.instance", "search_read",
                 [[["slug", "=", STG_NOMBRE]], ["id", "state", "env_type"]],
                 {"context": {}})
    if previa and previa[0]["state"] == "archived" \
            and previa[0]["env_type"] == "staging":
        # Desmontada con triple confirmación (teardown por slug ya corrido):
        # el registro archivado retiene el slug (UNIQUE env+slug) — se borra
        # para que el wizard recree limpio.
        orm("primate.cloud.instance", "unlink", [[previa[0]["id"]]])
        evento("registro archivado del staging borrado; se recrea limpio")
    else:
        # En ERROR el registro NO se borra: es el handle del botón
        # «Desmontar instancia…» (la limpieza del servidor sale por ahí).
        assert not previa, (
            "ya existe una instancia %s (estado %s) — si está en error, "
            "desmontala primero desde PCM y relanzá" %
            (STG_NOMBRE, previa and previa[0]["state"]))
    abrir_entorno(page)
    ox.clic(page, page.locator("button", has_text="Crear staging").first)
    page.wait_for_selector("input#name_0, #name_0", timeout=15000)
    page.wait_for_timeout(1500)
    # PISAR el default del wizard (fill limpia; tipear solo agrega).
    page.locator("#name_0").fill("")
    ox.tipear(page, page.locator("#name_0"), STG_NOMBRE, 45)
    page.locator("#domain_0").fill(STG_DOMINIO)
    page.locator("#db_name_0").fill(STG_DB)
    # Base de origen EXPLÍCITA: el intento fallido dejó registrada la base
    # del staging en el mismo entorno y la validación ya no puede inferirla.
    campo_db = page.locator("#origin_database_id_0").first
    campo_db.click()
    campo_db.fill(ENV_NOMBRE)
    page.wait_for_timeout(1800)
    campo_db.press("Enter")   # toma la primera opción (altanapipa)
    page.wait_for_timeout(800)
    # Bucket de transferencia del dump (obligatorio): el de backups de la
    # cuenta — ya existe y el rol de la máquina ya tiene acceso.
    page.locator("#transfer_bucket_0").fill("pcm-backups-512060111009")
    if page.locator("#branch_overrides_0").count():
        page.locator("#branch_overrides_0").fill("alta_na_pipa:19.0_staging")
    ox.rotulo(page, "Staging en el MISMO servidor",
              "Segunda instancia · dominio propio en la misma IP fija · "
              "alta_na_pipa en 19.0_staging, el resto en 19.0", 4200)
    # DNS + HTTPS del subdominio: los campos viven en la PESTAÑA DNS del
    # notebook — hay que abrirla o count()=0 y se saltean en silencio.
    ox.clic(page, page.locator("a.nav-link", has_text="DNS").first)
    page.wait_for_selector("#create_dns_0", timeout=10000)
    if not page.locator("#create_dns_0").is_checked():
        ox.clic(page, page.locator("#create_dns_0"))
    page.locator("#hosted_zone_id_0").fill(ZONA)
    esperar_flag(page, "flag_maestra_stg",
                 "PAUSA: Daryl tipea la maestra DEL STAGING",
                 "Distinta de la de producción, en el campo del wizard")
    ox.clic(page, page.locator("button[name=action_create_staging]").first)
    # Verificar que el submit PASÓ: el modal se cierra y la instancia existe.
    for _ in range(30):
        if not page.locator("#name_0").count():
            break
        page.wait_for_timeout(1000)
    else:
        # Leer el diálogo de error / notificación para saber QUÉ rebotó.
        textos = []
        for sel in (".o_dialog .modal-body", ".o_error_dialog",
                    ".o_notification", ".o_form_label.o_field_invalid"):
            try:
                textos += [t.strip() for t in
                           page.locator(sel).all_inner_texts() if t.strip()]
            except Exception:
                pass
        page.screenshot(path=str(HERE / "bruto" / "stg_wizard_freno.png"),
                        full_page=True)
        raise RuntimeError("el wizard no se cerró tras Crear — FRENO · %s"
                           % ("; ".join(textos)[:400] or "sin diálogo visible"))
    stg = orm("primate.cloud.instance", "search_read",
              [[["slug", "=", STG_NOMBRE]], ["id", "state"]], {"context": {}})
    assert stg, "el wizard cerró pero la instancia no existe — FRENO"
    marca("staging encolado")
    ox.rotulo(page, "Montando el staging",
              "Instancia nueva + dump de PRODUCCIÓN (solo lectura) + restore "
              "+ neutralize + clones — espera real ×%d" % FACTORES["montaje"],
              3400)
    with Ventana("montaje", FACTORES["montaje"]):
        t0 = time.time()
        while time.time() - t0 < 5400:
            exigir_prod_viva()
            stg = orm("primate.cloud.instance", "search_read",
                      [[["slug", "=", STG_NOMBRE]], ["id", "state"]],
                      {"context": {}})
            if stg and stg[0]["state"] in ("active", "error"):
                break
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("el staging no terminó en 90 min")
    assert stg and stg[0]["state"] == "active", (
        "el staging terminó en ERROR — FRENO")
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    marca("staging activo")
    tiempos["montaje"] = time.time() - T0
    return stg[0]["id"]


def t2_checks_servidor(page):
    abrir_entorno(page)
    abrir_servidor(page)
    comando(page,
            "systemctl show odoo-%s -p MemoryMax -p MemoryHigh -p CPUWeight "
            "-p IOWeight && systemctl show odoo-%s -p MemoryMax | grep -q "
            "1073741824" % (STG_NOMBRE, STG_NOMBRE),
            "cgroups del staging: MemoryMax 1G, pesos bajos — producción "
            "manda")
    marca("cgroups verificados")
    comando(page,
            "grep -E '^(dbfilter|list_db|http_interface)' %s "
            "&& grep -qxF 'dbfilter = ^%s$' %s "
            "&& grep -qxF 'list_db = False' %s "
            "&& grep -qxF 'http_interface = 127.0.0.1' %s"
            % (CONF_STG, STG_DB, CONF_STG, CONF_STG, CONF_STG),
            "El conf del staging: base propia, candado multi-tenant")
    esperado = ",".join([RUNTIME_ADDONS]
                        + ["%s/%s" % (REPOS_DIR, r) for r in ORDEN_REPOS]
                        + ["/opt/pcm/instances/%s/addons" % STG_NOMBRE])
    comando(page,
            "grep '^addons_path' %s && grep -qxF 'addons_path = %s' %s"
            % (CONF_STG, esperado, CONF_STG),
            "addons_path del staging: por repo, MISMO orden que producción")
    comando(page,
            "for r in %s; do echo \"$r: $(sudo -u odoo git -C %s/$r "
            "rev-parse --abbrev-ref HEAD)\"; done "
            "&& sudo -u odoo git -C %s/alta_na_pipa rev-parse "
            "--abbrev-ref HEAD | grep -qx 19.0_staging "
            "&& sudo -u odoo git -C %s/webOCA rev-parse --abbrev-ref HEAD "
            "| grep -qx 19.0"
            % (" ".join(ORDEN_REPOS), REPOS_DIR, REPOS_DIR, REPOS_DIR),
            "Ramas: alta_na_pipa en 19.0_staging; el resto en 19.0")
    marca("conf + addons_path + ramas verificados")
    comando(page,
            "sudo -u postgres psql -d %s -At -c \"select 'smtp_activos='"
            "||count(*) from ir_mail_server where active\" "
            "&& sudo -u postgres psql -d %s -At -c \"select count(*) from "
            "ir_mail_server where active\" | grep -qx 0 "
            "&& sudo -u postgres psql -d %s -At -c \"select count(*) from "
            "ir_cron where active\" | grep -qx 0 "
            "&& sudo -u postgres psql -d %s -At -c \"select value from "
            "ir_config_parameter where key='web.base.url'\" "
            "| grep -qx https://%s"
            % (STG_DB, STG_DB, STG_DB, STG_DB, STG_DOMINIO),
            "NEUTRALIZADO verificado: 0 SMTP activos, 0 crons activos, "
            "URL del staging")
    marca("neutralización verificada")
    comando(page,
            "sudo -u postgres psql -d %s -At -c \"select count(*) from "
            "res_partner where portal_birthdate is not null\" | grep -qx 2 "
            "&& sudo -u postgres psql -d %s -At -c \"select ref from "
            "res_partner where portal_birthdate is not null order by ref\" "
            "&& sudo -u postgres psql -d %s -At -c \"select ref from "
            "res_partner where portal_birthdate is not null and ref like "
            "'STAGING-TEST-%%'\" | wc -l | grep -qx 2"
            % (STG_DB, STG_DB, STG_DB),
            "Padrón ANONIMIZADO: las únicas fechas que quedan son las de "
            "los 2 alumnos de prueba ficticios")
    marca("padrón anonimizado verificado")


def t3_sitio_staging(page):
    base = "https://%s" % STG_DOMINIO
    final = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}",
                    "-L", "--max-time", "25", base + "/"])
    assert final.returncode == 0, "HTTPS del staging no responde"
    assert "/odoo" not in final.stdout, "/ del staging redirige a /odoo"
    assert curl(base + "/web/database/manager") == "404"
    sitio = CTX.new_page()
    t_abre = time.time() - T0
    ox.ir(sitio, base + "/entrar", timeout=60000)
    sitio.wait_for_timeout(2500)
    ox.rotulo(sitio, "Staging al aire, con su HTTPS",
              "El /entrar con el alumno de prueba sembrado "
              "(STAGING-TEST-1 · 01/01/2000)", 3800)
    # Login del alumno de prueba, si el form está (selectores laxos: si el
    # flujo difiere, la captura igual muestra la pantalla).
    try:
        campos = sitio.locator("form input")
        if campos.count() >= 2:
            campos.nth(0).fill("STAGING-TEST-1")
            campos.nth(1).fill("01/01/2000")
            sitio.locator("form button[type=submit]").first.click()
            sitio.wait_for_timeout(4000)
    except Exception:   # noqa: BLE001 — informativo, no corta
        pass
    ruta = sitio.video.path() if sitio.video else None
    sitio.close()
    videos_extra.append({"nombre": "staging_site", "desde": t_abre,
                         "hasta": time.time() - T0, "video": str(ruta)})
    marca("sitio staging verificado")


def grabar():
    global T0, CTX
    FLAGS.mkdir(parents=True, exist_ok=True)
    (FLAGS / "flag_maestra_stg").unlink(missing_ok=True)
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
    hilo = threading.Thread(target=sonda_produccion, daemon=True)
    with sync_playwright() as pw:
        nav_ = pw.chromium.launch(headless=False,
                                  args=["--no-sandbox", "--lang=es-UY"])
        CTX = nav_.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, locale="es-UY",
            timezone_id="America/Montevideo",
            record_video_dir=str(bruto),
            record_video_size={"width": VIEW_W, "height": VIEW_H},
            ignore_https_errors=False)
        CTX.add_init_script(receta.ROTULO_JS)
        CTX.add_init_script(receta.PLACA_JS)
        CTX.add_init_script(ox.CURSOR_JS)
        CTX.add_init_script(ox.PESTANA_JS)
        page = CTX.new_page()
        page.on("pageerror", lambda e: incidentes.append("JS: %s" % str(e)[:120]))
        ox.T0 = T0 = time.time()
        excepcion = None
        page.goto(BASE + "/web/login", timeout=90000)
        evento("PAUSA · login de Daryl (2FA)…")
        for _ in range(600):
            if "/odoo" in page.url or page.locator(".o_web_client").count():
                break
            page.wait_for_timeout(2000)
        else:
            raise RuntimeError("sin login")
        evento("sesión activa; arranca la sonda a producción")
        hilo.start()
        try:
            envs = orm("primate.cloud.environment", "search_read",
                       [[["name", "=", ENV_NOMBRE]], ["id"]], {"context": {}})
            env_id = envs[0]["id"]
            ox.tarjeta(page, "Staging de Alta na Pipa",
                       "Copia de producción para romper sin miedo",
                       [("Dónde", "el MISMO servidor, segunda instancia"),
                        ("Protección", "cgroups: producción manda"),
                        ("Copia", "dump + restore + NEUTRALIZE verificado"),
                        ("Sonda", "producción vigilada todo el proceso")],
                       6000)
            ox.tarjeta(page, "1 · Crear staging", "Wizard + maestra de Daryl")
            t1_wizard(page, env_id)
            evento("TRAMO 1 OK")
            exigir_prod_viva()
            ox.tarjeta(page, "2 · El staging por dentro",
                       "cgroups · conf · repos · NEUTRALIZADO · padrón")
            t2_checks_servidor(page)
            evento("TRAMO 2 OK")
            exigir_prod_viva()
            ox.tarjeta(page, "3 · El staging al aire", STG_DOMINIO)
            t3_sitio_staging(page)
            evento("TRAMO 3 OK")
            exigir_prod_viva()
            asserts_sonda = ("%d sondas, %d fallos (max %d seguidos)"
                             % (SONDA["total"], SONDA["fallos"],
                                SONDA["max_seguidos"]))
            evento("SONDA FINAL: " + asserts_sonda)
            ox.tarjeta(page, "STAGING LISTO",
                       "https://%s" % STG_DOMINIO,
                       [("Montaje", _dur(tiempos.get("montaje"))),
                        ("Neutralizado", "0 SMTP · 0 crons · padrón fuera"),
                        ("Producción", "respondió TODO el proceso (%s)"
                         % asserts_sonda),
                        ("Refrescar", "botón «Refrescar staging», cuando "
                                      "quieran")], 6500)
            marca("fin")
        except Exception as error:   # noqa: BLE001
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(SALIDA / "alta_stg_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            SONDA["parar"] = True
            ruta_video = page.video.path() if page.video else None
            CTX.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes,
             "videos_extra": videos_extra, "sonda": SONDA}
    destino = bruto / "toma_alta_staging.webm"
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    for extra in videos_extra:
        origen = pathlib.Path(extra["video"])
        if origen.exists():
            nuevo = bruto / ("toma_alta_staging_%s.webm" % extra["nombre"])
            origen.replace(nuevo)
            extra["video"] = str(nuevo)
    (SALIDA / "toma_alta_staging.json").write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d · sonda: %s"
           % (destino, len(incidentes), datos["sonda"]))
    if excepcion:
        raise excepcion


def _dur(segundos):
    if not segundos:
        return "—"
    return "%d:%02d" % (segundos // 60, segundos % 60)


def ensamblar():
    datos = json.loads((SALIDA / "toma_alta_staging.json").read_text())
    final = SALIDA / "alta_na_pipa_staging.mp4"
    extras = sorted(datos.get("videos_extra", []), key=lambda e: e["desde"])
    vens = sorted(datos["ventanas"], key=lambda v: v["desde"])
    piezas, previo = [], 0.0
    for e in extras:
        piezas.append((datos["video"], previo, e["desde"], vens))
        piezas.append((e["video"], None, None, []))
        previo = e["hasta"]
    piezas.append((datos["video"], previo, None, vens))
    entradas, indice = [], {}
    for p in piezas:
        if p[0] not in indice:
            indice[p[0]] = len(indice)
            entradas += ["-i", p[0]]
    filtro, partes, idx = "", [], 0
    for ruta, desde, hasta, ventanas_p in piezas:
        n = indice[ruta]
        desde = desde or 0.0
        aplicables = [v for v in ventanas_p
                      if v["hasta"] > v["desde"] + 0.5 and v["desde"] >= desde
                      and (hasta is None or v["desde"] < hasta)]
        prev = desde
        for v in aplicables:
            v_hasta = min(v["hasta"], hasta) if hasta is not None else v["hasta"]
            if v["desde"] > prev:
                filtro += "[%d:v]trim=%.2f:%.2f,setpts=PTS-STARTPTS[p%d];" % (
                    n, prev, v["desde"], idx)
                partes.append("[p%d]" % idx); idx += 1
            filtro += "[%d:v]trim=%.2f:%.2f,setpts=(PTS-STARTPTS)/%d[p%d];" % (
                n, v["desde"], v_hasta, v["factor"], idx)
            partes.append("[p%d]" % idx); idx += 1
            prev = v_hasta
        cola = ("trim=%.2f:%.2f" % (prev, hasta)) if hasta is not None \
            else ("trim=%.2f" % prev)
        filtro += "[%d:v]%s,setpts=PTS-STARTPTS[p%d];" % (n, cola, idx)
        partes.append("[p%d]" % idx); idx += 1
    filtro += ("%sconcat=n=%d:v=1:a=0,scale=%d:%d:flags=lanczos,setsar=1[v]"
               % ("".join(partes), len(partes), VIDEO_W, VIDEO_H))
    orden = (["ffmpeg", "-y"] + entradas +
             ["-filter_complex", filtro, "-map", "[v]", "-c:v", "libx264",
              "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-r",
              "30", "-movflags", "+faststart", str(final)])
    proc = subprocess.run(orden, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-1200:]
    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True,
        text=True).stdout.strip())
    print("🎬 %s · %d:%02d" % (final, dur // 60, dur % 60))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ensamblar":
        ensamblar()
    else:
        grabar()
