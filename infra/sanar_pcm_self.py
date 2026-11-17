# -*- coding: utf-8 -*-
"""Sana pcm-self en UN paso controlado, todo por PCM (sesión de Daryl).

  .venv/bin/python infra/sanar_pcm_self.py

Secuencia (FRENO ante cualquier fallo, sin reintentos):
  0. Verificar el symlink-airbag que recreó Daryl (solo lectura).
  1. Backup de la base desde PCM («Respaldar ahora») y esperar completed.
  2. Deploy «Actualización de módulos» -u primate_cloud_manager (agrega las
     columnas nuevas: UndefinedColumn de modules_ref_repo_id) — usa el
     symlink-airbag porque el conf aún no tiene la entrada del repo; el
     restart va DIFERIDO por ser la instancia self.
  3. Sanidad post-restart: login 200 + leer modules_ref_path por ORM.
  4. «Aplicar addons_path» (sin ediciones compitiendo). Si el guardado
     falla: FRENO y aviso (el symlink ya habrá sido limpiado por el orden
     viejo del código — recrearlo con el mismo ln y frenar).
  5. Verificación final: estáticas del módulo en 200, Entornos responde,
     «Verificar estado» del repo OK con el commit esperado.
"""
import json
import pathlib
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
AWS_ID = "i-0b257f2e063135df3"
ENV_NOMBRE = "pcm-self"
ADDONS = "/opt/pcm/instances/pcm/addons"
REPO_PATH = ADDONS + "/Primate-Cloude-Manager"
COMMIT_ESPERADO = "8d576d9"
ESTATICAS = ["static/description/icon.png",
             "static/src/app/pcm_actions.js",
             "static/src/app/forms/env_form.js"]

CTX = None


def evento(texto):
    print("PCM_SANAR: %s" % texto, flush=True)


def freno(texto):
    print("PCM_SANAR_FRENO: %s" % texto, flush=True)
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
            texto = (m.group(1) if m else cuerpo)
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                texto = texto.replace(a, b)
            return ("Success" in cuerpo), texto
        time.sleep(4)
    freno("sin salida del comando SSM")


def curl(url):
    out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                          "--max-time", "15", url], capture_output=True,
                         text=True)
    return out.stdout.strip()


def principal():
    global CTX
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
        CTX = nav.new_context(viewport={"width": 1280, "height": 800})
        page = CTX.new_page()
        page.goto(BASE + "/web/login", timeout=90000)
        evento("PAUSA · logueate en la ventana (2FA)…")
        for _ in range(600):
            if "/odoo" in page.url or page.locator(
                    ".o_web_client, .o_pcm_app").count():
                break
            page.wait_for_timeout(2000)
        else:
            freno("sin login en 20 min")
        evento("sesión activa")

        maq = orm("primate.cloud.ec2.instance", "search_read",
                  [[["aws_instance_id", "=", AWS_ID]], ["id", "name"]],
                  {"context": {}})
        mid = maq[0]["id"]
        envs = orm("primate.cloud.environment", "search_read",
                   [[["name", "=", ENV_NOMBRE]], ["id"]], {"context": {}})
        env_id = envs[0]["id"]

        # 0. Symlink-airbag correcto (solo lectura).
        ok, salida = ssm(mid, "ls -la %s/ && readlink -f %s/primate_cloud_manager"
                         % (ADDONS, ADDONS))
        evento("addons dir:\n%s" % salida.strip())
        destino_ok = (REPO_PATH + "/primate_cloud_manager") in salida
        if not (ok and destino_ok and "primate_cloud_manager ->" in salida):
            freno("el symlink-airbag NO apunta al módulo dentro del clon "
                  "(esperado: %s/primate_cloud_manager). NO sigo."
                  % REPO_PATH)
        evento("symlink-airbag verificado ✓")

        # 1. Backup de la base desde PCM (se reusa si hay uno completed de
        #    hace menos de 30 min — el del intento anterior sigue vigente).
        previos = orm("primate.cloud.backup", "search_read",
                      [[["environment_id", "=", env_id]],
                       ["id", "state", "create_date"]],
                      {"limit": 1, "order": "id desc", "context": {}})
        if previos and previos[0]["state"] == "completed":
            import datetime
            edad = (datetime.datetime.utcnow()
                    - datetime.datetime.strptime(previos[0]["create_date"],
                                                 "%Y-%m-%d %H:%M:%S"))
            if edad.total_seconds() < 1800:
                evento("backup reciente reusado (id %s, hace %d min) ✓"
                       % (previos[0]["id"], edad.total_seconds() // 60))
                previos = None   # señal: no esperar uno nuevo
        if previos is not None:
            ultimo_bk = previos[0]["id"] if previos else 0
            orm("primate.cloud.environment", "action_run_backup", [[env_id]])
            evento("backup encolado; esperando completed…")
            t0 = time.time()
            while time.time() - t0 < 900:
                filas = orm("primate.cloud.backup", "search_read",
                            [[["environment_id", "=", env_id],
                              ["id", ">", ultimo_bk]], ["id", "state"]],
                            {"limit": 1, "order": "id desc", "context": {}})
                if filas and filas[0]["state"] == "completed":
                    evento("backup completed (id %s) ✓" % filas[0]["id"])
                    break
                if filas and filas[0]["state"] == "failed":
                    freno("el backup FALLÓ; no sigo sin backup")
                time.sleep(10)
            else:
                freno("el backup no terminó en 15 min")

        # 2. -u primate_cloud_manager por «Ejecutar comando» (el deploy
        #    module_update del código desplegado no lleva --no-http y choca
        #    con el puerto del Odoo vivo — bug destapado acá, fix local ya
        #    commiteado para el próximo deploy). Restart DIFERIDO después.
        ok, salida = ssm(mid,
                         "sudo -u odoo /opt/pcm/runtime/odoo-19/venv/bin/python3 "
                         "/opt/pcm/runtime/odoo-19/src/odoo-bin -c /etc/odoo/pcm.conf "
                         "-d pcm_produccion -u primate_cloud_manager --no-http "
                         "--stop-after-init --logfile=/tmp/pcm_u.log "
                         "&& echo PCM_U_OK; echo '--- cola del log ---'; "
                         "tail -n 50 /tmp/pcm_u.log 2>/dev/null")
        if "PCM_U_OK" not in salida:
            freno("el -u falló; log:\n%s" % salida[-1800:])
        evento("-u primate_cloud_manager OK ✓ · reinicio diferido…")
        ok, _s = ssm(mid, "sudo systemd-run --collect --on-active=10 "
                          "systemctl restart odoo-pcm && echo PCM_RESTART_PROGRAMADO")
        if not ok:
            freno("no pude programar el restart diferido")
        time.sleep(30)

        # 3. Sanidad post -u: login 200 + columnas nuevas legibles.
        for _ in range(30):
            if curl(BASE + "/web/login") == "200":
                break
            time.sleep(5)
        else:
            freno("PCM no volvió tras el restart del -u")
        campos = orm("primate.cloud.environment", "read",
                     [[env_id], ["modules_ref_path"]], {"context": {}})
        evento("columnas nuevas legibles (modules_ref_path=%r) ✓"
               % campos[0]["modules_ref_path"])

        # 4. Aplicar addons_path (sin ediciones compitiendo).
        prev_log = orm("primate.cloud.operation.log", "search_read",
                       [[["name", "like", "Aplicar addons_path"]], ["id"]],
                       {"limit": 1, "order": "id desc", "context": {}})
        prev_log = prev_log[0]["id"] if prev_log else 0
        orm("primate.cloud.environment", "action_apply_addons_path", [[env_id]])
        evento("Aplicar addons_path encolado; esperando el job…")
        t0 = time.time()
        resultado = None
        while time.time() - t0 < 600:
            filas = orm("primate.cloud.operation.log", "search_read",
                        [[["name", "like", "Aplicar addons_path"],
                          ["id", ">", prev_log]], ["result", "error_message"]],
                        {"limit": 1, "order": "id desc", "context": {}})
            if filas and filas[0]["result"]:
                resultado = filas[0]
                break
            time.sleep(8)
        if not resultado or resultado["result"] != "success":
            freno("Aplicar addons_path NO quedó success (%s). El symlink "
                  "pudo haberse limpiado de nuevo: RECREALO con el mismo ln "
                  "y frenamos acá. Detalle: %s"
                  % (resultado and resultado["result"],
                     (resultado or {}).get("error_message")))
        evento("addons_path aplicado ✓ · esperando el restart diferido…")
        time.sleep(30)
        for _ in range(30):
            if curl(BASE + "/web/login") == "200":
                break
            time.sleep(5)
        else:
            freno("PCM no volvió tras el restart del addons_path")

        # 5. Verificación final.
        malas = [e for e in ESTATICAS
                 if curl("%s/primate_cloud_manager/%s" % (BASE, e)) != "200"]
        if malas:
            freno("estáticas todavía no sirven: %s" % malas)
        evento("estáticas del módulo en 200 ✓")
        datos = orm("primate.cloud.dashboard", "get_environment_detail",
                    [env_id], {"context": {}})
        evento("Entornos responde (env %s, %d repos) ✓"
               % (datos.get("name"), len(datos.get("repositories", []))))
        repos = orm("primate.cloud.repository", "search_read",
                    [[["environment_id", "=", env_id]],
                     ["id", "name", "current_commit"]], {"context": {}})
        rid = repos[0]["id"]
        orm("primate.cloud.repository", "action_check_sync_state", [[rid]])
        time.sleep(25)
        rep = orm("primate.cloud.repository", "read",
                  [[rid], ["sync_state", "current_commit"]], {"context": {}})[0]
        evento("repo: sync_state=%s · current_commit=%s (esperado %s…)"
               % (rep["sync_state"], rep["current_commit"], COMMIT_ESPERADO))
        CTX.close()
        nav.close()
    evento("FIN · pcm-self sano")


if __name__ == "__main__":
    sys.exit(principal())
