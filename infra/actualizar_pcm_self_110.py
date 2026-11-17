# -*- coding: utf-8 -*-
"""Actualiza pcm-self a 19.0.1.10.0 con el servicio DETENIDO (detached).

  .venv/bin/python infra/actualizar_pcm_self_110.py

Dispara por SSM una unit transitoria: stop odoo-pcm → -u (log propio) →
start. El comando vuelve al instante; PCM queda abajo ~1-2 min. El driver
espera la vuelta y VERIFICA la versión instalada. OK explícito de Daryl.
"""
import json
import re
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = "i-0b257f2e063135df3"
CTX = None

DETACHED = ("sudo systemd-run --collect --on-active=5 bash -c "
            "'systemctl stop odoo-pcm; sudo -u odoo "
            "/opt/pcm/runtime/odoo-19/venv/bin/python3 "
            "/opt/pcm/runtime/odoo-19/src/odoo-bin -c /etc/odoo/pcm.conf "
            "-d pcm_produccion -u primate_cloud_manager --no-http "
            "--stop-after-init --logfile=/tmp/pcm_u110.log; "
            "systemctl start odoo-pcm' && echo PCM_U_PROGRAMADO")


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=240000)
    d = r.json()
    assert "error" not in d, json.dumps(d["error"])[:300]
    return d.get("result")


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
            t = m.group(1) if m else cuerpo
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                t = t.replace(a, b)
            return ("Success" in cuerpo), t
        time.sleep(4)
    return False, "(sin salida)"


def curl(url):
    out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w",
                          "%{http_code}", "--max-time", "10", url],
                         capture_output=True, text=True)
    return out.stdout.strip()


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("U110: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("U110: sesión activa", flush=True)
    mid = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id"]],
              {"context": {}})[0]["id"]
    ok, salida = ssm(mid, DETACHED)
    assert ok and "PCM_U_PROGRAMADO" in salida, "no se programó: %s" % salida
    print("U110: programado — PCM se apaga en ~5 s para el -u (1-2 min)…",
          flush=True)
    # Esperar la caída y la vuelta.
    time.sleep(20)
    t0 = time.time()
    while time.time() - t0 < 600:
        if curl(BASE + "/web/login") == "200":
            break
        time.sleep(10)
    else:
        raise RuntimeError("PCM no volvió en 10 min — revisar /tmp/pcm_u110.log")
    print("U110: PCM de vuelta (login 200) — verificando versión…", flush=True)
    version = None
    for _ in range(30):
        try:
            fila = orm("ir.module.module", "search_read",
                       [[["name", "=", "primate_cloud_manager"]],
                        ["latest_version"]], {"context": {}})
            version = fila[0]["latest_version"]
            break
        except Exception:
            time.sleep(10)
    print("U110: versión instalada: %s" % version, flush=True)
    if version != "19.0.1.10.0":
        ok, salida = ssm(mid, "sudo tail -n 40 /tmp/pcm_u110.log")
        print("U110_FRENO: no quedó en 1.10.0; log:\n%s" % salida[-1500:],
              flush=True)
        raise SystemExit(1)
    CTX.close()
    nav.close()
print("U110: FIN — 19.0.1.10.0 instalada y PCM arriba", flush=True)
