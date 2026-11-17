# -*- coding: utf-8 -*-
"""PASO 0 — backup de Alta: verificar el manual, crear/asignar la política
diaria, verificar el cron; + lecturas (nonce del companion, CWAgent).

  .venv/bin/python infra/paso0_backup_alta.py

Producción: SOLO lectura salvo lo explícitamente pedido (crear la política
«Alta na Pipa diario» copiada de «PCM self diario» y asignarla).
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
    assert "error" not in d, json.dumps(d["error"])[:400]
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


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("PASO0: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("PASO0: sesión activa", flush=True)

    env = orm("primate.cloud.environment", "search_read",
              [[["name", "=", "altanapipa"]],
               ["id", "primary_instance_id"]], {"context": {}})[0]
    maq = orm("primate.cloud.ec2.instance", "search_read",
              [[["environment_id", "=", env["id"]]], ["id"]],
              {"context": {}})[0]

    # 1. El backup manual: estado + objeto en S3.
    bks = orm("primate.cloud.backup", "search_read",
              [[["environment_id", "=", env["id"]]],
               ["id", "state", "create_date", "s3_key", "s3_bucket",
                "size_mb"]],
              {"limit": 3, "order": "id desc", "context": {}})
    print("### BACKUPS:", json.dumps(bks), flush=True)
    if bks and bks[0]["state"] == "completed" and bks[0].get("s3_key"):
        ok, salida = ssm(maq["id"],
                         "aws s3api head-object --bucket %s --key %s "
                         "--region us-east-1 --output json | head -8"
                         % (bks[0].get("s3_bucket")
                            or "pcm-backups-512060111009", bks[0]["s3_key"]))
        print("### HEAD S3 (%s): %s" % ("OK" if ok else "FALLO",
                                        salida[:400]), flush=True)
    else:
        print("### BACKUP MANUAL NO COMPLETED — FRENO", flush=True)

    # 2. Política: copiar «PCM self diario» → «Alta na Pipa diario» + asignar.
    base_pol = orm("primate.cloud.backup.policy", "search_read",
                   [[["name", "=", "PCM self diario"]], []], {"context": {}})[0]
    print("### POLITICA BASE:", json.dumps(
        {k: base_pol[k] for k in ("policy_type", "expected_frequency",
                                  "expected_retention_days", "s3_bucket",
                                  "s3_prefix", "execution_hour",
                                  "managed_by_pcm") if k in base_pol}),
        flush=True)
    ya = orm("primate.cloud.backup.policy", "search_read",
             [[["name", "=", "Alta na Pipa diario"]], ["id"]], {"context": {}})
    if ya:
        pol_id = ya[0]["id"]
        print("### política ya existía (%s)" % pol_id, flush=True)
    else:
        vals = {k: base_pol[k] for k in (
            "policy_type", "expected_frequency", "expected_retention_days",
            "s3_bucket", "s3_prefix", "execution_hour", "managed_by_pcm")
            if k in base_pol}
        vals["name"] = "Alta na Pipa diario"
        pol_id = orm("primate.cloud.backup.policy", "create", [vals],
                     {"context": {}})
        print("### política creada (%s)" % pol_id, flush=True)
    orm("primate.cloud.instance", "write",
        [[env["primary_instance_id"][0]], {"backup_policy_id": pol_id}],
        {"context": {}})
    chk = orm("primate.cloud.environment", "read",
              [[env["id"]], ["backup_policy_id", "backup_compliance"]],
              {"context": {}})[0]
    print("### ASIGNADA:", json.dumps(chk), flush=True)

    # 3. El cron nocturno.
    cron = orm("ir.cron", "search_read",
               [[["cron_name", "ilike", "backup"]],
                ["cron_name", "active", "nextcall", "interval_number",
                 "interval_type"]], {"context": {}})
    print("### CRON:", json.dumps(cron), flush=True)

    # 4. Nonce del companion en prod (solo SELECT).
    ok, salida = ssm(maq["id"],
                     "sudo -u postgres psql -d altanapipa -At -c \"select "
                     "conname from pg_constraint where conrelid = "
                     "'pcm_impersonation_nonce'::regclass\" ; sudo -u postgres "
                     "psql -d altanapipa -At -c \"select count(*) from ("
                     "select nonce from pcm_impersonation_nonce group by "
                     "nonce having count(*) > 1) d\"")
    print("### NONCE constraints+dups:", salida[:300], flush=True)

    # 5. CWAgent: ¿llegan las métricas? (lectura con el rol de la máquina)
    for nombre, mid in (("alta", maq["id"]),):
        ok, salida = ssm(mid,
                         "aws cloudwatch list-metrics --namespace CWAgent "
                         "--region us-east-1 --output json 2>&1 | head -c 1200")
        print("### CWAgent desde %s:\n%s" % (nombre, salida[:1200]),
              flush=True)
    CTX.close()
    nav.close()
print("PASO0: FIN", flush=True)
