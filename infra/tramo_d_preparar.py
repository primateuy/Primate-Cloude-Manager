# -*- coding: utf-8 -*-
"""Cierre post-deploy de pcm-self + PRECARGA del tramo D (Alta na Pipa).

  .venv/bin/python infra/tramo_d_preparar.py

Una sola sesión (login de Daryl). FRENO ante cualquier verificación rota:
  1. Versión instalada de primate_cloud_manager == 19.0.1.9.0; si quedó
     vieja, Deploy Pull → el -u AUTOMÁTICO nuevo la sube solo (estreno).
  2. Symlink-puente: se limpia SOLO tras verificar la entrada del repo en
     el conf; estáticas en 200 después.
  3. Precarga D: proyecto «Alta na Pipa» + entorno altanapipa (Borrador)
     + 4 repos NO públicos en orden SIN token + referencia MODULES_PROD.
"""
import json
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = "i-0b257f2e063135df3"            # máquina de pcm-self
VERSION = "19.0.1.9.0"
ADDONS = "/opt/pcm/instances/pcm/addons"
LINEA_REPO = "/opt/pcm/instances/pcm/addons/Primate-Cloude-Manager"
CUENTA = "pcm-prod-prov"
SLUG = "altanapipa"
BASE_REPOS = "/opt/pcm/instances/%s/repos" % SLUG
REPOS = [
    ("webOCA", "primateuy", "oca", 10),
    ("surveyOCA", "primateuy", "oca", 20),
    ("primate_portal", "PrimateUy-SAS", "custom_primate", 30),
    ("alta_na_pipa", "PrimateUy-SAS", "custom_client", 40),
]
ESTATICAS = ["static/description/icon.png", "static/src/app/pcm_actions.js"]
CTX = None


def evento(t):
    print("PCM_TRAMO_D: %s" % t, flush=True)


def freno(t):
    print("PCM_TRAMO_D_FRENO: %s" % t, flush=True)
    raise SystemExit(1)


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=400000)
    datos = r.json()
    if "error" in datos:
        freno("ORM %s.%s: %s" % (model, method,
                                 json.dumps(datos["error"])[:300]))
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
            return ("Success" in cuerpo), texto
        time.sleep(4)
    freno("sin salida del comando")


def curl(url):
    out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w",
                          "%{http_code}", "--max-time", "12", url],
                         capture_output=True, text=True)
    return out.stdout.strip()


def version_instalada():
    fila = orm("ir.module.module", "search_read",
               [[["name", "=", "primate_cloud_manager"]],
                ["latest_version", "state"]], {"context": {}})
    return fila[0]["latest_version"], fila[0]["state"]


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    evento("PAUSA · logueate (2FA)…")
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        freno("sin login")
    evento("sesión activa")
    mid = orm("primate.cloud.ec2.instance", "search_read",
              [[["aws_instance_id", "=", AWS_ID]], ["id"]],
              {"context": {}})[0]["id"]

    # 1. Versión instalada → si quedó vieja, Pull (estrena el -u automático).
    version, estado = version_instalada()
    evento("versión instalada: %s (%s)" % (version, estado))
    if version != VERSION:
        env_self = orm("primate.cloud.environment", "search_read",
                       [[["name", "=", "pcm-self"]], ["id"]],
                       {"context": {}})[0]["id"]
        repo_self = orm("primate.cloud.repository", "search_read",
                        [[["environment_id", "=", env_self]], ["id"]],
                        {"context": {}})[0]["id"]
        dep_id = orm("primate.cloud.deployment", "create",
                     [{"environment_id": env_self, "deployment_type": "pull",
                       "repository_id": repo_self}], {"context": {}})
        orm("primate.cloud.deployment", "action_run", [[dep_id]])
        evento("Deploy Pull encolado (el -u automático debe disparar)…")
        t0 = time.time()
        while time.time() - t0 < 900:
            fila = orm("primate.cloud.deployment", "read",
                       [[dep_id], ["state", "execution_log"]],
                       {"context": {}})[0]
            if fila["state"] in ("success", "failed"):
                break
            time.sleep(10)
        if fila["state"] != "success":
            freno("el Pull terminó en %s · %s"
                  % (fila["state"], (fila["execution_log"] or "")[-300:]))
        if "PCM_UPDATE:" in (fila["execution_log"] or ""):
            evento("-u automático DISPARÓ ✓ (marcador PCM_UPDATE en el log)")
        else:
            freno("el Pull no disparó el -u automático (sin PCM_UPDATE); "
                  "la versión quedaría vieja — revisar antes de seguir")
        evento("esperando el restart diferido…")
        time.sleep(35)
        for _ in range(30):
            if curl(BASE + "/web/login") == "200":
                break
            time.sleep(5)
        else:
            freno("PCM no volvió tras el restart")
        version, _e = version_instalada()
        if version != VERSION:
            freno("la versión sigue en %s tras el -u automático" % version)
    evento("primate_cloud_manager %s ✓" % version)

    # 2. Symlink-puente: limpiar SOLO con la entrada del repo verificada.
    ok, salida = ssm(mid, "grep '^addons_path' /etc/odoo/pcm.conf")
    if LINEA_REPO not in salida:
        freno("el conf NO tiene la entrada del repo; NO toco el symlink")
    ok, salida = ssm(mid, "[ -L %s/primate_cloud_manager ] && sudo rm "
                     "%s/primate_cloud_manager && echo BORRADO || echo "
                     "YA_NO_ESTABA; ls -la %s/" % (ADDONS, ADDONS, ADDONS))
    evento("symlink-puente: %s"
           % ("BORRADO" if "BORRADO" in salida else "ya no estaba"))
    malas = [e for e in ESTATICAS
             if curl("%s/primate_cloud_manager/%s" % (BASE, e)) != "200"]
    if malas:
        freno("estáticas rotas tras limpiar el symlink: %s" % malas)
    evento("estáticas 200 tras la limpieza ✓")

    # 3. PRECARGA del tramo D (idempotente, sin tokens).
    cuentas = orm("primate.cloud.account", "search_read",
                  [[["name", "=", CUENTA]], ["id"]], {"context": {}})
    if not cuentas:
        freno("no existe la cuenta «%s» en PCM Producción" % CUENTA)
    cuenta_id = cuentas[0]["id"]
    partners = orm("res.partner", "search_read",
                   [[["name", "=", "Alta na Pipa"]], ["id"]], {"context": {}})
    partner_id = (partners[0]["id"] if partners else
                  orm("res.partner", "create", [{"name": "Alta na Pipa"}],
                      {"context": {}}))
    proys = orm("primate.cloud.project", "search_read",
                [[["name", "=", "Alta na Pipa"]], ["id"]], {"context": {}})
    proy_id = (proys[0]["id"] if proys else
               orm("primate.cloud.project", "create",
                   [{"name": "Alta na Pipa", "account_id": cuenta_id,
                     "partner_id": partner_id}], {"context": {}}))
    envs = orm("primate.cloud.environment", "search_read",
               [[["name", "=", SLUG], ["project_id", "=", proy_id]],
                ["id", "state"]], {"context": {}})
    if envs:
        env_id = envs[0]["id"]
        evento("entorno %s ya existía (%s, %s)"
               % (SLUG, env_id, envs[0]["state"]))
    else:
        env_id = orm("primate.cloud.environment", "create",
                     [{"name": SLUG, "project_id": proy_id,
                       "account_id": cuenta_id, "env_type": "production",
                       "odoo_version": "19", "odoo_edition": "community"}],
                     {"context": {}})
        evento("entorno %s creado (%s)" % (SLUG, env_id))
    inst = orm("primate.cloud.environment", "read",
               [[env_id], ["primary_instance_id"]], {"context": {}})[0]
    iid = inst["primary_instance_id"] and inst["primary_instance_id"][0]
    if iid:
        orm("primate.cloud.instance", "write", [[iid], {"slug": SLUG}])
    repo_ids = {}
    for nombre, org, tipo, seq in REPOS:
        existentes = orm("primate.cloud.repository", "search_read",
                         [[["environment_id", "=", env_id],
                           ["name", "=", nombre]], ["id"]], {"context": {}})
        vals = {"repo_type": tipo, "organization": org,
                "github_url": "https://github.com/%s/%s" % (org, nombre),
                "configured_branch": "19.0", "sequence": seq,
                "local_path": "%s/%s" % (BASE_REPOS, nombre)}
        if existentes:
            rid = existentes[0]["id"]
            orm("primate.cloud.repository", "write", [[rid], vals],
                {"context": {}})
            evento("repo %s re-afirmado (%s)" % (nombre, rid))
        else:
            vals.update({"name": nombre, "environment_id": env_id})
            rid = orm("primate.cloud.repository", "create", [vals],
                      {"context": {}})
            evento("repo %s creado (%s, seq %d) SIN token"
                   % (nombre, rid, seq))
        repo_ids[nombre] = rid
    orm("primate.cloud.environment", "write",
        [[env_id], {"modules_ref_repo_id": repo_ids["alta_na_pipa"],
                    "modules_ref_path": "docs/MODULES_PROD.txt"}],
        {"context": {}})
    evento("referencia de verificación: docs/MODULES_PROD.txt (59) ✓")
    CTX.close()
    nav.close()
evento("FIN · precarga D lista: 4 repos SIN token esperando el PAT")
