# -*- coding: utf-8 -*-
"""Inspección de SOLO LECTURA del estado real de pcm-self (una sesión).

  .venv/bin/python infra/inspeccionar_pcm_self.py

Trae hechos, no toca nada: timestamps del servicio, conf real, symlink,
unidades transitorias de systemd-run, cola de la bitácora de arranques y
los últimos jobs de la cola (vía ORM).
"""
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = "i-0b257f2e063135df3"
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
            texto = m.group(1) if m else cuerpo
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                texto = texto.replace(a, b)
            return texto
        time.sleep(4)
    return "(sin salida)"


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PCM_INSPECT: logueate (2FA)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_INSPECT: sesión activa", flush=True)
    mid = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id"]],
              {"context": {}})[0]["id"]

    print("\n### A. servicio:", flush=True)
    print(ssm(mid, "systemctl is-active odoo-pcm; systemctl show odoo-pcm "
                   "-p ActiveEnterTimestamp -p ExecMainStartTimestamp "
                   "-p MainPID"), flush=True)
    print("\n### B. conf real:", flush=True)
    print(ssm(mid, "grep '^addons_path' /etc/odoo/pcm.conf; "
                   "ls -la /etc/odoo/ | grep pcm"), flush=True)
    print("\n### C. addons dir:", flush=True)
    print(ssm(mid, "ls -la /opt/pcm/instances/pcm/addons/"), flush=True)
    print("\n### D. cmdline del proceso vivo:", flush=True)
    print(ssm(mid, "PID=$(systemctl show -p MainPID --value odoo-pcm); "
                   "tr '\\0' ' ' < /proc/$PID/cmdline; echo; "
                   "stat -c 'arrancado: %y' /proc/$PID"), flush=True)
    print("\n### E. systemd-run pendientes/caidos:", flush=True)
    print(ssm(mid, "systemctl list-units 'run-*' --all --no-legend | tail -6; "
                   "journalctl -u odoo-pcm --since '3 hours ago' --no-pager "
                   "| grep -E 'Start|Stopp|Fail' | tail -10"), flush=True)
    print("\n### F. últimos jobs de la cola (ORM):", flush=True)
    jobs = orm("queue.job", "search_read",
               [[], ["name", "state", "date_created", "exc_info"]],
               {"limit": 6, "order": "id desc", "context": {}})
    for j in jobs:
        print(" - %s · %s · %s" % (j["date_created"], j["state"], j["name"]),
              flush=True)
        if j["state"] == "failed" and j.get("exc_info"):
            print("   exc: %s" % j["exc_info"][-300:].replace("\n", " "),
                  flush=True)
    CTX.close()
    nav.close()
print("PCM_INSPECT: FIN", flush=True)
