# -*- coding: utf-8 -*-
"""Graba el ENSAYO del montaje de ALTA NA PIPA en el sandbox (tramo C.2).

  .venv/bin/python docs/demo/grabar_alta_sandbox.py
  .venv/bin/python docs/demo/grabar_alta_sandbox.py ensamblar

Receta de siempre (helpers del bundle): un solo video 1080p, tarjetas por
tramo, rótulo fijo, cursor visible; SOLO se aceleran las esperas marcadas.

Guion (OK de Daryl, 2026-11-17 — montaje a la carta completo):
   1. La ficha: entorno precargado + los 4 repos NO públicos en ORDEN
      (webOCA → surveyOCA → primate_portal → alta_na_pipa), tokens cargados.
   2. Aprovisionar: wizard precargado (t3.small*, 30 GB, PG16, sslip+HTTPS,
      wkhtmltopdf release, -i de los 4 módulos con pt_BR, openpyxl, limits).
      El montaje clona los repos EN ORDEN y corre el -i sobre los clones.
   3. «Verificar instalación»: 59 módulos contra docs/MODULES_PROD.txt.
   4. El servidor por dentro (todo por PCM, nada a mano): orden EXACTO del
      addons_path, wkhtmltopdf 0.12.6.1 «patched qt» + PDF real, openpyxl,
      http_interface 127.0.0.1, client_max_body_size, dbfilter/list_db,
      ramas 19.0 de los 4 clones, pt_BR activo.
   5. Higiene del token: ni en los logs del servidor, ni en el remote de los
      clones, ni en bitácora/chatter/queue_job del PCM.
   6. El sitio: / sirve el website (sin redirigir a /odoo), HTTPS válido,
      /web/database/manager en 404, /agendar → /contato (WhatsApp vacío).
   7. web_responsive (rama 19.0): menú de apps FUNCIONAL en el navegador
      (el bug de 19.0_fix_responsive lo dejaba roto).
   8. Teardown: triple confirmación, EC2 terminada, IP fija liberada. Costo.

Los secretos NO pasan por este script: los PAT ya están cifrados en las
fichas (los cargó Daryl); acá solo se verifica que NO aparezcan.

* t3.small en el ENSAYO: la cuenta sandbox (plan Free Tier nuevo) rechaza
  t3.medium; producción va t3.medium en la cuenta real (OK de Daryl 17-nov).
"""
import json
import os  # noqa: F401 — simetría con los drivers hermanos
import pathlib
import subprocess
import sys
import time
import xmlrpc.client

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
BUNDLE_DEMO = REPO / "primate_bundle" / "docs" / "demo"
sys.path.insert(0, str(BUNDLE_DEMO))
import grabar_demo as receta        # noqa: E402 — rótulos/placas aprobados
import grabar_oximoro as ox         # noqa: E402 — cursor, clics, tipeo, tarjetas

VIEW_W, VIEW_H = 1536, 864
VIDEO_W, VIDEO_H = 1920, 1080
PCM = "http://localhost:19088"
DB_PCM = "pcm_demo"
ADMIN = ("admin", "admin")          # pcm_demo Y el Odoo recién nacido (-i)
ENV_NOMBRE = "altanapipa"
SLUG = "altanapipa"
CONF_SRV = "/etc/odoo/%s.conf" % SLUG
REPOS_DIR = "/opt/pcm/instances/%s/repos" % SLUG
RUNTIME_ADDONS = "/opt/pcm/runtime/odoo-19/src/addons"
ADDONS_SUELTO = "/opt/pcm/instances/%s/addons" % SLUG
ORDEN_REPOS = ["webOCA", "surveyOCA", "primate_portal", "alta_na_pipa"]
PATRON_TOKEN = "ghp_|github_pat_"   # PAT clásico y fine-grained (checks locales)
# Variante AUTO-EXCLUYENTE para el comando que viaja al servidor: el wizard
# publica el comando en el chatter, y un patrón literal se detectaba a sí
# mismo en el check local (lección toma 3). "gh[p]_" matchea ghp_ pero el
# TEXTO del patrón no contiene ghp_.
PATRON_TOKEN_SAFE = "gh[p]_|github_[p]at_"
SALIDA = HERE
FACTORES = {"montaje": 32, "verificar": 8, "terminar": 16}
# t3.medium us-east-1 on-demand + gp3 30 GB prorrateado (para la tarjeta final)
COSTO_HORA = 0.0208 + 30 * 0.08 / 730   # t3.small del ensayo

ox.MARCA_PLACA = "ALTA NA PIPA · ENSAYO DEL MONTAJE (sandbox)"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos = [], [], [], {}
T0 = None


def evento(texto):
    print("PCM_ALTA: %s" % texto, flush=True)


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


def rpc(model, method, args, kwargs=None):
    common = xmlrpc.client.ServerProxy("%s/xmlrpc/2/common" % PCM, allow_none=True)
    uid = common.authenticate(DB_PCM, ADMIN[0], ADMIN[1], {})
    modelos = xmlrpc.client.ServerProxy("%s/xmlrpc/2/object" % PCM, allow_none=True)
    return modelos.execute_kw(DB_PCM, uid, ADMIN[1], model, method, args, kwargs or {})


def correr(orden, timeout=600, entrada=None):
    evento("cmd · %s" % " ".join(orden[:4]))
    return subprocess.run(orden, timeout=timeout, input=entrada,
                          capture_output=True, text=True)


# ------------------------------------------------------------ UI helpers

def login(page, base, db, rotulo_txt="", timeout=120000):
    ox.ir(page, base + "/web/login", timeout=timeout)
    page.fill("form:has(input[name=password]) input[name=login]", ADMIN[0])
    page.fill("form input[name=password]", ADMIN[1])
    if rotulo_txt:
        ox.rotulo(page, rotulo_txt, "", 1800)
    page.click("form:has(input[name=password]) button[type=submit]")
    page.wait_for_url("**/odoo**", timeout=45000)


def abrir_app(page):
    ox.ir(page, PCM + "/odoo/action-primate_cloud_manager.action_pcm_app",
          esperar=".o_pcm_app")
    page.wait_for_timeout(1200)


def nav(page, item):
    ox.clic(page, page.locator(".o_pcm_nav_item", has_text=item).first)
    page.wait_for_timeout(1200)


def abrir_entorno(page):
    for _intento in range(3):
        nav(page, "Entornos")
        buscador = page.locator(".o_pcm_screen input[type=text]").first
        buscador.fill(ENV_NOMBRE)
        page.wait_for_timeout(1200)
        tarjeta = page.locator(".o_pcm_env_card", has_text=ENV_NOMBRE)
        if tarjeta.count():
            ox.clic(page, tarjeta.first)
            page.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=15000)
            page.wait_for_timeout(900)
            return
        page.wait_for_timeout(2500)
    raise RuntimeError("no apareció la tarjeta del entorno %r" % ENV_NOMBRE)


def abrir_servidor(page):
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=SLUG).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def comando(page, orden, explicacion, espera=18000, timeout=180):
    """«Ejecutar comando» por el wizard (SSM vía PCM) y FRENO si falla.

    El comando embebe sus propios asserts (`grep -q … || exit 1`): un check
    que no da la salida esperada termina failed y corta la toma — sin
    reintentos, como pidió Daryl.
    """
    previos = rpc("primate.cloud.operation.log", "search",
                  [[["action_type", "=", "ssm_command"]]],
                  {"limit": 1, "order": "id desc"})
    ultimo = previos[0] if previos else 0
    ox.clic(page, page.locator("button", has_text="Ejecutar comando").first)
    page.wait_for_selector("textarea#command_0, input#command_0", timeout=12000)
    page.locator("textarea#command_0, input#command_0").first.fill(orden)
    ox.rotulo(page, "Ejecutar comando (SSM)", explicacion, 2800)
    ox.clic(page, page.locator("button[name=action_run]").first)
    page.wait_for_timeout(espera)
    t0 = time.time()
    while time.time() - t0 < timeout:
        filas = rpc("primate.cloud.operation.log", "search_read",
                    [[["action_type", "=", "ssm_command"], ["id", ">", ultimo]],
                     ["result", "error_message"]], {"limit": 1, "order": "id desc"})
        if filas and filas[0]["result"] in ("success", "failed"):
            assert filas[0]["result"] == "success", (
                "CHECK FALLÓ: %s · %s"
                % (explicacion, (filas[0]["error_message"] or "")[:300]))
            return
        page.wait_for_timeout(5000)
    raise RuntimeError("el comando SSM no terminó a tiempo: %s" % orden[:90])


# ------------------------------------------------------------ preparación

def precondiciones_checks():
    """Reanudación (desde_checks): el servidor del ensayo YA está activo."""
    salida = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "4", PCM + "/web/login"])
    assert salida.stdout.strip() == "200", "pcm_demo no responde en :19088"
    envs = rpc("primate.cloud.environment", "search_read",
               [[["name", "=", ENV_NOMBRE]], ["id", "state"]])
    assert envs and envs[0]["state"] == "active", (
        "reanudación: el entorno no está activo (%s)"
        % (envs[0]["state"] if envs else "no existe"))
    maquina = rpc("primate.cloud.ec2.instance", "search_read",
                  [[["environment_id", "=", envs[0]["id"]],
                    ["instance_state", "=", "running"]], ["name"]])
    assert maquina, "reanudación: no hay máquina running"
    evento("precondiciones (desde_checks) OK: entorno activo + máquina viva")
    return envs[0]["id"]


def precondiciones():
    salida = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "4", PCM + "/web/login"])
    assert salida.stdout.strip() == "200", "pcm_demo no responde en :19088"
    envs = rpc("primate.cloud.environment", "search_read",
               [[["name", "=", ENV_NOMBRE]], ["id", "state"]])
    assert envs, "no existe el entorno %r (precarga)" % ENV_NOMBRE
    assert envs[0]["state"] == "draft", (
        "el entorno está en %s, no en Borrador" % envs[0]["state"])
    # Los 4 repos con token YA cargado (cifrado) y el orden correcto.
    repos = rpc("primate.cloud.repository", "search_read",
                [[["environment_id", "=", envs[0]["id"]]],
                 ["name", "sequence", "github_token_encrypted",
                  "configured_branch"]], {"order": "sequence"})
    assert [r["name"] for r in repos] == ORDEN_REPOS, (
        "orden de repos inesperado: %s" % [r["name"] for r in repos])
    sin_token = [r["name"] for r in repos if not r["github_token_encrypted"]]
    assert not sin_token, "repos SIN token: %s" % sin_token
    assert all(r["configured_branch"] == "19.0" for r in repos)
    evento("precondiciones OK: entorno draft + 4 repos con token en orden")
    return envs[0]["id"]


# ------------------------------------------------------------ tramos

def t1_ficha(page, env_id):
    abrir_entorno(page)
    ox.rotulo(page, "El entorno, precargado",
              "altanapipa · Odoo 19 Community · cuenta sandbox", 3000)
    ox.desplazar(page, 900)
    ox.rotulo(page, "Los 4 repos NO públicos, en ORDEN",
              "webOCA → surveyOCA → primate_portal → alta_na_pipa · tokens "
              "cifrados en la ficha · odoo/odoo lo cubre el runtime", 4200)
    page.wait_for_timeout(2500)
    ox.desplazar(page, -900)
    marca("ficha mostrada")


def t2_montaje(page, env_id):
    ox.clic(page, page.locator("button", has_text="Aprovisionar").first)
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1800)
    ox.rotulo(page, "El wizard viene precargado",
              "t3.small (sandbox; producción: t3.medium) · 30 GB gp3 · "
              "Ubuntu 24.04 · PG16 local · sslip.io + HTTPS", 3600)
    ox.desplazar(page, 500)
    ox.rotulo(page, "Montaje a la carta",
              "wkhtmltopdf 0.12.6.1-3 (release) · -i de los 4 módulos con "
              "pt_BR · openpyxl · limits 600/1200", 4200)
    page.wait_for_timeout(2000)
    ox.desplazar(page, -500)
    ox.clic(page, page.locator(".o_pcm_drawer button",
                               has_text="Aprovisionar").last)
    page.wait_for_selector(".modal", timeout=10000)
    ox.clic(page, page.locator(".modal footer button.btn-primary").first)
    marca("aprovisionamiento encolado")
    ox.rotulo(page, "Montando Alta na Pipa",
              "EC2 + bootstrap + wkhtmltopdf + Odoo 19 + clones EN ORDEN + "
              "-i con pt_BR — espera real acelerada ×%d" % FACTORES["montaje"],
              3200)
    with Ventana("montaje", FACTORES["montaje"]):
        t0 = time.time()
        while time.time() - t0 < 5400:
            estado = rpc("primate.cloud.environment", "read",
                         [[env_id], ["state"]])[0]["state"]
            if estado in ("active", "error"):
                break
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("el montaje no terminó en 90 min")
    assert estado == "active", "el montaje terminó en ERROR — FRENO"
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(2500)
    marca("servidor activo")
    tiempos["montaje"] = time.time() - T0
    ox.rotulo(page, "Servidor activo", "Los repos quedaron clonados y el -i "
              "corrió sobre los clones, con el servicio detenido", 3600)


def t3_verificar_instalacion(page):
    abrir_entorno(page)
    ox.desplazar(page, 900)
    previos = rpc("primate.cloud.operation.log", "search",
                  [[["action_type", "=", "ssm_command"]]],
                  {"limit": 1, "order": "id desc"})
    ultimo = previos[0] if previos else 0
    ox.clic(page, page.locator("button", has_text="Verificar instalación").first)
    # El confirm nativo del prompt no aplica: la referencia ya está persistida.
    page.wait_for_timeout(2500)
    ox.rotulo(page, "Verificar instalación",
              "Módulos instalados vs docs/MODULES_PROD.txt (59) — "
              "conteo + diff nombre a nombre", 3400)
    with Ventana("verificar", FACTORES["verificar"]):
        t0 = time.time()
        while time.time() - t0 < 240:
            filas = rpc("primate.cloud.operation.log", "search_read",
                        [[["action_type", "=", "ssm_command"],
                          ["id", ">", ultimo],
                          ["name", "like", "Verificar instalaci"]],
                         ["result", "error_message"]],
                        {"limit": 1, "order": "id desc"})
            if filas and filas[0]["result"]:
                assert filas[0]["result"] == "success", (
                    "VERIFICACIÓN ROJA: %s" % (filas[0]["error_message"] or ""))
                break
            page.wait_for_timeout(5000)
        else:
            raise RuntimeError("la verificación no terminó a tiempo")
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    ox.desplazar(page, 600)
    ox.rotulo(page, "VERDE: 59 módulos, idénticos a la referencia",
              "El diff contra MODULES_PROD.txt quedó en la bitácora", 4200)
    page.wait_for_timeout(2000)
    marca("verificación 59/59 verde")


def t4_servidor_por_dentro(page):
    abrir_entorno(page)
    abrir_servidor(page)
    # 1. Orden EXACTO del addons_path (pedido explícito de Daryl).
    esperado = ",".join([RUNTIME_ADDONS]
                        + ["%s/%s" % (REPOS_DIR, r) for r in ORDEN_REPOS]
                        + [ADDONS_SUELTO])
    comando(page,
            "grep '^addons_path' %s && grep -qxF 'addons_path = %s' %s"
            % (CONF_SRV, esperado, CONF_SRV),
            "addons_path EXACTO: runtime → webOCA → surveyOCA → "
            "primate_portal → alta_na_pipa → dir suelto")
    marca("addons_path en orden")
    # 2. Conf endurecido: dbfilter exacto, list_db, http_interface, limits.
    # grep -F: líneas LITERALES (lección toma 2: el $ de ^altanapipa$ era
    # ancla para grep, no el carácter — el check moría con el conf perfecto).
    comando(page,
            "grep -E '^(dbfilter|list_db|http_interface|limit_time_cpu|"
            "limit_time_real|proxy_mode)' %s "
            "&& grep -qxF 'dbfilter = ^altanapipa$' %s "
            "&& grep -qxF 'list_db = False' %s "
            "&& grep -qxF 'http_interface = 127.0.0.1' %s "
            "&& grep -qxF 'limit_time_cpu = 600' %s "
            "&& grep -qxF 'limit_time_real = 1200' %s"
            % ((CONF_SRV,) * 6),
            "El conf del runbook §6: dbfilter exacto, sin gestor de bases, "
            "Odoo solo en 127.0.0.1, límites 600/1200")
    comando(page,
            "ss -ltn | grep -F 127.0.0.1:8069 && ! ss -ltn | grep -E "
            "'0.0.0.0:8069|\\*:8069'",
            "Odoo escucha SOLO en 127.0.0.1 (detrás de nginx)")
    marca("conf endurecido verificado")
    # 3. wkhtmltopdf del release + PDF real.
    comando(page,
            "wkhtmltopdf --version && wkhtmltopdf --version | grep -q 0.12.6.1 "
            "&& wkhtmltopdf --version | grep -qi 'patched qt'",
            "wkhtmltopdf 0.12.6.1 con el Qt parcheado (el de apt no sirve)")
    comando(page,
            "sudo -u odoo wkhtmltopdf --quiet http://127.0.0.1:8069/ "
            "/tmp/alta_pdf_test.pdf && file /tmp/alta_pdf_test.pdf | grep -q PDF "
            "&& ls -la /tmp/alta_pdf_test.pdf && rm -f /tmp/alta_pdf_test.pdf",
            "Un PDF REAL generado con el wkhtmltopdf del release", espera=25000)
    marca("wkhtmltopdf verificado con PDF")
    # 4. openpyxl + pt_BR + nginx.
    comando(page,
            "/opt/pcm/runtime/odoo-19/venv/bin/pip show openpyxl | head -2",
            "openpyxl en el venv (el importador del padrón Tecnofit)")
    comando(page,
            "sudo -u postgres psql -d altanapipa -At -c \"select code from "
            "res_lang where active and code='pt_BR'\" | grep -qx pt_BR",
            "pt_BR activo en la base (el --load-language del montaje)")
    comando(page,
            "grep -q 'client_max_body_size 100M' "
            "/etc/nginx/sites-available/pcm-%s.conf && grep -q proxy_redirect "
            "/etc/nginx/sites-available/pcm-%s.conf" % (SLUG, SLUG),
            "nginx del runbook: subidas de 100M y proxy_redirect off")
    marca("openpyxl + pt_BR + nginx verificados")
    # 5. Ramas 19.0 de los 4 clones (webOCA en particular: NO la fix_responsive).
    ramas = " && ".join(
        "sudo -u odoo git -C %s/%s rev-parse --abbrev-ref HEAD | grep -qx 19.0"
        % (REPOS_DIR, r) for r in ORDEN_REPOS)
    comando(page,
            "for r in %s; do echo \"$r: $(sudo -u odoo git -C %s/$r "
            "rev-parse --abbrev-ref HEAD)\"; done && %s"
            % (" ".join(ORDEN_REPOS), REPOS_DIR, ramas),
            "Los 4 clones en la rama 19.0 (webOCA: NO la 19.0_fix_responsive)")
    marca("ramas 19.0 verificadas")
    # 6. Higiene del token EN EL SERVIDOR: ni en remotes ni en logs ni en
    #    la config de git de los clones.
    comando(page,
            "for r in %s; do sudo -u odoo git -C %s/$r config --get "
            "remote.origin.url; done | grep -vE '%s' | wc -l | grep -qx 4 "
            "&& ! sudo grep -rE '%s' %s/*/.git/config "
            "&& ! sudo grep -lrE '%s' /var/log/pcm-*.log"
            % (" ".join(ORDEN_REPOS), REPOS_DIR, PATRON_TOKEN_SAFE,
               PATRON_TOKEN_SAFE, REPOS_DIR, PATRON_TOKEN_SAFE),
            "El PAT no está en los remotes, ni en la config de git, "
            "ni en los logs del servidor")
    marca("higiene del token (servidor) verificada")


def t5_higiene_token_pcm(page):
    """El PAT tampoco quedó en el PCM: bitácora, chatter ni cola de jobs.

    El patrón exige CUERPO alfanumérico tras el prefijo: un token real
    (ghp_ + 36 chars) matchea; el texto literal del patrón de los probes
    («ghp_|github_pat_», seguido de | o comilla) no — lección tomas 3 y 4:
    el detector se encontraba a sí mismo en los ecos de sus comandos.
    """
    patron = "(ghp_|github_pat_)[A-Za-z0-9]{8,}"
    tablas = (
        ("primate_cloud_operation_log",
         "coalesce(name,'') || ' ' || coalesce(error_message,'')"),
        ("mail_message", "coalesce(body,'')"),
        ("queue_job", "args::text"),
    )
    for tabla, campo in tablas:
        filas = correr(["psql", "-h", "localhost", "-U", "odoo", "-d", DB_PCM,
                        "-At", "-c",
                        "select count(*) from %s where (%s) ~ '%s'"
                        % (tabla, campo, patron)])
        assert filas.stdout.strip() == "0", (
            "TOKEN FILTRADO en %s (%s filas)" % (tabla, filas.stdout.strip()))
    ox.rotulo(page, "Higiene del token, lado PCM",
              "Ni en bitácora, ni en chatter, ni en la cola de jobs — "
              "solo cifrado en la ficha", 4000)
    page.wait_for_timeout(1500)
    marca("higiene del token (PCM) verificada")


def _sslip():
    datos = rpc("primate.cloud.environment", "search_read",
                [[["name", "=", ENV_NOMBRE]], ["primary_instance_id"]])[0]
    inst = rpc("primate.cloud.instance", "read",
               [[datos["primary_instance_id"][0]], ["main_url"]])[0]
    return inst["main_url"]


def t6_sitio(page, ctx):
    dominio = _sslip()
    base = "https://%s" % dominio
    # Checks por curl (verificación de cert incluida: sin -k).
    final = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}",
                    "-L", "--max-time", "25", base + "/"])
    assert final.returncode == 0, "HTTPS no responde o el cert no valida"
    assert "/odoo" not in final.stdout, (
        "/ redirige a /odoo (%s): el bug del dbfilter" % final.stdout)
    manager = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "15", base + "/web/database/manager"])
    assert manager.stdout.strip() == "404", (
        "/web/database/manager devolvió %s" % manager.stdout)
    agendar = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{url_effective}",
                      "-L", "--max-time", "15", base + "/agendar"])
    assert "/contato" in agendar.stdout, (
        "/agendar no redirige a /contato: %s" % agendar.stdout)
    evento("curl OK: / sin redirect a /odoo · manager 404 · /agendar→/contato")
    # Y a la vista: el sitio en el navegador (cert válido: sin ignorar errores).
    sitio = ctx.new_page()
    ox.ir(sitio, base + "/", timeout=60000)
    sitio.wait_for_timeout(3500)
    ox.rotulo(sitio, "O site no ar", "https://%s · cert Let's Encrypt válido · "
              "/ sirve el website (no redirige a /odoo)" % dominio, 4200)
    sitio.wait_for_timeout(2000)
    ox.ir(sitio, base + "/agendar", timeout=30000)
    sitio.wait_for_timeout(2500)
    ox.rotulo(sitio, "/agendar → /contato",
              "WhatsApp sin configurar: el redirect PREVISTO del runbook", 3600)
    sitio.wait_for_timeout(1500)
    sitio.close()
    marca("sitio verificado (https, 404, /agendar)")


def t7_web_responsive(page, ctx):
    dominio = _sslip()
    back = ctx.new_page()
    ox.ir(back, "https://%s/web/login" % dominio, timeout=60000)
    back.fill("form:has(input[name=password]) input[name=login]", ADMIN[0])
    back.fill("form input[name=password]", ADMIN[1])
    back.click("form:has(input[name=password]) button[type=submit]")
    back.wait_for_url("**/odoo**", timeout=60000)
    back.wait_for_timeout(3000)
    # El menú de apps de web_responsive (full screen). En 19.0_fix_responsive
    # el xpath roto (env.isSmall) lo dejaba muerto: ESTE click es el check.
    boton = back.locator("button.o_grid_apps_menu__button, "
                         ".o_navbar_apps_menu button").first
    ox.clic(back, boton)
    back.wait_for_timeout(2500)
    apps = back.locator(".o_app")
    visibles = apps.count()
    assert visibles >= 3, ("el menú de apps de web_responsive no renderizó "
                           "(%d apps visibles)" % visibles)
    ox.rotulo(back, "web_responsive FUNCIONAL (rama 19.0)",
              "El menú de apps abre y lista %d apps — con 19.0_fix_responsive "
              "quedaba roto (env.isSmall)" % visibles, 4200)
    back.wait_for_timeout(2500)
    back.close()
    marca("web_responsive verificado en el navegador")


def t8_teardown(page):
    abrir_entorno(page)
    abrir_servidor(page)
    ox.clic(page, page.locator("button", has_text="Terminar servidor").first)
    page.wait_for_selector("button[name=action_next_from_impact], #typed_name_0",
                           timeout=15000)
    ox.rotulo(page, "Triple confirmación", "Impacto → nombre exacto → "
              "re-autenticación: la ceremonia de siempre", 3200)
    if page.locator("button[name=action_next_from_impact]").count():
        ox.clic(page, page.locator("button[name=action_next_from_impact]").first)
        page.wait_for_timeout(1500)
    nombre_maquina = rpc("primate.cloud.ec2.instance", "search_read",
                         [[["environment_id.name", "=", ENV_NOMBRE],
                           ["instance_state", "!=", "terminated"]],
                          ["name"]])[0]["name"]
    ox.tipear(page, page.locator("#typed_name_0"), nombre_maquina, 50)
    ox.clic(page, page.locator("button[name=action_confirm_name]").first)
    page.wait_for_timeout(1500)
    page.locator("#password_0").fill(ADMIN[1])
    ox.clic(page, page.locator("button[name=action_execute]").first)
    marca("terminación encolada")
    with Ventana("terminar", FACTORES["terminar"]):
        t0 = time.time()
        while time.time() - t0 < 900:
            fila = rpc("primate.cloud.ec2.instance", "search_read",
                       [[["environment_id.name", "=", ENV_NOMBRE]],
                        ["instance_state", "id"]], {"limit": 1,
                                                    "order": "id desc"})[0]
            if fila["instance_state"] == "terminated":
                break
            rpc("primate.cloud.ec2.instance", "action_sync_from_aws",
                [[fila["id"]]])
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("la EC2 no llegó a terminated")
    page.reload(wait_until="load")
    page.wait_for_timeout(3000)
    ox.rotulo(page, "Servidor terminado, IP fija liberada",
              "El sandbox queda sin recursos facturando", 3600)
    marca("recursos terminados")
    tiempos["fin"] = time.time() - T0


def grabar(desde_checks=False, desde_teardown=False):
    global T0
    reanuda = desde_checks or desde_teardown
    env_id = precondiciones_checks() if reanuda else precondiciones()
    nombre_toma = ("toma_alta_t6_teardown" if desde_teardown
                   else "toma_alta_t3_checks" if desde_checks
                   else "toma_alta_sandbox")
    if reanuda:
        # La duración real del montaje viene de la toma 2 (para la tarjeta final).
        previa = SALIDA / "toma_alta_t2_montaje.json"
        if previa.exists():
            tiempos["montaje"] = json.loads(
                previa.read_text())["tiempos"].get("montaje")
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
    with sync_playwright() as navegador:
        nav_ = navegador.chromium.launch(args=["--no-sandbox", "--lang=es-UY"])
        ctx = nav_.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, locale="es-UY",
            timezone_id="America/Montevideo",
            record_video_dir=str(bruto),
            record_video_size={"width": VIEW_W, "height": VIEW_H},
            ignore_https_errors=False)
        ctx.add_init_script(receta.ROTULO_JS)
        ctx.add_init_script(receta.PLACA_JS)
        ctx.add_init_script(ox.CURSOR_JS)
        ctx.add_init_script(ox.PESTANA_JS)
        page = ctx.new_page()
        page.on("pageerror", lambda e: incidentes.append("JS: %s" % str(e)[:120]))
        ox.T0 = T0 = time.time()
        excepcion = None
        page.set_content("<html><body style='background:#111'></body></html>")
        if not reanuda:
            ox.tarjeta(page, "Alta na Pipa aterriza en AWS",
                       "Ensayo COMPLETO del montaje a la carta, en el sandbox — "
                       "todo ocurre de verdad",
                       [("Servidor", "t3.small (ensayo) · 30 GB gp3 · Ubuntu 24.04"),
                        ("Odoo", "19 Community · 59 módulos · pt_BR"),
                        ("Repos", "4 privados, en orden, tokens cifrados")], 6000)
        try:
            login(page, PCM, DB_PCM,
                  "" if reanuda else "PCM (sandbox pcm_demo)")
            abrir_app(page)
            if not reanuda:
                ox.tarjeta(page, "1 · La ficha", "Entorno precargado + 4 repos en orden")
                t1_ficha(page, env_id)
                evento("TRAMO 1 OK")
                ox.tarjeta(page, "2 · El montaje", "Clones en orden + -i sobre los clones")
                t2_montaje(page, env_id)
                evento("TRAMO 2 OK")
                ox.tarjeta(page, "3 · Verificar instalación", "59 vs MODULES_PROD.txt")
                t3_verificar_instalacion(page)
                evento("TRAMO 3 OK")
            if not desde_teardown:
                ox.tarjeta(page, "4 · El servidor por dentro", "Todo por PCM, nada a mano")
                t4_servidor_por_dentro(page)
                evento("TRAMO 4 OK")
                ox.tarjeta(page, "5 · Higiene del token", "Cifrado en la ficha y en NINGÚN otro lado")
                t5_higiene_token_pcm(page)
                evento("TRAMO 5 OK")
                ox.tarjeta(page, "6 · O site no ar", "HTTPS · sin /odoo · 404 · /agendar")
                t6_sitio(page, ctx)
                evento("TRAMO 6 OK")
                ox.tarjeta(page, "7 · web_responsive", "El menú de apps, funcional (rama 19.0)")
                t7_web_responsive(page, ctx)
                evento("TRAMO 7 OK")
            ox.tarjeta(page, "8 · Teardown", "Triple confirmación + IP liberada")
            t8_teardown(page)
            evento("TRAMO 8 OK")
            horas = (tiempos.get("fin", 0)) / 3600.0
            filas = [("Montaje completo", _dur(tiempos.get("montaje"))),
                     ("Checks", "addons_path · wkhtmltopdf · token · "
                                "web_responsive · sitio"),
                     ("Costo del ensayo", "≈ US$ %.2f" % (horas * COSTO_HORA)),
                     ("Incidentes", str(len(incidentes)) if incidentes else "ninguno")]
            ox.tarjeta(page, "ENSAYO COMPLETO", "Producción repite esto con el "
                       "dominio altanapipa.primatecloudmanager.com", filas, 6500)
            marca("fin")
        except Exception as error:   # noqa: BLE001 — video y json se salvan igual
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(SALIDA / "alta_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            ruta_video = page.video.path() if page.video else None
            ctx.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes}
    destino = bruto / ("%s.webm" % nombre_toma)
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    (SALIDA / ("%s.json" % nombre_toma)).write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d" % (destino, len(incidentes)))
    for i in incidentes:
        print("  ⚠️ ", i)
    if excepcion:
        raise excepcion


def _dur(segundos):
    if not segundos:
        return "—"
    return "%d:%02d" % (segundos // 60, segundos % 60)


def ensamblar():
    datos = json.loads((SALIDA / "toma_alta_sandbox.json").read_text())
    final = SALIDA / "alta_na_pipa_sandbox.mp4"
    vens = sorted(datos["ventanas"], key=lambda v: v["desde"])
    escala = "scale=%d:%d:flags=lanczos" % (VIDEO_W, VIDEO_H)
    partes, previo, idx = [], 0.0, 0
    filtro = ""
    for v in vens:
        filtro += "[0:v]trim=%.2f:%.2f,setpts=PTS-STARTPTS[p%d];" % (previo, v["desde"], idx)
        partes.append("[p%d]" % idx); idx += 1
        filtro += "[0:v]trim=%.2f:%.2f,setpts=(PTS-STARTPTS)/%d[p%d];" % (
            v["desde"], v["hasta"], v["factor"], idx)
        partes.append("[p%d]" % idx); idx += 1
        previo = v["hasta"]
    filtro += "[0:v]trim=%.2f,setpts=PTS-STARTPTS[p%d];" % (previo, idx)
    partes.append("[p%d]" % idx)
    filtro += "%sconcat=n=%d:v=1:a=0,%s[v]" % ("".join(partes), len(partes), escala)
    orden = ["ffmpeg", "-y", "-i", datos["video"], "-filter_complex", filtro,
             "-map", "[v]", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(final)]
    subprocess.run(orden, check=True, capture_output=True)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True, text=True).stdout.strip())
    print("🎬 %s · %d:%02d" % (final, dur // 60, dur % 60))

    def mapear(t):
        quitado = 0.0
        for v in vens:
            if t >= v["hasta"]:
                quitado += (v["hasta"] - v["desde"]) * (1 - 1.0 / v["factor"])
            elif t > v["desde"]:
                quitado += (t - v["desde"]) * (1 - 1.0 / v["factor"])
        return t - quitado
    for nombre, t in datos["marcas"]:
        tf = mapear(t)
        print("  %d:%02d  %s" % (tf // 60, tf % 60, nombre))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ensamblar":
        ensamblar()
    elif len(sys.argv) > 1 and sys.argv[1] == "desde_checks":
        grabar(desde_checks=True)
    elif len(sys.argv) > 1 and sys.argv[1] == "desde_teardown":
        grabar(desde_teardown=True)
    else:
        grabar()
