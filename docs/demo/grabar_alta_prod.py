# -*- coding: utf-8 -*-
"""Graba el MONTAJE DE PRODUCCIÓN de Alta na Pipa (tramo D, PCM Producción).

  .venv/bin/python docs/demo/grabar_alta_prod.py
  .venv/bin/python docs/demo/grabar_alta_prod.py ensamblar

Guion (OK de Daryl 2026-11-17 — SIN teardown; frenos sin reintentos):
   0. Login de DARYL (2FA) en la ventana del driver.
   1. La ficha precargada: entorno altanapipa + 4 repos en orden con token.
   2. Validación del token por la API de GitHub SIN clonar: «Sincronizar
      commits» de los 4 repos (lista la rama 19.0 con el token cifrado de
      cada ficha; el token jamás pasa por este script).
   3. El wizard, llenado EN CÁMARA: t3.medium · 30 GB gp3 · PG16 local ·
      dominio real altanapipa.primatecloudmanager.com (Route 53) + HTTPS ·
      IP fija · CloudWatch · wkhtmltopdf release · -i + pt_BR · openpyxl ·
      limits 600/1200. PAUSA: Daryl tipea la maestra en el drawer.
   4. Montaje real (clones en orden + -i sobre los clones) → activo.
   5. «Verificar instalación»: 59/59 contra docs/MODULES_PROD.txt.
   6. El servidor por dentro (por PCM): addons_path EXACTO, conf §6,
      wkhtmltopdf + PDF real, openpyxl, pt_BR, nginx, ramas 19.0, higiene
      del token en el servidor.
   7. CloudWatch ACTIVO + Elastic IP asociada + DNS real resolviendo.
   8. O site no ar (HTTPS válido, / sin /odoo, 404, /agendar→/contato).
   9. web_responsive: menú de apps funcional en el navegador.
  10. Higiene del token lado PCM. Tarjetas: EN PRODUCCIÓN + pasos manuales.
"""
import json
import os  # noqa: F401
import pathlib
import re
import subprocess
import sys
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
ENV_NOMBRE = "altanapipa"
SLUG = "altanapipa"
DOMINIO = "altanapipa.primatecloudmanager.com"
ZONA = "Z06178081BO0Q1OK7MTAO"
CONF_SRV = "/etc/odoo/%s.conf" % SLUG
REPOS_DIR = "/opt/pcm/instances/%s/repos" % SLUG
RUNTIME_ADDONS = "/opt/pcm/runtime/odoo-19/src/addons"
ADDONS_SUELTO = "/opt/pcm/instances/%s/addons" % SLUG
ORDEN_REPOS = ["webOCA", "surveyOCA", "primate_portal", "alta_na_pipa"]
PATRON_TOKEN_SAFE = "gh[p]_|github_[p]at_"
ADMIN_NUEVO = ("admin", "admin")      # el Odoo recién nacido (cambiarla: paso manual)
FLAGS = REPO / "infra" / "alta_prod"
SALIDA = HERE
FACTORES = {"montaje": 32, "verificar": 8, "pausa": 32, "sync": 8}

ox.MARCA_PLACA = "ALTA NA PIPA · MONTAJE DE PRODUCCIÓN"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos, videos_extra = [], [], [], {}, []
T0 = None
CTX = None


def evento(texto):
    print("PCM_ALTA_PROD: %s" % texto, flush=True)


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


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=400000)
    datos = r.json()
    assert "error" not in datos, json.dumps(datos["error"])[:400]
    return datos["result"]


def correr(orden, timeout=600):
    return subprocess.run(orden, timeout=timeout, capture_output=True,
                          text=True)


def curl(url):
    out = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                  "--max-time", "15", url])
    return out.stdout.strip()


# ------------------------------------------------------------ UI helpers

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
    raise RuntimeError("no apareció la tarjeta del entorno")


def abrir_servidor(page):
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=SLUG).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def campo_pcm(page, etiqueta):
    return page.locator(
        ".o_pcm_field:has(.o_pcm_field_label:has-text('%s')) input"
        % etiqueta).first


def check_owl(page, texto):
    return page.locator("label.o_pcm_form_check:has-text('%s') input"
                        % texto).first


def set_check(page, texto, valor):
    caja = check_owl(page, texto)
    if caja.is_checked() != valor:
        ox.clic(page, caja)
        page.wait_for_timeout(500)


def comando(page, orden, explicacion, espera=18000, timeout=240):
    """«Ejecutar comando» por el wizard; los asserts viajan DENTRO del
    comando (grep -q … || exit 1): un check roto corta la toma (FRENO)."""
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
    raise RuntimeError("comando SSM sin terminar: %s" % orden[:80])


def esperar_flag(page, nombre, titulo, detalle):
    bandera = FLAGS / nombre
    evento("PAUSA · esperando %s" % nombre)
    with Ventana("pausa-%s" % nombre, FACTORES["pausa"]):
        ox.rotulo(page, titulo, detalle + " · espera real, acelerada", 2600)
        while not bandera.exists():
            page.wait_for_timeout(4000)
    evento("PAUSA LEVANTADA · %s" % nombre)
    page.wait_for_timeout(800)


# ------------------------------------------------------------ tramos

def precondiciones():
    FLAGS.mkdir(parents=True, exist_ok=True)
    (FLAGS / "flag_maestra").unlink(missing_ok=True)
    envs = orm("primate.cloud.environment", "search_read",
               [[["name", "=", ENV_NOMBRE]], ["id", "state"]], {"context": {}})
    assert envs and envs[0]["state"] == "draft", (
        "el entorno no está en Borrador: %s" % envs)
    env_id = envs[0]["id"]
    repos = orm("primate.cloud.repository", "search_read",
                [[["environment_id", "=", env_id]],
                 ["id", "name", "sequence", "github_token_encrypted",
                  "configured_branch"]],
                {"order": "sequence", "context": {}})
    assert [r["name"] for r in repos] == ORDEN_REPOS, repos
    sin = [r["name"] for r in repos if not r["github_token_encrypted"]]
    assert not sin, "repos SIN token: %s" % sin
    evento("precondiciones OK: draft + 4 repos con token en orden")
    return env_id, {r["name"]: r["id"] for r in repos}


def t1_ficha(page):
    abrir_app(page)
    abrir_entorno(page)
    ox.rotulo(page, "PRODUCCIÓN, de verdad",
              "PCM Producción · cuenta real · dominio propio", 3200)
    ox.desplazar(page, 900)
    ox.rotulo(page, "Los 4 repos NO públicos, en ORDEN, con token cifrado",
              "webOCA → surveyOCA → primate_portal → alta_na_pipa · "
              "odoo/odoo lo cubre el runtime", 4200)
    page.wait_for_timeout(2000)
    ox.desplazar(page, -900)
    marca("ficha mostrada")


def t2_validar_token(page, repo_ids):
    """La API de GitHub con el token de cada ficha, SIN clonar: lista la
    rama 19.0. Un token sin acceso muere acá, antes de crear la EC2."""
    with Ventana("sync-tokens", FACTORES["sync"]):
        for nombre in ORDEN_REPOS:
            rid = repo_ids[nombre]
            orm("primate.cloud.repository", "action_sync_commits", [[rid]])
            t0 = time.time()
            while time.time() - t0 < 180:
                rep = orm("primate.cloud.repository", "read",
                          [[rid], ["sync_state", "latest_commit"]],
                          {"context": {}})[0]
                if rep["sync_state"] == "error":
                    raise RuntimeError(
                        "TOKEN SIN ACCESO (o rama inexistente) en %s" % nombre)
                if rep["latest_commit"]:
                    evento("token OK en %s · HEAD %s"
                           % (nombre, rep["latest_commit"][:9]))
                    break
                page.wait_for_timeout(4000)
            else:
                raise RuntimeError("sync de %s sin resultado" % nombre)
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    ox.desplazar(page, 900)
    ox.rotulo(page, "Token validado por la API, sin clonar",
              "Los 4 repos listan su rama 19.0 con el token de la ficha "
              "(el token no pasa por el driver)", 4200)
    page.wait_for_timeout(2200)
    ox.desplazar(page, -900)
    marca("token validado en los 4 repos")


def t3_wizard_y_montaje(page, env_id):
    ox.clic(page, page.locator("button", has_text="Aprovisionar").first)
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1500)
    campo_pcm(page, "Nombre de la instancia").fill(SLUG)
    campo_pcm(page, "Tipo de instancia").fill("t3.medium")
    campo_pcm(page, "Disco raíz (GB)").fill("30")
    campo_pcm(page, "Dominio").fill(DOMINIO)
    campo_pcm(page, "Nombre de la base").fill("altanapipa")
    ox.rotulo(page, "El fierro de producción",
              "t3.medium · 30 GB gp3 · Ubuntu 24.04 · PG16 local", 3000)
    set_check(page, "Dominio sslip.io", False)
    set_check(page, "IP fija (Elastic IP", True)
    set_check(page, "Crear registro DNS", True)
    page.wait_for_timeout(600)
    campo_pcm(page, "Hosted Zone ID").fill(ZONA)
    set_check(page, "Emitir HTTPS al terminar", True)
    set_check(page, "Monitoreo CloudWatch", True)
    ox.rotulo(page, "Dominio REAL + HTTPS + monitoreo",
              "%s · Route 53 · Let's Encrypt · agente CloudWatch" % DOMINIO,
              3600)
    ox.desplazar(page, 500)
    campo_pcm(page, "Módulos a instalar (-i)").fill(
        "alta_na_pipa_website,alta_awards_portal,alta_ranking_portal,"
        "web_responsive")
    campo_pcm(page, "Idioma inicial").fill("pt_BR")
    campo_pcm(page, "Paquetes Python extra").fill("openpyxl")
    campo_pcm(page, "wkhtmltopdf (release GitHub)").fill("0.12.6.1-3")
    campo_pcm(page, "limit_time_cpu (s)").fill("600")
    campo_pcm(page, "limit_time_real (s)").fill("1200")
    ox.rotulo(page, "Montaje a la carta",
              "-i de los 4 módulos con pt_BR · openpyxl · wkhtmltopdf del "
              "release · limits del runbook", 3800)
    ox.desplazar(page, -500)
    esperar_flag(page, "flag_maestra",
                 "PAUSA: Daryl tipea la contraseña maestra",
                 "En el campo del drawer; no se muestra en pantalla")
    ox.clic(page, page.locator(".o_pcm_drawer button",
                               has_text="Aprovisionar").last)
    page.wait_for_selector(".modal", timeout=10000)
    ox.clic(page, page.locator(".modal footer button.btn-primary").first)
    marca("aprovisionamiento encolado")
    ox.rotulo(page, "Montando PRODUCCIÓN",
              "EC2 + DNS real + wkhtmltopdf + Odoo + clones EN ORDEN + "
              "-i + HTTPS + CloudWatch — espera real ×%d" % FACTORES["montaje"],
              3200)
    with Ventana("montaje", FACTORES["montaje"]):
        t0 = time.time()
        while time.time() - t0 < 5400:
            estado = orm("primate.cloud.environment", "read",
                         [[env_id], ["state"]], {"context": {}})[0]["state"]
            if estado in ("active", "error"):
                break
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("el montaje no terminó en 90 min")
    assert estado == "active", "el montaje terminó en ERROR — FRENO"
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(2500)
    marca("PRODUCCIÓN activa")
    tiempos["montaje"] = time.time() - T0


def t4_verificar_instalacion(page, env_id):
    abrir_entorno(page)
    ox.desplazar(page, 900)
    previos = orm("primate.cloud.operation.log", "search",
                  [[["name", "like", "Verificar instalaci"]]],
                  {"limit": 1, "order": "id desc", "context": {}})
    ultimo = previos[0] if previos else 0
    ox.clic(page, page.locator("button", has_text="Verificar instalación").first)
    page.wait_for_timeout(2500)
    ox.rotulo(page, "Verificar instalación",
              "59 módulos contra docs/MODULES_PROD.txt del clon", 3400)
    with Ventana("verificar", FACTORES["verificar"]):
        t0 = time.time()
        while time.time() - t0 < 240:
            filas = orm("primate.cloud.operation.log", "search_read",
                        [[["name", "like", "Verificar instalaci"],
                          ["id", ">", ultimo]], ["result", "error_message"]],
                        {"limit": 1, "order": "id desc", "context": {}})
            if filas and filas[0]["result"]:
                assert filas[0]["result"] == "success", (
                    "VERIFICACIÓN ROJA: %s" % filas[0]["error_message"])
                break
            page.wait_for_timeout(5000)
        else:
            raise RuntimeError("verificación sin terminar")
    ox.rotulo(page, "VERDE: 59/59 contra la referencia", "", 3200)
    page.wait_for_timeout(1500)
    marca("verificación 59/59 verde")


def t5_servidor_por_dentro(page):
    abrir_entorno(page)
    abrir_servidor(page)
    esperado = ",".join([RUNTIME_ADDONS]
                        + ["%s/%s" % (REPOS_DIR, r) for r in ORDEN_REPOS]
                        + [ADDONS_SUELTO])
    comando(page,
            "grep '^addons_path' %s && grep -qxF 'addons_path = %s' %s"
            % (CONF_SRV, esperado, CONF_SRV),
            "addons_path EXACTO: runtime → webOCA → surveyOCA → "
            "primate_portal → alta_na_pipa → dir suelto")
    marca("addons_path en orden")
    comando(page,
            "grep -E '^(dbfilter|list_db|http_interface|limit_time_cpu|"
            "limit_time_real|proxy_mode)' %s "
            "&& grep -qxF 'dbfilter = ^altanapipa$' %s "
            "&& grep -qxF 'list_db = False' %s "
            "&& grep -qxF 'http_interface = 127.0.0.1' %s "
            "&& grep -qxF 'limit_time_cpu = 600' %s "
            "&& grep -qxF 'limit_time_real = 1200' %s"
            % ((CONF_SRV,) * 6),
            "El conf del runbook §6 completo")
    comando(page,
            "wkhtmltopdf --version && wkhtmltopdf --version | grep -q 0.12.6.1 "
            "&& wkhtmltopdf --version | grep -qi 'patched qt' "
            "&& sudo -u odoo wkhtmltopdf --quiet http://127.0.0.1:8069/ "
            "/tmp/alta_pdf.pdf && file /tmp/alta_pdf.pdf | grep -q PDF "
            "&& rm -f /tmp/alta_pdf.pdf",
            "wkhtmltopdf 0.12.6.1 «patched qt» + un PDF REAL", espera=25000)
    marca("wkhtmltopdf verificado con PDF")
    comando(page,
            "/opt/pcm/runtime/odoo-19/venv/bin/pip show openpyxl | head -2 "
            "&& sudo -u postgres psql -d altanapipa -At -c \"select code from "
            "res_lang where active and code='pt_BR'\" | grep -qx pt_BR "
            "&& grep -q 'client_max_body_size 100M' "
            "/etc/nginx/sites-available/pcm-%s.conf" % SLUG,
            "openpyxl + pt_BR activo + nginx de 100M")
    ramas = " && ".join(
        "sudo -u odoo git -C %s/%s rev-parse --abbrev-ref HEAD | grep -qx 19.0"
        % (REPOS_DIR, r) for r in ORDEN_REPOS)
    comando(page,
            "for r in %s; do echo \"$r: $(sudo -u odoo git -C %s/$r "
            "rev-parse --abbrev-ref HEAD)\"; done && %s"
            % (" ".join(ORDEN_REPOS), REPOS_DIR, ramas),
            "Los 4 clones en la rama 19.0")
    comando(page,
            "for r in %s; do sudo -u odoo git -C %s/$r config --get "
            "remote.origin.url; done | grep -vE '%s' | wc -l | grep -qx 4 "
            "&& ! sudo grep -rE '%s' %s/*/.git/config "
            "&& ! sudo grep -lrE '%s' /var/log/pcm-*.log"
            % (" ".join(ORDEN_REPOS), REPOS_DIR, PATRON_TOKEN_SAFE,
               PATRON_TOKEN_SAFE, REPOS_DIR, PATRON_TOKEN_SAFE),
            "El PAT no está en remotes, git-config ni logs del servidor")
    marca("servidor por dentro verificado")


def t6_cloudwatch_eip_dns(page, env_id):
    datos = orm("primate.cloud.environment", "read",
                [[env_id], ["cw_agent_state"]], {"context": {}})[0]
    assert datos["cw_agent_state"] == "active", (
        "agente CloudWatch en %s (se esperaba active)"
        % datos["cw_agent_state"])
    comando(page, "systemctl is-active amazon-cloudwatch-agent",
            "El agente CloudWatch, corriendo en el servidor")
    maquinas = orm("primate.cloud.ec2.instance", "search_read",
                   [[["environment_id", "=", env_id],
                     ["instance_state", "=", "running"]],
                    ["public_ip", "name"]], {"context": {}})
    ip = maquinas[0]["public_ip"]
    assert ip, "la máquina no reporta IP pública"
    dig = correr(["dig", "+short", DOMINIO, "@8.8.8.8"]).stdout.strip()
    assert ip in dig.splitlines(), (
        "el DNS no resuelve a la IP fija: dig=%r ip=%r" % (dig, ip))
    ox.rotulo(page, "CloudWatch ACTIVO · IP fija %s · DNS real resolviendo"
              % ip, "dig %s → la Elastic IP del servidor" % DOMINIO, 4200)
    page.wait_for_timeout(2000)
    marca("cloudwatch + eip + dns verificados")


def t7_sitio(page):
    base = "https://%s" % DOMINIO
    final = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}",
                    "-L", "--max-time", "25", base + "/"])
    assert final.returncode == 0, "HTTPS no responde o el cert no valida"
    assert "/odoo" not in final.stdout, "/ redirige a /odoo"
    assert curl(base + "/web/database/manager") == "404", "manager visible"
    agendar = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}",
                      "-L", "--max-time", "15", base + "/agendar"])
    assert "/contato" in agendar.stdout, "/agendar no redirige a /contato"
    evento("curl OK: sitio + 404 + /agendar")
    sitio = CTX.new_page()
    t_abre = time.time() - T0
    ox.ir(sitio, base + "/", timeout=60000)
    sitio.wait_for_timeout(3500)
    ox.rotulo(sitio, "O site no ar — EN PRODUCCIÓN",
              "https://%s · cert válido · / sirve el website" % DOMINIO, 4200)
    sitio.wait_for_timeout(1800)
    ruta = sitio.video.path() if sitio.video else None
    sitio.close()
    videos_extra.append({"nombre": "sitio", "desde": t_abre,
                         "hasta": time.time() - T0, "video": str(ruta)})
    marca("sitio verificado")


def t8_web_responsive(page):
    back = CTX.new_page()
    t_abre = time.time() - T0
    ox.ir(back, "https://%s/web/login" % DOMINIO, timeout=60000)
    back.fill("form:has(input[name=password]) input[name=login]",
              ADMIN_NUEVO[0])
    back.fill("form input[name=password]", ADMIN_NUEVO[1])
    back.click("form:has(input[name=password]) button[type=submit]")
    back.wait_for_url("**/odoo**", timeout=60000)
    back.wait_for_timeout(3000)
    ox.clic(back, back.locator("button.o_grid_apps_menu__button, "
                               ".o_navbar_apps_menu button").first)
    back.wait_for_timeout(2500)
    visibles = back.locator(".o_app").count()
    assert visibles >= 3, "menú de apps sin renderizar (%d)" % visibles
    ox.rotulo(back, "web_responsive FUNCIONAL (rama 19.0)",
              "El menú de apps abre y lista %d apps" % visibles, 4000)
    back.wait_for_timeout(2200)
    ruta = back.video.path() if back.video else None
    back.close()
    videos_extra.append({"nombre": "web_responsive", "desde": t_abre,
                         "hasta": time.time() - T0, "video": str(ruta)})
    marca("web_responsive verificado")


def t9_higiene_pcm(page):
    for model, campo in (("primate.cloud.operation.log", "error_message"),
                         ("primate.cloud.operation.log", "name"),
                         ("mail.message", "body")):
        for patron in ("ghp_", "github_pat_"):
            n = orm(model, "search_count", [[[campo, "ilike", patron]]],
                    {"context": {}})
            assert not n, "TOKEN FILTRADO en %s.%s (%d)" % (model, campo, n)
    ox.rotulo(page, "Higiene del token, lado PCM",
              "Ni en bitácora ni en chatter — solo cifrado en las fichas",
              3600)
    page.wait_for_timeout(1500)
    marca("higiene del token (PCM) verificada")


def grabar():
    global T0, CTX
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
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
        evento("PAUSA · login de Daryl (2FA) en esta ventana…")
        for _ in range(600):
            if "/odoo" in page.url or page.locator(".o_web_client").count():
                break
            page.wait_for_timeout(2000)
        else:
            raise RuntimeError("sin login en 20 min")
        evento("sesión activa")
        try:
            env_id, repo_ids = precondiciones()
            ox.tarjeta(page, "Alta na Pipa — PRODUCCIÓN",
                       "El montaje real, con el dominio definitivo",
                       [("Servidor", "t3.medium · 30 GB gp3 · IP fija"),
                        ("Dominio", DOMINIO),
                        ("Odoo", "19 Community · 59 módulos · pt_BR"),
                        ("Monitoreo", "agente CloudWatch")], 6000)
            ox.tarjeta(page, "1 · La ficha", "4 repos en orden, tokens cifrados")
            t1_ficha(page)
            evento("TRAMO 1 OK")
            ox.tarjeta(page, "2 · El token, validado por la API",
                       "Sin clonar: la rama 19.0 de los 4 repos")
            t2_validar_token(page, repo_ids)
            evento("TRAMO 2 OK")
            ox.tarjeta(page, "3 · El montaje", "Wizard en cámara + maestra de Daryl")
            t3_wizard_y_montaje(page, env_id)
            evento("TRAMO 3 OK")
            ox.tarjeta(page, "4 · Verificar instalación", "59 vs MODULES_PROD.txt")
            t4_verificar_instalacion(page, env_id)
            evento("TRAMO 4 OK")
            ox.tarjeta(page, "5 · El servidor por dentro", "Todo por PCM")
            t5_servidor_por_dentro(page)
            evento("TRAMO 5 OK")
            ox.tarjeta(page, "6 · CloudWatch · IP fija · DNS real", "")
            t6_cloudwatch_eip_dns(page, env_id)
            evento("TRAMO 6 OK")
            ox.tarjeta(page, "7 · O site no ar", DOMINIO)
            t7_sitio(page)
            evento("TRAMO 7 OK")
            ox.tarjeta(page, "8 · web_responsive", "Menú de apps funcional")
            t8_web_responsive(page)
            evento("TRAMO 8 OK")
            t9_higiene_pcm(page)
            evento("TRAMO 9 OK")
            ox.tarjeta(page, "EN PRODUCCIÓN", "https://%s" % DOMINIO,
                       [("Montaje", _dur(tiempos.get("montaje"))),
                        ("Checks", "59/59 · addons_path · wkhtmltopdf · "
                                   "token · CW · EIP · sitio"),
                        ("Sin teardown", "el servidor queda sirviendo"),
                        ("Incidentes", str(len(incidentes)) or "ninguno")],
                       6000)
            ox.tarjeta(page, "Pasos manuales (Daryl + Diego)",
                       "Lo que PCM no decide por ustedes",
                       [("1", "Compañía «Alta na Pipa», país BR"),
                        ("2", "Cambiar la contraseña de admin del Odoo nuevo"),
                        ("3", "Número de WhatsApp (hoy /agendar → /contato)"),
                        ("4", "SMTP saliente"),
                        ("5", "Padrón Tecnofit (.xlsx, NO se versiona)")], 7000)
            marca("fin")
        except Exception as error:   # noqa: BLE001
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(SALIDA / "alta_prod_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            ruta_video = page.video.path() if page.video else None
            CTX.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes,
             "videos_extra": videos_extra}
    destino = bruto / "toma_alta_prod.webm"
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    for extra in videos_extra:
        origen = pathlib.Path(extra["video"])
        if origen.exists():
            nuevo = bruto / ("toma_alta_prod_%s.webm" % extra["nombre"])
            origen.replace(nuevo)
            extra["video"] = str(nuevo)
    (SALIDA / "toma_alta_prod.json").write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d"
           % (destino, len(incidentes)))
    for i in incidentes:
        print("  ⚠️ ", i)
    if excepcion:
        raise excepcion


def _dur(segundos):
    if not segundos:
        return "—"
    return "%d:%02d" % (segundos // 60, segundos % 60)


def ensamblar():
    """Toma principal con ventanas + pestañas (sitio, backend) empalmadas."""
    datos = json.loads((SALIDA / "toma_alta_prod.json").read_text())
    final = SALIDA / "alta_na_pipa_produccion.mp4"
    extras = sorted(datos.get("videos_extra", []), key=lambda e: e["desde"])
    vens = sorted(datos["ventanas"], key=lambda v: v["desde"])
    # Piezas: principal cortada alrededor de cada pestaña; la pestaña entera.
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
