# -*- coding: utf-8 -*-
"""REPARACIÓN de pcm-self (symlink + conf + restart). Vía ORM de la sesión:
funciona aunque la UI esté rota — solo requiere pasar el login.

  .venv/bin/python infra/reparar_pcm_self.py

Pasos (freno ante cualquier verificación que no cierre):
  1. Recrear el symlink del módulo dentro del clon (idempotente).
  2. Escribir la entrada del repo en el addons_path (sed con backup
     .bak-sanado) y VERIFICAR la línea exacta antes de seguir.
  3. Reinicio diferido (systemd-run +10 s).
  4. Verificar: login 200, estáticas del módulo 200, Entornos responde.
"""
import json
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = "i-0b257f2e063135df3"
ADDONS = "/opt/pcm/instances/pcm/addons"
MODULO = ADDONS + "/Primate-Cloude-Manager/primate_cloud_manager"
LINEA = ("addons_path = /opt/pcm/runtime/odoo-19/src/addons,"
         "/opt/pcm/instances/pcm/addons/Primate-Cloude-Manager,"
         "/opt/pcm/instances/pcm/addons")
ESTATICAS = ["static/description/icon.png", "static/src/app/pcm_actions.js"]
CTX = None


def evento(t):
    print("PCM_REPARAR: %s" % t, flush=True)


def freno(t):
    print("PCM_REPARAR_FRENO: %s" % t, flush=True)
    raise SystemExit(1)


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=240000)
    datos = r.json()
    if "error" in datos:
        freno("ORM %s.%s: %s" % (model, method,
                                 json.dumps(datos["error"])[:300]))
    return datos["result"]


def ssm(mid, comando):
    previo = orm("mail.message", "search_read",
                 [[["model", "=", "primate.cloud.ec2.instance"],
                   ["res_id", "=", mid]], ["id"]],
                 {"limit": 1, "order": "id desc", "context": {}})
    previo = previo[0]["id"] if previo else 0
    orm("primate.cloud.ec2.instance", "job_execute_command", [[mid], comando])
    for _ in range(40):
        filas = orm("mail.message", "search_read",
                    [[["model", "=", "primate.cloud.ec2.instance"],
                      ["res_id", "=", mid], ["id", ">", previo]], ["body"]],
                    {"limit": 1, "order": "id desc", "context": {}})
        if filas:
            cuerpo = filas[0]["body"] or ""
            m = re.search(r"<pre>(.*?)</pre>", cuerpo, re.S)
            texto = m.group(1) if m else cuerpo
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                texto = texto.replace(a, b)
            return ("Success" in cuerpo), texto
        time.sleep(4)
    freno("sin salida del comando")


def curl(url):
    out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w",
                          "%{http_code}", "--max-time", "12", url],
                         capture_output=True, text=True)
    return out.stdout.strip()


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    evento("PAUSA · logueate (2FA)…")
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        freno("sin login")
    evento("sesión activa")
    mid = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id"]],
              {"context": {}})[0]["id"]

    # 1. Symlink (idempotente: -f no, mejor crear solo si falta).
    ok, salida = ssm(mid, "[ -L %s/primate_cloud_manager ] || sudo ln -s %s "
                     "%s/primate_cloud_manager; readlink -f "
                     "%s/primate_cloud_manager"
                     % (ADDONS, MODULO, ADDONS, ADDONS))
    if MODULO not in salida:
        freno("el symlink no quedó apuntando a %s · %s" % (MODULO, salida))
    evento("symlink OK ✓")

    # 2. Conf con la entrada del repo + verificación de la línea EXACTA.
    ok, salida = ssm(mid, "sudo sed -i.bak-sanado 's|^addons_path = .*|%s|' "
                     "/etc/odoo/pcm.conf && grep '^addons_path' "
                     "/etc/odoo/pcm.conf" % LINEA)
    if LINEA not in salida:
        freno("el conf NO quedó con la línea esperada:\n%s" % salida)
    evento("conf OK ✓ (backup .bak-sanado)")

    # 3. Reinicio diferido.
    ok, _s = ssm(mid, "sudo systemd-run --collect --on-active=10 "
                      "systemctl restart odoo-pcm && echo PROGRAMADO")
    if not ok:
        freno("no pude programar el restart")
    evento("restart programado · esperando…")
    time.sleep(35)

    # 4. Verificación.
    for _ in range(30):
        if curl(BASE + "/web/login") == "200":
            break
        time.sleep(5)
    else:
        freno("PCM no volvió tras el restart")
    evento("login 200 ✓")
    pendientes = list(ESTATICAS)
    t0 = time.time()
    while pendientes and time.time() - t0 < 180:
        pendientes = [e for e in pendientes
                      if curl("%s/primate_cloud_manager/%s" % (BASE, e)) != "200"]
        if pendientes:
            time.sleep(8)
    if pendientes:
        freno("estáticas todavía rotas: %s" % pendientes)
    evento("estáticas del módulo 200 ✓")
    envs = orm("primate.cloud.environment", "search_read",
               [[["name", "=", "pcm-self"]], ["id"]], {"context": {}})
    datos = orm("primate.cloud.dashboard", "get_environment_detail",
                [envs[0]["id"]], {"context": {}})
    evento("Entornos responde (%s, %d repos) ✓"
           % (datos.get("name"), len(datos.get("repositories", []))))
    CTX.close()
    nav.close()
evento("FIN · PCM reparado")
