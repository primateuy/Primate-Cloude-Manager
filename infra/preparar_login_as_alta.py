# -*- coding: utf-8 -*-
"""Despliega y habilita el companion de impersonación en Alta na Pipa.

  .venv/bin/python infra/preparar_login_as_alta.py

Vía ORM de la sesión (métodos públicos del modelo): deploy_impersonate
(manda el addon, instala con la instancia detenida y --no-http, escribe la
pubkey) + set_impersonate_enabled. Verifica que /pcm/impersonate deje de
dar 404.
"""
import json
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
ALTA = "https://altanapipa.primatecloudmanager.com"
CTX = None


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=600000)
    d = r.json()
    assert "error" not in d, json.dumps(d["error"])[:400]
    return d["result"]


def estado_ruta():
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "12", ALTA + "/pcm/impersonate?token=x"],
        capture_output=True, text=True)
    return out.stdout.strip()


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_LOGINAS: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_LOGINAS: sesión activa", flush=True)
    inst = orm("primate.cloud.instance", "search_read",
               [[["slug", "=", "altanapipa"]], ["id", "name"]], {"context": {}})
    iid = inst[0]["id"]
    print("PCM_LOGINAS: desplegando companion (instala con la instancia "
          "detenida; ~2-4 min)…", flush=True)
    orm("primate.cloud.instance", "deploy_impersonate",
        [[iid], ["altanapipa"]], {"context": {}})
    print("PCM_LOGINAS: companion desplegado ✓", flush=True)
    orm("primate.cloud.instance", "set_impersonate_enabled",
        [[iid], ["altanapipa"], True], {"context": {}})
    print("PCM_LOGINAS: impersonación HABILITADA ✓", flush=True)
    for _ in range(24):
        código = estado_ruta()
        if código != "404":
            print("PCM_LOGINAS: /pcm/impersonate responde %s (ya no 404) ✓"
                  % código, flush=True)
            break
        time.sleep(5)
    else:
        raise RuntimeError("/pcm/impersonate sigue en 404")
    CTX.close()
    nav.close()
print("PCM_LOGINAS: FIN — probá «Iniciar sesión como…» de nuevo", flush=True)
