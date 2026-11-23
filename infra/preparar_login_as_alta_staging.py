# -*- coding: utf-8 -*-
"""Despliega y habilita el companion de impersonación en el STAGING de Alta.

  .venv/bin/python infra/preparar_login_as_alta_staging.py

Igual que preparar_login_as_alta.py pero sobre la instancia staging
(slug *staging* del servidor de alta): deploy_impersonate manda el addon al
addons_dir DE LA INSTANCIA STAGING, instala con esa unit detenida y escribe
su pubkey; set_impersonate_enabled la habilita. Producción no se toca.
Verifica que /pcm/impersonate del staging deje de dar 404.
"""
import json
import subprocess
import time

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


def estado_ruta():
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "12", STG + "/pcm/impersonate?token=x"],
        capture_output=True, text=True)
    return out.stdout.strip()


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_LOGINAS_STG: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_LOGINAS_STG: sesión activa", flush=True)
    inst = orm("primate.cloud.instance", "search_read",
               [[["slug", "like", "staging"]], ["id", "name", "slug"]],
               {"context": {}})
    # FRENO si no es exactamente una instancia staging de alta.
    assert len(inst) == 1, "esperaba UNA instancia staging, hay %d: %s" % (
        len(inst), json.dumps(inst))
    iid, slug = inst[0]["id"], inst[0]["slug"]
    print("PCM_LOGINAS_STG: instancia %s (%s)" % (inst[0]["name"], slug),
          flush=True)
    print("PCM_LOGINAS_STG: desplegando companion (detiene y levanta SOLO "
          "la unit del staging; ~2-4 min)…", flush=True)
    orm("primate.cloud.instance", "deploy_impersonate",
        [[iid], [DB]], {"context": {}})
    print("PCM_LOGINAS_STG: companion desplegado ✓", flush=True)
    orm("primate.cloud.instance", "set_impersonate_enabled",
        [[iid], [DB], True], {"context": {}})
    print("PCM_LOGINAS_STG: impersonación HABILITADA ✓", flush=True)
    for _ in range(24):
        codigo = estado_ruta()
        if codigo != "404":
            print("PCM_LOGINAS_STG: /pcm/impersonate responde %s (ya no 404) ✓"
                  % codigo, flush=True)
            break
        time.sleep(5)
    else:
        raise RuntimeError("/pcm/impersonate del staging sigue en 404")
    CTX.close()
    nav.close()
print("PCM_LOGINAS_STG: FIN — probá «Iniciar sesión como…» en el staging",
      flush=True)
