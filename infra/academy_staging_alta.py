# -*- coding: utf-8 -*-
"""Prepara el STAGING de Alta para instalar primate_academy.

  .venv/bin/python infra/academy_staging_alta.py

Por PCM (sesión de Daryl):
  1. Pull del primate_portal DEL STAGING (su clon quedó anterior al módulo).
  2. Agrega OCA/contract (rama 19.0) a LA INSTANCIA staging si no está
     (trae subscription_oca, dependencia de primate_academy).
Después queda el paso manual en el Odoo del staging (Login as admin):
modo dev → Apps → Actualizar lista de aplicaciones → instalar primate_academy.
FRENA ante cualquier estado failed/raro, sin reintentos.
"""
import json
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
CTX = None


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=600000)
    d = r.json()
    assert "error" not in d, json.dumps(d["error"])[:600]
    assert "result" in d, "RPC sin result (¿timeout del proxy?): %s" % (
        json.dumps(d)[:300])
    return d["result"]


def esperar(cond, titulo, intentos=60, pausa=10):
    """Espera cond() truthy; FRENO (RuntimeError) si no llega."""
    for _ in range(intentos):
        v = cond()
        if v:
            return v
        time.sleep(pausa)
    raise RuntimeError("TIMEOUT esperando: %s" % titulo)


with sync_playwright() as pw:
    nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    CTX = nav.new_context(viewport={"width": 1280, "height": 800})
    page = CTX.new_page()
    page.goto(BASE + "/web/login", timeout=90000)
    print("ACADEMY_STG: logueate (2FA — código nuevo)…", flush=True)
    for _ in range(600):
        if "/odoo" in page.url or page.locator(
                ".o_web_client, .o_pcm_app").count():
            break
        page.wait_for_timeout(2000)
    else:
        raise RuntimeError("sin login")
    print("ACADEMY_STG: sesión activa", flush=True)

    inst = orm("primate.cloud.instance", "search_read",
               [[["slug", "=", "altanapipa-staging"]],
                ["id", "name", "environment_id"]], {"context": {}})
    assert len(inst) == 1, "instancia staging no encontrada: %s" % json.dumps(inst)
    iid = inst[0]["id"]
    env_id = inst[0]["environment_id"][0]

    repos = orm("primate.cloud.repository", "search_read",
                [[["instance_id", "=", iid]],
                 ["id", "name", "local_path", "sync_state", "current_commit"]],
                {"context": {}})
    print("ACADEMY_STG: repos del staging: %s" % ", ".join(
        "%s(%s)" % (r["name"], r["sync_state"]) for r in repos), flush=True)
    portal = [r for r in repos if r["name"] == "primate_portal"]
    assert len(portal) == 1, "primate_portal del staging no está: %s" % (
        json.dumps(repos))
    rp = portal[0]

    # --- 1. Pull del primate_portal del staging -----------------------
    dep_id = orm("primate.cloud.deployment", "create",
                 [{"environment_id": env_id, "repository_id": rp["id"],
                   "deployment_type": "pull"}], {"context": {}})
    orm("primate.cloud.deployment", "action_run", [[dep_id]], {"context": {}})
    print("ACADEMY_STG: Pull del primate_portal (staging) encolado…", flush=True)

    def estado_pull():
        d = orm("primate.cloud.deployment", "read",
                [[dep_id], ["state", "target_commit"]], {"context": {}})[0]
        if d["state"] == "failed":
            raise RuntimeError("FRENO: el Pull quedó FALLIDO — mirá el log "
                               "del deploy en el panel.")
        return d if d["state"] == "success" else None

    d = esperar(estado_pull, "Pull exitoso", intentos=60, pausa=10)
    print("ACADEMY_STG: Pull OK → commit %s" % (d.get("target_commit") or "?"),
          flush=True)

    # --- 2. OCA/contract en la instancia staging ----------------------
    contract = [r for r in repos if r["name"] == "contract"]
    if contract and contract[0]["sync_state"] == "updated":
        print("ACADEMY_STG: contract ya está en el staging (Actualizado) — "
              "no se agrega de nuevo", flush=True)
    elif contract:
        raise RuntimeError("FRENO: contract ya existe en el staging con "
                           "estado «%s» — revisá su log antes de reintentar."
                           % contract[0]["sync_state"])
    else:
        rid = orm("primate.cloud.environment", "action_add_addon_ui",
                  [[env_id], {"github_url": "https://github.com/OCA/contract",
                              "configured_branch": "19.0",
                              "repo_type": "oca",
                              "instance_id": iid}], {"context": {}})
        print("ACADEMY_STG: alta de contract encolada (clona + addons_path + "
              "reinicio del staging; puede tardar varios min)…", flush=True)

        def estado_contract():
            rs = orm("primate.cloud.repository", "search_read",
                     [[["id", "=", rid]], ["sync_state"]], {"context": {}})
            if not rs:
                raise RuntimeError("FRENO: el registro de contract se borró "
                                   "→ el clone FALLÓ (ver bitácora del "
                                   "entorno).")
            return rs[0] if rs[0]["sync_state"] == "updated" else None

        esperar(estado_contract, "contract clonado y verificado",
                intentos=90, pausa=10)
        print("ACADEMY_STG: contract agregado y verificado ✓", flush=True)

    # --- 3. Verificación final: el módulo está en el clon del staging --
    orm("primate.cloud.repository", "action_detect_modules", [[rp["id"]]],
        {"context": {}})

    def academy_detectado():
        mods = orm("primate.cloud.module", "search_read",
                   [[["repository_id", "=", rp["id"]],
                     ["technical_name", "=", "primate_academy"]],
                    ["id", "version", "db_state"]], {"context": {}})
        return mods[0] if mods else None

    m = esperar(academy_detectado, "primate_academy detectado en el clon",
                intentos=30, pausa=10)
    print("ACADEMY_STG: primate_academy está en el clon del staging ✓ (%s)"
          % json.dumps(m), flush=True)
    CTX.close()
    nav.close()
print("ACADEMY_STG: FIN — ahora en el Odoo del STAGING (Login as admin): "
      "modo dev → Apps → «Actualizar lista de aplicaciones» → instalar "
      "primate_academy.", flush=True)
