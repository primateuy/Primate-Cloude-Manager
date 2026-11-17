# -*- coding: utf-8 -*-
"""Lee el log del -u automático fallido en pcm-self (solo lectura).

  .venv/bin/python infra/leer_log_u.py
"""
import json
import re
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
    print("PCM_LOG_U: logueate (2FA)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PCM_LOG_U: sesión activa", flush=True)
    mid = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id"]],
              {"context": {}})[0]["id"]
    print("### errores recientes del log de la instancia:", flush=True)
    print(ssm(mid, "sudo tail -n 400 /opt/pcm/instances/pcm/log/odoo.log | "
                   "grep -B2 -A30 -E 'Traceback|CRITICAL' | tail -n 100"),
          flush=True)
    print("### instancia: binarios que usa el deploy:", flush=True)
    inst = orm("primate.cloud.instance", "search_read",
               [[["slug", "=", "pcm"]],
                ["python_bin", "odoo_bin", "conf_path", "service_name"]],
               {"context": {}})
    print(json.dumps(inst, indent=1), flush=True)
    CTX.close()
    nav.close()
print("PCM_LOG_U: FIN", flush=True)
