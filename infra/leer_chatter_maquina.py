# -*- coding: utf-8 -*-
"""Lee los últimos mensajes del chatter de una máquina en PCM Producción.

  .venv/bin/python infra/leer_chatter_maquina.py [aws_instance_id] [n]

Solo lectura, vía la sesión del navegador (Daryl se loguea en la ventana).
Default: la máquina de pcm-self (i-0b257f2e063135df3), últimos 3 mensajes.
"""
import html
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = sys.argv[1] if len(sys.argv) > 1 else "i-0b257f2e063135df3"
CUANTOS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

CTX = None


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=120000)
    datos = r.json()
    assert "error" not in datos, json.dumps(datos["error"])[:400]
    return datos["result"]


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_LECTOR: logueate en la ventana (2FA)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login en 20 min")
    print("PCM_LECTOR: sesión activa", flush=True)
    maq = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id", "name"]],
              {"context": {}})
    assert maq, "máquina %s no encontrada" % AWS_ID
    filas = orm("mail.message", "search_read",
                [[["model", "=", "primate.cloud.ec2.instance"],
                  ["res_id", "=", maq[0]["id"]]], ["id", "date", "body"]],
                {"limit": CUANTOS, "order": "id desc", "context": {}})
    for f in reversed(filas):
        texto = html.unescape(re.sub(r"<[^>]+>", "\n", f["body"] or ""))
        texto = re.sub(r"\n{2,}", "\n", texto).strip()
        print("\n===== msg %s · %s =====\n%s" % (f["id"], f["date"], texto),
              flush=True)
    CTX.close()
    nav.close()
print("PCM_LECTOR: FIN", flush=True)
