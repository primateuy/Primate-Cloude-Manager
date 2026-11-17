# -*- coding: utf-8 -*-
"""Lecturas de cierre del tramo D + mediciones para el plan de staging.

  .venv/bin/python infra/cierre_alta_lecturas.py

TODO solo lectura: backups/política, métricas CloudWatch, warnings del log
del montaje, RAM/disco/uso real del servidor de Alta.
"""
import json
import re
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
CTX = None


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
            return t
        time.sleep(4)
    return "(sin salida)"


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("LECTURAS: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("LECTURAS: sesión activa", flush=True)
    env = orm("primate.cloud.environment", "search_read",
              [[["name", "=", "altanapipa"]],
               ["id", "cw_agent_state", "backup_compliance"
                ]], {"context": {}})[0]
    print("\n### ENV:", json.dumps(env), flush=True)
    pol = orm("primate.cloud.backup.policy", "search_read",
              [[], ["id", "name", "environment_ids"]], {"context": {}})
    print("### POLITICAS:", json.dumps(pol)[:600], flush=True)
    bks = orm("primate.cloud.backup", "search_read",
              [[["environment_id", "=", env["id"]]],
               ["id", "state", "create_date"]],
              {"limit": 5, "order": "id desc", "context": {}})
    print("### BACKUPS ALTA:", json.dumps(bks), flush=True)
    maq = orm("primate.cloud.ec2.instance", "search_read",
              [[["environment_id", "=", env["id"]]], ["id"]],
              {"context": {}})[0]
    detalle = orm("primate.cloud.dashboard", "get_server_detail",
                  [maq["id"]], {"context": {}})
    print("### METRICAS:", json.dumps(detalle.get("metrics", {}))[:500],
          flush=True)
    print("\n### RAM/DISCO:", flush=True)
    print(ssm(maq["id"], "free -m; echo ---; df -h / /opt; echo ---; "
                         "sudo du -sh /opt/pcm/instances/altanapipa/data "
                         "/var/lib/postgresql /opt/pcm/runtime 2>/dev/null; "
                         "echo ---; ls /etc/nginx/sites-enabled/"), flush=True)
    print("\n### WARNINGS DEL LOG DEL MONTAJE:", flush=True)
    print(ssm(maq["id"], "sudo grep -ciE 'warning' "
                         "/opt/pcm/instances/altanapipa/log/odoo.log; "
                         "sudo grep -iE 'warning|error' "
                         "/opt/pcm/instances/altanapipa/log/odoo.log "
                         "| grep -viE 'werkzeug|longpolling' | sort | uniq -c "
                         "| sort -rn | head -12"), flush=True)
    CTX.close()
    nav.close()
print("LECTURAS: FIN", flush=True)
