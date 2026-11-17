# -*- coding: utf-8 -*-
"""Restablece la contraseña del admin del Odoo de Alta na Pipa (pedido de
Daryl 2026-11-17) y VERIFICA el login contra el sitio real.

  .venv/bin/python infra/reset_pass_alta.py

Vía PCM (sesión de Daryl + «Ejecutar comando»). La contraseña viaja en
base64 dentro del comando (no queda en texto plano en chatter/SSM) y el
write pasa por el ORM (hash correcto de Odoo).
"""
import base64
import json
import re
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
ALTA = "https://altanapipa.primatecloudmanager.com"
ENV_NOMBRE = "altanapipa"
PASS_B64 = base64.b64encode(b"admin12345").decode()
CTX = None


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=240000)
    datos = r.json()
    assert "error" not in datos, json.dumps(datos["error"])[:300]
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
            return ("Success" in cuerpo), (m.group(1) if m else cuerpo)
        time.sleep(4)
    raise RuntimeError("sin salida")


COMANDO = r"""export PCM_NP=$(printf '%s' @B64@ | base64 -d)
sudo -u odoo -E env PCM_NP="$PCM_NP" \
  /opt/pcm/runtime/odoo-19/venv/bin/python3 \
  /opt/pcm/runtime/odoo-19/src/odoo-bin shell \
  -c /etc/odoo/altanapipa.conf --no-http <<'PY'
import os
u = env["res.users"].search([("login", "=", "admin")])
assert len(u) == 1, "admin no es único: %s" % u
u.write({"password": os.environ["PCM_NP"]})
env.cr.commit()
print("PCM_PASS_OK", u.login)
PY""".replace("@B64@", PASS_B64)


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_PASS: logueate (2FA)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_PASS: sesión activa", flush=True)
    envs = orm("primate.cloud.environment", "search_read",
               [[["name", "=", ENV_NOMBRE]], ["id"]], {"context": {}})
    maq = orm("primate.cloud.ec2.instance", "search_read",
              [[["environment_id", "=", envs[0]["id"]],
                ["instance_state", "=", "running"]], ["id"]], {"context": {}})
    ok, salida = ssm(maq[0]["id"], COMANDO)
    assert ok and "PCM_PASS_OK" in salida, "el reset falló: %s" % salida[-300:]
    print("PCM_PASS: contraseña restablecida ✓", flush=True)
    # Verificación REAL: login contra el Odoo de Alta con la nueva.
    alta = CTX.new_page()
    alta.goto(ALTA + "/web/login", timeout=60000)
    alta.fill("form:has(input[name=password]) input[name=login]", "admin")
    alta.fill("form input[name=password]", "admin12345")
    alta.click("form:has(input[name=password]) button[type=submit]")
    alta.wait_for_url("**/odoo**", timeout=60000)
    print("PCM_PASS: login verificado en %s ✓" % ALTA, flush=True)
    alta.close()
    CTX.close()
    nav.close()
print("PCM_PASS: FIN", flush=True)
