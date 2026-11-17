# -*- coding: utf-8 -*-
"""Diagnóstico de SOLO LECTURA de las vistas heredadas rotas de bj-uat.

  .venv/bin/python infra/diagnostico_bjuat.py

Todo va VÍA PCM Producción (regla del día D: nada directo contra la cuenta
real): el navegador se abre en https://primatecloudmanager.com, DARYL se
loguea (2FA) y recién ahí el script corre los comandos de lectura con
``job_execute_command`` (SSM por PCM, salida al chatter, de donde se lee).

Nada muta: psql solo con SELECT; el paso de validación corre odoo-bin shell
con rollback final y sin commit. Los resultados quedan en
``infra/bjuat_diag/``.
"""
import json
import pathlib
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://primatecloudmanager.com"
MAQUINA_AWS = "i-0617ee15ddadb0a71"      # bj-uat (de la base congelada local)
DB = "bj_uat"
CONF = "/etc/odoo/bj-uat.conf"
RUNTIME = "/opt/pcm/runtime/odoo-19"
HERE = pathlib.Path(__file__).parent
SALIDA = HERE / "bjuat_diag"
PSQL = "sudo -u postgres psql -d %s -At -F'|' -c " % DB

CTX = None


def evento(texto):
    print("PCM_BJUAT: %s" % texto, flush=True)


def orm(model, method, args, kwargs=None):
    r = CTX.request.post(
        BASE + "/web/dataset/call_kw/%s/%s" % (model, method),
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": kwargs or {"context": {}}}}),
        headers={"Content-Type": "application/json"}, timeout=400000)
    datos = r.json()
    assert "error" not in datos, json.dumps(datos["error"])[:400]
    return datos["result"]


def ultimo_msg(mid):
    filas = orm("mail.message", "search_read",
                [[["model", "=", "primate.cloud.ec2.instance"],
                  ["res_id", "=", mid]], ["id"]],
                {"limit": 1, "order": "id desc", "context": {}})
    return filas[0]["id"] if filas else 0


def correr_ssm(mid, comando, rotulo):
    """job_execute_command (sync) + salida desde el chatter. Solo lectura."""
    evento("SSM · %s" % rotulo)
    previo = ultimo_msg(mid)
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
            # des-escapar lo básico del html del chatter
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                texto = texto.replace(a, b)
            estado = "Success" in cuerpo
            (SALIDA / ("%s.txt" % rotulo)).write_text(texto)
            evento("SSM OK · %s (%d bytes, success=%s)"
                   % (rotulo, len(texto), estado))
            return texto
        time.sleep(4)
    raise RuntimeError("sin salida en el chatter para %s" % rotulo)


# Fase 4 (validación): raíces primero, pinpoint después. SIN COMMIT.
VALIDADOR = r'''sudo -u odoo timeout 280 @RT@/venv/bin/python3 @RT@/src/odoo-bin shell -c @CONF@ --no-http <<'PY'
Vista = env["ir.ui.view"]
raices = Vista.search([("inherit_id", "=", False), ("active", "=", True)])
print("PCM_RAICES:", len(raices))
malas = []
for r in raices:
    try:
        r.get_combined_arch()
    except Exception as e:
        malas.append((r, str(e).replace("\n", " ")[:160]))
print("PCM_RAICES_ROTAS:", len(malas))
for r, err in malas[:60]:
    print("PCM_RAIZ_ROTA:|%s|%s|%s|%s|%s" % (
        r.id, (r.name or "")[:50], r.model or "", r.type or "", err))
env.cr.rollback()
print("PCM_FIN_VALIDACION")
PY'''.replace("@RT@", RUNTIME).replace("@CONF@", CONF)


def principal():
    global CTX
    SALIDA.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=False, args=["--no-sandbox"])
        CTX = nav.new_context(viewport={"width": 1440, "height": 860})
        page = CTX.new_page()
        page.goto(BASE + "/web/login", timeout=90000)
        evento("PAUSA · logueate en la ventana (2FA); espero el backend…")
        # wait_for_timeout (y no time.sleep): bombea los eventos del navegador
        # — con sleep la URL nunca se refresca para el script (lección real).
        logueado = False
        for _ in range(600):
            if "/odoo" in page.url or page.locator(
                    ".o_web_client, .o_pcm_app").count():
                logueado = True
                break
            page.wait_for_timeout(2000)
        if not logueado:
            raise RuntimeError("no hubo login en 20 min")
        evento("sesión activa; arranco el diagnóstico de solo lectura")

        filas = orm("primate.cloud.ec2.instance", "search_read",
                    [[["aws_instance_id", "=", MAQUINA_AWS]],
                     ["id", "name", "instance_state"]], {"context": {}})
        assert filas, "no encuentro la máquina de bj-uat en PCM"
        maq = filas[0]
        assert maq["instance_state"] == "running", maq
        mid = maq["id"]
        evento("máquina %s (%s)" % (maq["name"], MAQUINA_AWS))

        # 1. La vista del error + su módulo + su padre (y xml_ids).
        correr_ssm(mid, PSQL + "\"select v.id, v.name, v.model, v.active, "
                   "v.inherit_id, coalesce(d.module,'(sin xml_id)'), "
                   "coalesce(d.name,'') from ir_ui_view v left join "
                   "ir_model_data d on d.model='ir.ui.view' and d.res_id=v.id "
                   "where v.arch_db::text like '%button_import_placeholder%'\"",
                   "c1_vista_rota")
        correr_ssm(mid, PSQL + "\"select v.id, p.id, p.name, p.model, p.type, "
                   "coalesce(dp.module,''), coalesce(dp.name,'') from "
                   "ir_ui_view v join ir_ui_view p on p.id=v.inherit_id left "
                   "join ir_model_data dp on dp.model='ir.ui.view' and "
                   "dp.res_id=p.id where v.arch_db::text like "
                   "'%button_import_placeholder%'\"", "c2_vista_padre")
        # 2. Estado de módulos no-estándar con vistas (candidatos v13).
        correr_ssm(mid, PSQL + "\"select m.name, m.state, "
                   "coalesce(m.latest_version,''), coalesce(m.author,'') from "
                   "ir_module_module m where m.state in "
                   "('installed','to upgrade','to install') and m.author not "
                   "ilike '%odoo%' order by m.name\"", "c3_modulos_no_odoo")
        # 3. Menús que abren el modelo de la vista padre (se cruza después).
        correr_ssm(mid, PSQL + "\"select a.id, a.name->>'en_US', a.res_model, "
                   "coalesce(mm.id,0), coalesce(mm.complete_name,'') from "
                   "ir_act_window a left join ir_ui_menu mm on mm.action = "
                   "'ir.actions.act_window,'||a.id where a.res_model in "
                   "(select distinct model from ir_ui_view where "
                   "arch_db::text like '%button_import_placeholder%')\"",
                   "c4_menus")
        # 4. TODAS las vistas heredadas rotas (validación runtime, sin commit).
        correr_ssm(mid, VALIDADOR, "c5_validacion_total")
        evento("FIN · resultados en %s" % SALIDA)
        CTX.close()
        nav.close()


if __name__ == "__main__":
    sys.exit(principal())
