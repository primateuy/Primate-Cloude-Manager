# -*- coding: utf-8 -*-
"""Habilita la impersonación en el STAGING de Alta (paso que quedó pendiente).

  .venv/bin/python infra/habilitar_login_as_alta_staging.py

El companion ya está desplegado (verificado: /pcm/impersonate responde 403).
Esto corre SOLO set_impersonate_enabled (llamada corta, no toca la unit) y
re-verifica la ruta.
"""
import json
import subprocess

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
STG = "https://altanapipa-staging.primatecloudmanager.com"
DB = "altanapipa_staging"
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


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_ENABLE_STG: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_ENABLE_STG: sesión activa", flush=True)
    inst = orm("primate.cloud.instance", "search_read",
               [[["slug", "=", "altanapipa-staging"]], ["id", "name"]],
               {"context": {}})
    assert len(inst) == 1, "esperaba la instancia altanapipa-staging: %s" % (
        json.dumps(inst))
    iid = inst[0]["id"]
    orm("primate.cloud.instance", "set_impersonate_enabled",
        [[iid], [DB], True], {"context": {}})
    print("PCM_ENABLE_STG: impersonación HABILITADA ✓", flush=True)
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "12", STG + "/pcm/impersonate?token=x"],
        capture_output=True, text=True)
    print("PCM_ENABLE_STG: /pcm/impersonate responde %s (403 = correcto)"
          % out.stdout.strip(), flush=True)
    CTX.close()
    nav.close()
print("PCM_ENABLE_STG: FIN — probá «Iniciar sesión como…» en el staging",
      flush=True)
