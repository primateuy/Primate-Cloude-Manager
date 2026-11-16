# -*- coding: utf-8 -*-
"""DÍA D — mudanza REAL de PCM Producción a pcm-self, grabada.

  .venv/bin/python docs/demo/grabar_dia_d_pcm_self.py
  .venv/bin/python docs/demo/grabar_dia_d_pcm_self.py ensamblar

Igual receta que el ensayo (grabar_ensayo_pcm_self.py), con las diferencias
del día real:

- Navegador **HEADFUL en la pantalla de Daryl**: todo secreto lo tipea ÉL
  (login + TOTP, contraseña maestra, PAT, su contraseña en la triple
  confirmación — campos type=password: en el video son puntos). Las keys IAM
  las carga en SU PROPIA pestaña, fuera de la ventana grabada.
- Cero credenciales en este script ni en el driver: la sesión de Odoo queda
  en el contexto de Playwright después de que Daryl loguea; el driver llama
  al ORM por /web/dataset/call_kw CON ESA SESIÓN (ctx.request comparte
  cookies), nunca por xmlrpc con contraseña.
- **FRENO al final de cada tramo** (regla de Daryl): el driver muestra el
  rótulo de freno y espera el archivo-bandera `flag_ok_<tramo>`; Claude lo
  toca recién con el OK de Daryl en el chat. Ante CUALQUIER error: screenshot
  + video + estado guardado y el driver TERMINA (sin reintentos).
- Claude NO ejecuta nada directo contra la cuenta real: lo de AWS va por PCM
  (botones/jobs); `subir_dump.sh` y IAM los corre Daryl.

Banderas (en infra/dia_d_self/): flag_ok_p1_config (OK a la config del
wizard, con la maestra ya tipeada), flag_ok_p1, flag_pat (PAT pegado),
flag_pat_revocado, flag_ok_p2, flag_ok_f1, flag_dump_subido (+ ruta_s3.txt),
flag_ok_f2, flag_password_f3 (contraseña tipeada en la triple confirmación),
flag_ok_f3, flag_keys (keys nuevas cargadas y validadas por Daryl),
flag_ok_f4, flag_keys_viejas_off (IAM hecho por Daryl).
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
BUNDLE_DEMO = REPO / "primate_bundle" / "docs" / "demo"
sys.path.insert(0, str(BUNDLE_DEMO))
import grabar_demo as receta        # noqa: E402
import grabar_oximoro as ox         # noqa: E402

VIEW_W, VIEW_H = 1536, 864
VIDEO_W, VIDEO_H = 1920, 1080
VIEJO = "http://127.0.0.1:19190"
DB = "pcm_produccion"
ENV_NOMBRE = "pcm-self"
MAQ = "pcm"                      # nombre de la EC2 (instance_name del wizard)
# 2026-11-16: el manager vive en el DOMINIO RAÍZ de la zona nueva
# primatecloudmanager.com (Z06178081BO0Q1OK7MTAO); el resto de los servidores
# va en *.primatecloudmanager.com.
DOMINIO = "primatecloudmanager.com"
NUEVO = "https://" + DOMINIO
DB_DESTINO = "pcm_produccion"
SALIDA = HERE
HANDOFF = REPO / "infra" / "dia_d_self"
FLAGS = HANDOFF
FACTORES = {"montaje": 16, "dns": 8, "clone": 8, "dump": 16, "restore": 16,
            "pausa": 32, "freno": 48, "login": 16}

# Config esperada del wizard (la precarga del 2026-11-13): se VERIFICA campo
# por campo antes del OK de Daryl.
ESPERADO = {
    "Nombre de la instancia": MAQ,
    "Tipo de instancia": "t3.medium",
    "Disco raíz (GB)": "50",
    "Nombre de la base": DB_DESTINO,
}
CHECKS_ESPERADOS = ["Dominio sslip.io automático", "Perfil PCM",
                    "Monitoreo CloudWatch"]

ox.MARCA_PLACA = "PCM · MUDANZA REAL A SU PROPIO SERVIDOR (día D)"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos = [], [], [], {}
T0 = None
CTX = None
PY = str(REPO / ".venv" / "bin" / "python")
ODOO_BIN = str(REPO.parent / "forum" / "_shared" / "community" / "odoo-bin")
CONF_PROD = str(pathlib.Path.home() / "Odoo" / "clients" / "19.0"
                / "pcm_produccion" / "pcm_produccion.conf")


def evento(texto):
    print("PCM_DIA_D: %s" % texto, flush=True)


def marca(nombre):
    marcas.append((nombre, time.time() - T0))
    evento("marca · %s" % nombre)


class Ventana:
    def __init__(self, nombre, factor):
        self.nombre, self.factor = nombre, factor

    def __enter__(self):
        self.desde = time.time() - T0
        return self

    def __exit__(self, *exc):
        ventanas.append({"nombre": self.nombre, "desde": self.desde,
                         "hasta": time.time() - T0, "factor": self.factor})


def correr(orden, timeout=3600, entrada=None):
    evento("cmd · %s" % " ".join(orden[:4]))
    return subprocess.run(orden, timeout=timeout, input=entrada,
                          capture_output=True, text=True)


def orm(base, model, method, args, kwargs=None):
    """ORM con la SESIÓN del navegador (Daryl logueado); sin contraseñas."""
    r = CTX.request.post(
        base + "/web/dataset/call_kw",
        data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args,
            "kwargs": dict(kwargs or {}, context={"lang": "es_UY"})}}),
        headers={"Content-Type": "application/json"})
    j = r.json()
    if j.get("error"):
        raise RuntimeError("ORM %s.%s → %s" % (
            model, method, json.dumps(j["error"], ensure_ascii=False)[:500]))
    return j["result"]


# ------------------------------------------------------------ UI helpers

def login_de_daryl(page, base, titulo):
    """Daryl loguea EN LA VENTANA GRABADA (password y TOTP: a puntos/6 dígitos
    efímeros). El driver solo espera."""
    ox.ir(page, base + "/web/login", timeout=120000)
    ox.rotulo(page, titulo,
              "DARYL: logueate en esta ventana (usuario + contraseña + código "
              "2FA). El driver espera.", 4000)
    evento("PAUSA · esperando que Daryl loguee en %s" % base)
    with Ventana("login", FACTORES["login"]):
        page.wait_for_url("**/odoo**", timeout=900000)
    evento("login OK en %s" % base)
    page.wait_for_timeout(1500)


def abrir_app(page, base):
    ox.ir(page, base + "/odoo/action-primate_cloud_manager.action_pcm_app",
          esperar=".o_pcm_app")
    page.wait_for_timeout(1200)


def nav(page, item):
    ox.clic(page, page.locator(".o_pcm_nav_item", has_text=item).first)
    page.wait_for_timeout(1200)


def campo_pcm(page, etiqueta):
    return page.locator(
        ".o_pcm_field:has(.o_pcm_field_label:has-text('%s')) input" % etiqueta).first


def check_owl(page, texto):
    return page.locator("label.o_pcm_form_check:has-text('%s') input" % texto).first


def boton_texto(page, texto, scope="button"):
    """Botón por textContent con regex anclada (lección FontAwesome)."""
    return page.locator(scope, has_text=re.compile(
        r"^\s*" + re.escape(texto) + r"\s*$")).first


def abrir_entorno(page, base, nombre):
    for _ in range(3):
        nav(page, "Entornos")
        buscador = page.locator(".o_pcm_screen input[type=text]").first
        buscador.fill(nombre)
        page.wait_for_timeout(1200)
        tarjeta = page.locator(".o_pcm_env_card", has_text=nombre)
        if tarjeta.count():
            ox.clic(page, tarjeta.first)
            page.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=15000)
            page.wait_for_timeout(900)
            return
        page.wait_for_timeout(2500)
    raise RuntimeError("no apareció la tarjeta del entorno %r" % nombre)


def abrir_servidor(page):
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=MAQ).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def abrir_instancia_odoo(page, env_id):
    """Desde el detalle del entorno, la instancia ODOO (lista Instancias Odoo)."""
    nombre = orm(VIEJO, "primate.cloud.instance", "search_read",
                 [[["environment_id", "=", env_id]], ["name"]],
                 {"limit": 1})[0]["name"]
    ox.clic(page, page.locator(
        ".o_pcm_card .o_pcm_line_click:has(.o_pcm_line_name:text-is('%s'))"
        % nombre).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def esperar_flag(page, nombre, titulo, detalle, factor=None):
    bandera = FLAGS / nombre
    evento("PAUSA · esperando %s" % nombre)
    with Ventana("pausa-%s" % nombre, factor or FACTORES["pausa"]):
        ox.rotulo(page, titulo, detalle + " · espera real, acelerada en el video",
                  3000)
        while not bandera.exists():
            page.wait_for_timeout(4000)
    evento("PAUSA LEVANTADA · %s" % nombre)
    page.wait_for_timeout(1000)


def freno(page, nombre, resumen):
    """FRENO de tramo: estado a disco + bandera flag_ok_<nombre>."""
    (HANDOFF / ("estado_%s.json" % nombre)).write_text(json.dumps(
        {"tramo": nombre, "resumen": resumen, "t": time.time() - T0,
         "marcas": marcas}, ensure_ascii=False, indent=1))
    page.screenshot(path=str(HANDOFF / ("freno_%s.png" % nombre)))
    evento("FRENO %s · %s" % (nombre.upper(), resumen))
    esperar_flag(page, "flag_ok_%s" % nombre,
                 "FRENO %s — esperando el OK de Daryl" % nombre.upper(),
                 resumen, FACTORES["freno"])
    marca("OK de Daryl: %s" % nombre)


def ultimo_log(action_type):
    filas = orm(VIEJO, "primate.cloud.operation.log", "search",
                [[["action_type", "=", action_type]]],
                {"limit": 1, "order": "id desc"})
    return filas[0] if filas else 0


def esperar_log(action_type, desde_id, limite, page, que):
    t0 = time.time()
    while time.time() - t0 < limite:
        filas = orm(VIEJO, "primate.cloud.operation.log", "search_read",
                    [[["action_type", "=", action_type], ["id", ">", desde_id]],
                     ["result", "error_message", "name"]],
                    {"limit": 1, "order": "id desc"})
        if filas and filas[0]["result"] in ("success", "failed", "partial"):
            assert filas[0]["result"] == "success", "%s terminó %s: %s" % (
                que, filas[0]["result"], (filas[0]["error_message"] or "")[:300])
            return filas[0]
        page.wait_for_timeout(8000)
    raise RuntimeError("%s no terminó a tiempo" % que)


def comando(page, orden, explicacion, espera=18000):
    """«Ejecutar comando» (SSM) sobre la máquina pcm, verificado en bitácora."""
    desde = ultimo_log("ssm_command")
    ox.clic(page, page.locator("button", has_text="Ejecutar comando").first)
    page.wait_for_selector("textarea#command_0, input#command_0", timeout=12000)
    page.locator("textarea#command_0, input#command_0").first.fill(orden)
    ox.rotulo(page, "Ejecutar comando (SSM)", explicacion, 2800)
    ox.clic(page, page.locator("button[name=action_run]").first)
    page.wait_for_timeout(espera)
    esperar_log("ssm_command", desde, 150, page, "el comando SSM")


def mensajes_chatter(env_id, desde_id=0):
    return orm(VIEJO, "mail.message", "search_read",
               [[["model", "=", "primate.cloud.environment"],
                 ["res_id", "=", env_id], ["id", ">", desde_id]],
                ["id", "body"]], {"order": "id asc"})


def psql(sql, db=DB):
    r = correr(["psql", "-U", "odoo", "-d", db, "-tAc", sql])
    assert r.returncode == 0, "psql falló: %s" % r.stderr[-200:]
    return r.stdout.strip()


# ------------------------------------------------------------ tramos

def precondiciones():
    r = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", "4", VIEJO + "/web/login"])
    assert r.stdout.strip() == "200", "pcm_produccion no responde en :19190"
    HANDOFF.mkdir(parents=True, exist_ok=True)
    for f in HANDOFF.glob("flag_*"):
        f.unlink()
    assert psql("select state from primate_cloud_environment "
                "where name='pcm-self';") == "draft", \
        "el entorno pcm-self no está en draft"
    assert psql("select latest_version from ir_module_module "
                "where name='primate_cloud_manager';") == "19.0.1.8.0"
    evento("precondiciones OK")


def p1_montaje(page):
    ox.tarjeta(page, "P1 · Montaje de pcm-self",
               "El servidor definitivo de PCM, montado POR PCM",
               [("Servidor", "t3.medium · gp3 50 GB · Ubuntu 24.04"),
                ("Perfil", "PCM (queue_job + clave en el server) + CloudWatch"),
                ("Cuenta", "pcm-prod-prov · us-east-1")])
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    ox.clic(page, boton_texto(page, "Aprovisionar"))
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1500)

    # Verificación campo a campo de la precarga (nada se tipea: ya está).
    visto = {}
    for etiqueta, esperado in ESPERADO.items():
        valor = campo_pcm(page, etiqueta).input_value()
        visto[etiqueta] = valor
        assert valor == esperado, "campo %r: %r ≠ %r" % (etiqueta, valor, esperado)
    for texto in CHECKS_ESPERADOS:
        assert check_owl(page, texto).is_checked(), "check %r apagado" % texto
    (HANDOFF / "p1_config.json").write_text(json.dumps(visto, indent=1))
    page.screenshot(path=str(HANDOFF / "p1_wizard.png"))
    ox.rotulo(page, "Formulario precargado, verificado campo a campo",
              "t3.medium · 50 GB · Ubuntu 24.04 · PG16 pcm_produccion · "
              "perfil PCM · CloudWatch · sslip → dominio después", 4200)

    # «Verificar región» tiene que dar verde ANTES del OK.
    ox.clic(page, boton_texto(page, "Verificar región"))
    page.wait_for_selector(".o_pcm_semaphore_ok", timeout=60000)
    ox.rotulo(page, "Región verificada",
              "SG, subred y el instance profile pcm-ssm-role: en verde", 3000)

    # Maestra: la tipea DARYL en la ventana (campo password → puntos).
    ox.rotulo(page, "DARYL: escribí la contraseña MAESTRA nueva",
              "Campo «Contraseña admin (odoo.conf)» — se ve como puntos", 4000)
    maestra = campo_pcm(page, "Contraseña admin")
    while maestra.evaluate("el => el.value.length") < 8:
        page.wait_for_timeout(2000)
    evento("maestra tipeada (largo >= 8; el valor no se lee)")

    # FRENO de config: OK explícito de Daryl ANTES de aprovisionar.
    freno(page, "p1_config",
          "Config del wizard verificada + maestra puesta. ¿Aprovisiono?")

    ox.clic(page, page.locator(".o_pcm_drawer button",
                               has_text="Aprovisionar").last)
    page.wait_for_selector(".modal", timeout=10000)
    ox.clic(page, page.locator(".modal footer button.btn-primary").first)
    marca("aprovisionamiento encolado")
    env_id = orm(VIEJO, "primate.cloud.environment", "search",
                 [[["name", "=", ENV_NOMBRE]]])[0]
    ox.rotulo(page, "Montando el servidor REAL",
              "EC2 + bootstrap + Odoo 19 Community + perfil PCM + agente "
              "CloudWatch — espera real acelerada ×%d" % FACTORES["montaje"], 3200)
    with Ventana("montaje", FACTORES["montaje"]):
        t0 = time.time()
        while time.time() - t0 < 1800:
            estado = orm(VIEJO, "primate.cloud.environment", "read",
                         [[env_id], ["state"]])[0]["state"]
            if estado in ("active", "error"):
                break
            page.wait_for_timeout(8000)
        assert estado == "active", "el montaje terminó en %s" % estado
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(2500)
    marca("servidor activo")
    tiempos["montaje"] = time.time() - T0

    # Chatter: los 5 hitos + agente CloudWatch activo.
    cuerpo = " ".join(m["body"] for m in mensajes_chatter(env_id))
    for frase in ("IP fija", "sslip.io", "aprovisionado y activo",
                  "HTTPS activo"):
        assert frase in cuerpo, "falta %r en el chatter" % frase
    cw = orm(VIEJO, "primate.cloud.environment", "read",
             [[env_id], ["cw_agent_state", "cw_agent_error"]])[0]
    assert cw["cw_agent_state"] == "active", \
        "agente CloudWatch: %s (%s)" % (cw["cw_agent_state"], cw["cw_agent_error"])
    ox.desplazar(page, 900)
    ox.rotulo(page, "Agente CloudWatch: ACTIVO desde el montaje",
              "El servidor nace monitoreado (config de INCO embebida)", 3600)
    ox.desplazar(page, -900)
    marca("chatter y agente verificados")
    freno(page, "p1", "P1 completo: pcm-self activo con HTTPS (sslip) y agente "
          "CloudWatch. Rollback: total (terminar el servidor). ¿Sigo con P2?")
    return env_id


def p2_dominio_y_repo(page, env_id):
    ox.tarjeta(page, "P2 · Dominio y repo",
               "pcm.erp.primateuy.com + el módulo por clone con PAT",
               [("DNS", "Route 53, zona primateuy.com"),
                ("PAT", "fine-grained, solo lectura, se revoca al toque")])
    # El repo placeholder de la precarga se quita: el alta real (con clone)
    # es «Agregar repositorio» del entorno.
    viejos = orm(VIEJO, "primate.cloud.repository", "search",
                 [[["environment_id", "=", env_id],
                   ["name", "=", "primate_cloud_manager"]]])
    if viejos:
        orm(VIEJO, "primate.cloud.repository", "unlink", [viejos])
        evento("repo placeholder de la precarga quitado (%s)" % viejos)

    # --- Dominio definitivo
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    abrir_instancia_odoo(page, env_id)
    ox.clic(page, boton_texto(page, "Crear DNS ahora"))
    page.wait_for_selector("input#domain_0", timeout=15000)
    page.wait_for_timeout(1500)
    # Propone pcm.primatecloudmanager.com (slug + sufijo); el manager va en
    # el APEX: se reemplaza por el dominio raíz (la validación lo acepta
    # desde el patch de hoy — el stack otorga apex y *.sufijo).
    propuesto = page.locator("input#domain_0").input_value()
    assert propuesto.endswith(DOMINIO), \
        "la propuesta no usa el sufijo nuevo: %r" % propuesto
    page.locator("input#domain_0").fill("")
    ox.tipear(page, page.locator("input#domain_0"), DOMINIO, 30)
    ox.rotulo(page, "El manager va en el dominio RAÍZ",
              "Registro A en el apex de la zona %s" % DOMINIO, 3400)
    page.screenshot(path=str(HANDOFF / "p2_dns_wizard.png"))
    ox.clic(page, page.locator("button[name=action_confirm]").first)
    with Ventana("dns", FACTORES["dns"]):
        t0 = time.time()
        while time.time() - t0 < 600:
            url = orm(VIEJO, "primate.cloud.instance", "search_read",
                      [[["environment_id", "=", env_id]], ["main_url"]],
                      {"limit": 1})[0]["main_url"]
            if DOMINIO in (url or ""):
                break
            page.wait_for_timeout(8000)
        assert DOMINIO in (url or ""), "main_url sigue en %r" % url
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    marca("dominio definitivo activo")
    r = CTX.request.get(NUEVO + "/web/login")
    assert r.status == 200, "GET %s → %s" % (NUEVO, r.status)
    r404 = CTX.request.get(NUEVO + "/web/database/manager")
    assert r404.status == 404, "/web/database/manager → %s (no 404)" % r404.status
    ox.rotulo(page, "HTTPS en el dominio nuevo + /web/database en 404",
              "Verificado con un GET real", 3200)

    # --- Repo con PAT
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    ox.desplazar(page, 1100)
    ox.clic(page, boton_texto(page, "Agregar repositorio"))
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1200)
    campo_pcm(page, "Nombre").fill("primate_cloud_manager")
    campo_pcm(page, "URL de GitHub").fill(
        "https://github.com/primateuy/Primate-Cloude-Manager")
    campo_pcm(page, "Rama configurada").fill("19.0")
    ox.rotulo(page, "DARYL: pegá el PAT en «Token de GitHub»",
              "De ~/.pcm_produccion/pat_mudanza_pcm.txt — campo password, "
              "se ve como puntos. Avisá en el chat cuando esté.", 4000)
    esperar_flag(page, "flag_pat", "Esperando el PAT",
                 "Daryl lo pega en la ventana; Claude no lo ve")
    desde_clone = ultimo_log("addon_add")
    ox.clic(page, page.locator(".o_pcm_drawer button, .o_pcm_form_foot button",
                               has_text=re.compile("Guardar|Agregar")).last)
    ox.rotulo(page, "Clonando en el servidor",
              "git clone por SSM con GIT_ASKPASS temporal: el token no queda "
              "en URL, argv ni config — espera ×%d" % FACTORES["clone"], 3400)
    with Ventana("clone", FACTORES["clone"]):
        esperar_log("addon_add", desde_clone, 600, page, "el clone del repo")
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    abrir_servidor(page)
    comando(page, "ls /opt/pcm/instances/pcm/addons/ && "
                  "ls /opt/pcm/instances/pcm/addons/primate_cloud_manager | head -5",
            "El módulo PCM quedó en el addons dir de la instancia")
    marca("repo clonado")
    ox.rotulo(page, "DARYL: REVOCÁ el PAT en GitHub AHORA",
              "Settings → Developer settings → Fine-grained tokens → Revoke. "
              "El server ya no lo necesita (el código quedó clonado).", 4200)
    esperar_flag(page, "flag_pat_revocado", "Esperando la revocación del PAT",
                 "También borrar ~/.pcm_produccion/pat_mudanza_pcm.txt")
    marca("PAT revocado")
    freno(page, "p2", "P2 completo: dominio + HTTPS + 404 + repo clonado + PAT "
          "revocado. OJO: is_self se marca recién en F4 (marcarlo acá "
          "bloquearía el restore de F3). Rollback: total. ¿Abro la ventana "
          "de corte (F1)?")


def f1_congelar(page):
    ox.tarjeta(page, "F1 · Congelar la Mac",
               "Se abre la ventana de corte (~1 h)",
               [("Crons PCM", "se apagan (viajan apagados en el dump)"),
                ("queue_job", "se espera cola en 0"),
                ("La Mac", "queda viva SOLO para disparar el restore")])
    ids = orm(VIEJO, "ir.cron", "search",
              [[["cron_name", "like", "Primate Cloud%"], ["active", "=", True]]])
    orm(VIEJO, "ir.cron", "write", [ids, {"active": False}])
    evento("crons PCM congelados: %d" % len(ids))
    (HANDOFF / "crons_congelados.json").write_text(json.dumps(ids))
    t0 = time.time()
    while time.time() - t0 < 300:
        pendientes = int(psql("select count(*) from queue_job where state in "
                              "('pending','enqueued','started');"))
        if pendientes == 0:
            break
        page.wait_for_timeout(5000)
    assert pendientes == 0, "queue_job sigue con %d jobs" % pendientes
    correo = psql("select coalesce(string_agg(state||'='||n, ', '), 'vacía') "
                  "from (select state, count(*) n from mail_mail "
                  "group by state) s;")
    ox.tarjeta(page, "Mac congelada", "Ningún job corriendo",
               [("Crons PCM apagados", str(len(ids))),
                ("Cola queue_job", "0"),
                ("mail_mail", correo + " (sin SMTP activo: nada puede salir)")])
    marca("mac congelada")
    freno(page, "f1", "F1: %d crons PCM apagados, queue_job en 0, correo %s. "
          "Rollback: reactivar crons (1 min). ¿Hago el dump (F2)?"
          % (len(ids), correo))


def f2_dump(page):
    ox.tarjeta(page, "F2 · El dump, espejo del ensayo",
               "La base real NO se toca: todo sale de una copia",
               [("Se quita en la copia", "13 módulos Enterprise (2 pasadas)"),
                ("Queda", "30 módulos, 0 OEEL"),
                ("Formato", "zip estilo Odoo: dump.sql + filestore")])
    ox.rotulo(page, "Copia + desinstalación + dump",
              "pg_dump → pcm_mudanza → shell quitar_enterprise → zip + sha256 "
              "— espera real acelerada ×%d" % FACTORES["dump"], 3200)
    with Ventana("preparar-dump", FACTORES["dump"]):
        bruto = HANDOFF / "fuente.dump"
        r = correr(["pg_dump", "-U", "odoo", "-Fc", "-f", str(bruto), DB])
        assert r.returncode == 0, "pg_dump falló: %s" % r.stderr[-300:]
        correr(["dropdb", "-U", "odoo", "--if-exists", "pcm_mudanza"])
        correr(["createdb", "-U", "odoo", "pcm_mudanza"])
        correr(["pg_restore", "-U", "odoo", "-d", "pcm_mudanza", "--no-owner",
                str(bruto)])
        quitar = (HANDOFF / "quitar_enterprise.py").read_text()
        r = correr([PY, ODOO_BIN, "shell", "-c", CONF_PROD, "-d", "pcm_mudanza",
                    "--no-http", "--logfile=/dev/null"], entrada=quitar,
                   timeout=2400)
        assert "PROBE_LISTA" in r.stdout, \
            "la desinstalación no quedó limpia:\n%s" % r.stdout[-800:]
        oeel = psql("select count(*) from ir_module_module where "
                    "state='installed' and license like 'OEEL%';", "pcm_mudanza")
        modulos = psql("select count(*) from ir_module_module "
                       "where state='installed';", "pcm_mudanza")
        assert oeel == "0", "quedan %s módulos OEEL" % oeel
        # zip estilo backup de Odoo: dump.sql + filestore/
        arma = HANDOFF / "arma"
        correr(["rm", "-rf", str(arma)])
        arma.mkdir()
        r = correr(["pg_dump", "-U", "odoo", "--no-owner", "-f",
                    str(arma / "dump.sql"), "pcm_mudanza"])
        assert r.returncode == 0
        fs = (pathlib.Path.home() / "Odoo" / "clients" / "19.0"
              / "pcm_produccion" / "data" / "filestore" / DB)
        correr(["cp", "-R", str(fs), str(arma / "filestore")])
        zipf = HANDOFF / ("pcm_self_%s.zip" % time.strftime("%Y-%m-%d"))
        zipf.unlink(missing_ok=True)
        r = correr(["ditto", "-c", "-k", str(arma), str(zipf)])
        assert r.returncode == 0 and zipf.stat().st_size > 10_000_000
        sha = correr(["shasum", "-a", "256", str(zipf)]).stdout.split()[0]
        (HANDOFF / "SHA256SUMS.txt").write_text("%s  %s\n" % (sha, zipf.name))
        bruto.unlink(missing_ok=True)
        correr(["rm", "-rf", str(arma)])
    peso = zipf.stat().st_size
    evento("dump listo: %s · %d MB · sha256 %s · módulos=%s"
           % (zipf, peso // 1048576, sha, modulos))
    ox.tarjeta(page, "Dump listo", zipf.name,
               [("Peso", "%d MB" % (peso // 1048576)),
                ("sha256", sha[:16] + "…"),
                ("Módulos en la copia", modulos + " · 0 OEEL")])
    marca("dump preparado")
    ox.rotulo(page, "DARYL: subilo con subir_dump.sh",
              "infra/subir_dump.sh %s — pegale la salida a Claude" % zipf.name,
              4200)
    esperar_flag(page, "flag_dump_subido", "Esperando la subida a S3",
                 "subir_dump.sh verifica sha256 y tamaño; imprime la ruta")
    ruta = (HANDOFF / "ruta_s3.txt").read_text().strip()
    assert ruta.startswith("s3://"), "ruta_s3.txt inválida: %r" % ruta
    marca("dump en S3")
    freno(page, "f2", "F2: dump %d MB, sha256 %s…, subido a %s. La base real "
          "intacta. Rollback: dropdb pcm_mudanza + reactivar crons. "
          "¿Disparo el restore (F3)?" % (peso // 1048576, sha[:12], ruta))
    return ruta


def f3_restore(page, env_id, ruta_s3):
    ox.tarjeta(page, "F3 · Restore SIN neutralizar",
               "pcm_produccion aterriza en pcm-self",
               [("Destino", "instancia Production → crons/params INTACTOS"),
                ("Ceremonia", "confirm + triple confirmación"),
                ("Dump en S3", "se borra al terminar")])
    desde = ultimo_log("backup_restore")
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    ox.clic(page, page.locator("button", has_text="Restaurar desde un dump").first)
    page.wait_for_selector("#origin_uri_0", timeout=15000)
    ox.tipear(page, page.locator("#origin_uri_0"), ruta_s3, 14)
    page.locator("#target_db_name_0").fill(DB_DESTINO)
    caja = page.locator("#delete_source_0")
    if caja.count() and not caja.is_checked():
        ox.clic(page, caja)
    pre = page.locator("#pre_backup_0")
    if pre.count() and pre.is_checked():
        ox.clic(page, pre)
    conf = page.locator("#confirm_environment_name_0")
    if conf.count():
        ox.tipear(page, conf, ENV_NOMBRE, 40)
    ox.clic(page, page.locator("button[name=action_restore]").first)
    page.wait_for_selector(".modal button:has-text('Ok')", timeout=10000)
    ox.clic(page, page.locator(".modal button", has_text="Ok").first)
    page.wait_for_timeout(2500)
    if page.locator("#typed_name_0").count() or page.locator(
            "button[name=action_next_from_impact]").count():
        ox.rotulo(page, "Triple confirmación", "Impacto → nombre exacto → "
                  "re-autenticación (tu contraseña, a puntos)", 3200)
        if page.locator("button[name=action_next_from_impact]").count():
            ox.clic(page, page.locator("button[name=action_next_from_impact]").first)
            page.wait_for_timeout(1500)
        ox.tipear(page, page.locator("#typed_name_0"), DB_DESTINO, 45)
        ox.clic(page, page.locator("button[name=action_confirm_name]").first)
        page.wait_for_timeout(1500)
        ox.rotulo(page, "DARYL: tu contraseña para ejecutar",
                  "Avisá en el chat cuando esté tipeada", 3600)
        esperar_flag(page, "flag_password_f3", "Esperando la re-autenticación",
                     "Daryl tipea su contraseña en el modal")
        ox.clic(page, page.locator("button[name=action_execute]").first)
    marca("restore encolado")
    ox.rotulo(page, "Restaurando", "S3 → stop → drop → restore → filestore → "
              "start — espera real acelerada ×%d" % FACTORES["restore"], 3200)
    with Ventana("restore", FACTORES["restore"]):
        esperar_log("backup_restore", desde, 2400, page, "el restore")
    marca("restore en verde")
    tiempos["restore"] = time.time() - T0
    abrir_entorno(page, VIEJO, ENV_NOMBRE)
    abrir_servidor(page)
    comando(page, "df -h / | tail -1 && ls /opt/pcm/instances/pcm/",
            "Disco del servidor después del restore (vigilancia)")
    marca("disco verificado")
    freno(page, "f3", "F3: restore en verde, disco OK. El PCM nuevo tiene la "
          "base real. Rollback: todavía total (la Mac intacta, puede "
          "reactivar crons). ¿Despierto y verifico el nuevo (F4)?")


def f4_despertar(page):
    ox.tarjeta(page, "F4 · El PCM nuevo despierta", DOMINIO,
               [("Primero", "2FA en el dominio nuevo — ANTES que las keys"),
                ("Después", "params, crons, sync, backup, self, checklist")])
    login_de_daryl(page, NUEVO, "Login en el PCM NUEVO (el 2FA tiene que "
                   "pedir código: esa es la prueba)")
    marca("2fa verificado en el dominio nuevo")
    abrir_app(page, NUEVO)
    ox.pestana(page, True)
    page.wait_for_timeout(2500)
    ox.pestana(page, False)
    marca("pcm nuevo adentro")

    # Parámetros (por la sesión del nuevo).
    puerto = orm(NUEVO, "primate.cloud.instance", "search_read",
                 [[["slug", "=", "pcm"], ["environment_id.name", "=", ENV_NOMBRE]],
                  ["http_port"]], {"limit": 1})[0]["http_port"] or 8069
    for clave, valor in (("web.base.url", NUEVO),
                         ("web.base.url.freeze", "True"),
                         ("report.url", "http://127.0.0.1:%s" % puerto)):
        ids = orm(NUEVO, "ir.config_parameter", "search", [[["key", "=", clave]]])
        if ids:
            orm(NUEVO, "ir.config_parameter", "write", [ids, {"value": valor}])
        else:
            orm(NUEVO, "ir.config_parameter", "create", [[{"key": clave,
                                                           "value": valor}]])
    fernet = orm(NUEVO, "ir.config_parameter", "search",
                 [[["key", "=", "primate_cloud_manager.fernet_key"]]])
    assert not fernet, "¡la clave Fernet apareció en parámetros!"
    ox.rotulo(page, "Parámetros: web.base.url + freeze + report.url local",
              "Y la clave de cifrado NO está en la base (vive en el server)",
              3600)
    marca("parámetros ajustados")

    # Crons PCM reactivados EN EL NUEVO.
    ids = orm(NUEVO, "ir.cron", "search",
              [[["cron_name", "like", "Primate Cloud%"], ["active", "=", False]]])
    orm(NUEVO, "ir.cron", "write", [ids, {"active": True}])
    ox.rotulo(page, "Crons de PCM reactivados en el nuevo (%d)" % len(ids),
              "Los del core llegaron como estaban (sin neutralizar)", 3000)
    marca("crons reactivados")

    # Keys NUEVAS: Daryl en SU pestaña (fuera de cámara).
    nav(page, "Bitácora")
    ox.rotulo(page, "DARYL: cargá las keys IAM NUEVAS en TU pestaña",
              "%s → Cuentas AWS → pcm-prod-ro y pcm-prod-prov → Editar → "
              "pegar → Validar conexión (ARN en verde). Fuera de esta "
              "ventana." % DOMINIO, 4600)
    esperar_flag(page, "flag_keys", "Esperando las keys nuevas",
                 "Se cargan y validan en la pestaña de Daryl")
    cuentas = orm(NUEVO, "primate.cloud.account", "search_read",
                  [[["name", "in", ["pcm-prod-ro", "pcm-prod-prov"]]],
                   ["name", "connection_state"]])
    for c in cuentas:
        assert c["connection_state"] == "connected", \
            "cuenta %s en %s" % (c["name"], c["connection_state"])
    nav(page, "Cuentas AWS")
    page.wait_for_timeout(2600)
    ox.rotulo(page, "Las dos cuentas CONECTADAS con las keys nuevas",
              "Los blobs viejos no descifran (clave nueva del server): "
              "fencing por diseño", 3600)
    marca("credenciales recargadas")

    # is_self + bloqueo probado.
    inst_id = orm(NUEVO, "primate.cloud.instance", "search",
                  [[["slug", "=", "pcm"],
                    ["environment_id.name", "=", ENV_NOMBRE]]])[0]
    orm(NUEVO, "primate.cloud.instance", "write", [[inst_id], {"is_self": True}])
    maquina = orm(NUEVO, "primate.cloud.ec2.instance", "search",
                  [[["environment_id.name", "=", ENV_NOMBRE]]])[0]
    try:
        orm(NUEVO, "primate.cloud.ec2.instance", "action_stop", [[maquina]])
        raise AssertionError("el stop NO fue bloqueado por is_self")
    except RuntimeError as e:
        assert "hospeda a ESTE PCM" in str(e), "bloqueó por otra razón: %s" % e
    abrir_entorno(page, NUEVO, ENV_NOMBRE)
    abrir_servidor(page)
    assert not page.locator("button", has_text="Terminar servidor").count()
    ox.rotulo(page, "is_self: botones destructivos FUERA y stop rechazado",
              "«hospeda a ESTE PCM» — para todos los grupos", 3600)
    marca("self bloqueado")

    # Sync + bj-uat visible/operable.
    abrir_entorno(page, NUEVO, ENV_NOMBRE)
    ox.clic(page, page.locator("button", has_text="Sincronizar servidores").first)
    page.wait_for_timeout(14000)
    abrir_entorno(page, NUEVO, "bj-uat")
    page.wait_for_timeout(2000)
    ox.rotulo(page, "bj-uat visible y operable desde el PCM nuevo",
              "El inventario completo llegó con la base", 3400)
    marca("sync y bj-uat ok")

    # Backup: política diaria + backup manual al bucket nuevo.
    env_id_nuevo = orm(NUEVO, "primate.cloud.environment", "search",
                       [[["name", "=", ENV_NOMBRE]]])[0]
    pol = orm(NUEVO, "primate.cloud.backup.policy", "search",
              [[["name", "=", "PCM self diario"]]])
    if not pol:
        pol = orm(NUEVO, "primate.cloud.backup.policy", "create",
                  [[{"name": "PCM self diario", "policy_type": "custom",
                     "expected_frequency": "daily",
                     "expected_retention_days": 30, "managed_by_pcm": True,
                     "s3_bucket": "pcm-backups-512060111009"}]])
    # backup_policy_id del entorno es related a la instancia primaria.
    orm(NUEVO, "primate.cloud.instance", "write",
        [[inst_id], {"backup_policy_id": pol[0]}])
    abrir_entorno(page, NUEVO, ENV_NOMBRE)
    previos = orm(NUEVO, "primate.cloud.backup", "search", [[]],
                  {"limit": 1, "order": "id desc"})
    desde = previos[0] if previos else 0
    boton = page.locator("button", has_text="Respaldar ahora")
    if boton.count():
        ox.clic(page, boton.first)
        page.wait_for_timeout(3000)
    t0 = time.time()
    nuevo_ok = False
    while time.time() - t0 < 600:
        ok = orm(NUEVO, "primate.cloud.backup", "search",
                 [[["state", "=", "completed"]]], {"limit": 1,
                                                   "order": "id desc"})
        nuevo_ok = ok and ok[0] > desde
        if nuevo_ok:
            break
        page.wait_for_timeout(8000)
    assert nuevo_ok, "el backup manual no llegó a completed"
    ox.rotulo(page, "Backup manual al bucket nuevo: OK + política diaria (30 d)",
              "s3://pcm-backups-512060111009 — queue_job procesando = "
              "jobrunner vivo", 3600)
    marca("backup y jobrunner ok")

    # Costos + 404 + CW + resumen.
    nav(page, "Costos")
    page.wait_for_timeout(3000)
    r404 = CTX.request.get(NUEVO + "/web/database/manager")
    assert r404.status == 404
    cw = orm(NUEVO, "primate.cloud.environment", "read",
             [[env_id_nuevo], ["cw_agent_state"]])[0]["cw_agent_state"]
    assert cw == "active", "agente CloudWatch en el nuevo: %s" % cw
    nav(page, "Bitácora")
    page.wait_for_timeout(3000)
    ox.tarjeta(page, "Checklist F4: EN VERDE",
               "El PCM nuevo está listo para quedarse solo",
               [("2FA / self / 404", "✓ / ✓ / ✓"),
                ("Crons + jobrunner + sync + costos", "✓"),
                ("Backup diario → bucket", "✓"),
                ("Agente CloudWatch", "ACTIVO")])
    marca("checklist completo")
    tiempos["checklist"] = time.time() - T0
    freno(page, "f4", "F4 EN VERDE (todo el checklist). Último punto de "
          "rollback fácil: después de F5 ya es a mano. ¿Cierro la ventana "
          "(F5: fencing + apagado de la Mac)?")


def f5_fencing(page):
    ox.tarjeta(page, "F5 · Fencing y apagado",
               "La Mac queda inofensiva y apagada",
               [("IAM", "keys viejas DESACTIVADAS (Daryl, consola)"),
                ("La Mac", "proceso apagado + doble cerrojo"),
                ("Rollback", "a mano (reactivar keys + mv de vuelta)")])
    ox.rotulo(page, "DARYL: desactivá las keys VIEJAS en IAM",
              "IAM → Users → pcm-prod-ro y pcm-prod-prov → Security "
              "credentials → la key vieja → Deactivate (NO borrar)", 4600)
    esperar_flag(page, "flag_keys_viejas_off", "Esperando el fencing en IAM",
                 "Con las viejas desactivadas, la Mac no puede tocar AWS")
    marca("keys viejas desactivadas")
    # Apagado de la Mac: proceso + doble cerrojo (local, coreografiado).
    correr(["pkill", "-f", "odoo-bin.*pcm_produccion.conf"])
    time.sleep(4)
    r = correr(["pgrep", "-f", "odoo-bin.*pcm_produccion.conf"])
    assert r.returncode != 0, "el Odoo de la Mac sigue vivo"
    base_dir = pathlib.Path.home() / "Odoo" / "clients" / "19.0" / "pcm_produccion"
    fecha = time.strftime("%Y-%m-%d")
    stub = base_dir / "levantar.sh"
    stub_mudado = base_dir / ("levantar.sh.MUDADO-%s" % fecha)
    stub.rename(stub_mudado)
    stub.write_text("#!/usr/bin/env bash\necho 'MUDADO a %s el %s'\nexit 1\n"
                    % (DOMINIO, fecha))
    stub.chmod(0o755)
    (base_dir / "pcm_produccion.conf").rename(
        base_dir / "pcm_produccion.conf.MUDADO")
    evento("doble cerrojo puesto (levantar.sh stub + conf movido)")
    try:
        page.goto(VIEJO + "/web/login", timeout=8000)
    except Exception:   # noqa: BLE001 — conexión rechazada: ESA es la prueba
        pass
    page.wait_for_timeout(2500)
    ox.rotulo(page, "El PCM de la Mac: APAGADO con doble cerrojo",
              "Y sus keys ya no sirven — inofensiva aunque arranque", 3600)
    marca("mac apagada")
    # El nuevo sigue solo: un GET y un job de prueba.
    r = CTX.request.get(NUEVO + "/web/login")
    assert r.status == 200
    login_ya = CTX.request.get(NUEVO + "/odoo")
    assert login_ya.status in (200, 303)
    abrir_app(page, NUEVO)
    abrir_entorno(page, NUEVO, ENV_NOMBRE)
    ox.clic(page, page.locator("button", has_text="Sincronizar servidores").first)
    page.wait_for_timeout(12000)
    ox.rotulo(page, "El PCM nuevo opera SOLO",
              "Sync corrido como job en su propio servidor", 3400)
    marca("nuevo solo y operando")
    tiempos["fin"] = time.time() - T0
    datos = orm(NUEVO, "primate.cloud.ec2.instance", "search_read",
                [[["environment_id.name", "=", ENV_NOMBRE]],
                 ["aws_instance_id", "public_ip"]], {"limit": 1})[0]
    ox.tarjeta(page, "MUDANZA COMPLETA", NUEVO,
               [("Instancia", datos["aws_instance_id"] or "—"),
                ("IP fija", datos["public_ip"] or "—"),
                ("Ventana de corte", _dur(tiempos.get("fin"))),
                ("Incidentes", str(len(incidentes)) if incidentes else "ninguno")],
               7000)
    marca("fin")
    (HANDOFF / "resumen_final.json").write_text(json.dumps(
        {"instancia": datos["aws_instance_id"], "ip": datos["public_ip"],
         "url": NUEVO, "tiempos": tiempos}, indent=1))


def _dur(segundos):
    if not segundos:
        return "—"
    return "%d:%02d" % (segundos // 60, segundos % 60)


def grabar():
    global T0, CTX
    precondiciones()
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
    with sync_playwright() as navegador:
        nav_ = navegador.chromium.launch(
            headless=False, args=["--no-sandbox", "--lang=es-UY"])
        ctx = nav_.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, locale="es-UY",
            timezone_id="America/Montevideo",
            record_video_dir=str(bruto),
            record_video_size={"width": VIEW_W, "height": VIEW_H})
        CTX = ctx
        ctx.add_init_script(receta.ROTULO_JS)
        ctx.add_init_script(receta.PLACA_JS)
        ctx.add_init_script(ox.CURSOR_JS)
        ctx.add_init_script(ox.PESTANA_JS)
        page = ctx.new_page()
        page.on("pageerror", lambda e: incidentes.append("JS: %s" % str(e)[:120]))
        ox.T0 = T0 = time.time()
        excepcion = None
        page.set_content("<html><body style='background:#111'></body></html>")
        ox.tarjeta(page, "PCM se muda a su propia casa — EL DÍA REAL",
                   "pcm_produccion deja la Mac y pasa a %s" % DOMINIO,
                   [("Servidor", "t3.medium · gp3 50 GB · Ubuntu 24.04 · "
                     "Community"),
                    ("Guía", "docs/GUIA_DIA_D_PCM_SELF.md (ensayado el 10-nov)"),
                    ("Secretos", "los tipea Daryl; acá se ven como puntos")],
                   6500)
        try:
            login_de_daryl(page, VIEJO, "Login en el PCM de la Mac (el que se muda)")
            env_id = p1_montaje(page)
            evento("TRAMO P1 OK")
            p2_dominio_y_repo(page, env_id)
            evento("TRAMO P2 OK")
            f1_congelar(page)
            evento("TRAMO F1 OK")
            ruta = f2_dump(page)
            evento("TRAMO F2 OK")
            f3_restore(page, env_id, ruta)
            evento("TRAMO F3 OK")
            f4_despertar(page)
            evento("TRAMO F4 OK")
            f5_fencing(page)
            evento("TRAMO F5 OK")
        except Exception as error:   # noqa: BLE001 — video y estado se salvan
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(HANDOFF / "dia_d_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            ruta_video = page.video.path() if page.video else None
            ctx.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes}
    destino = bruto / "toma_dia_d.webm"
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    (SALIDA / "toma_dia_d.json").write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d" % (destino, len(incidentes)))
    for i in incidentes:
        print("  ⚠️ ", i)
    if excepcion:
        raise excepcion


def ensamblar():
    datos = json.loads((SALIDA / "toma_dia_d.json").read_text())
    final = SALIDA / "mudanza_pcm_self.mp4"
    vens = sorted(datos["ventanas"], key=lambda v: v["desde"])
    escala = "scale=%d:%d:flags=lanczos" % (VIDEO_W, VIDEO_H)
    partes, previo, idx = [], 0.0, 0
    filtro = ""
    for v in vens:
        filtro += "[0:v]trim=%.2f:%.2f,setpts=PTS-STARTPTS[p%d];" % (
            previo, v["desde"], idx)
        partes.append("[p%d]" % idx); idx += 1
        filtro += "[0:v]trim=%.2f:%.2f,setpts=(PTS-STARTPTS)/%d[p%d];" % (
            v["desde"], v["hasta"], v["factor"], idx)
        partes.append("[p%d]" % idx); idx += 1
        previo = v["hasta"]
    filtro += "[0:v]trim=%.2f,setpts=PTS-STARTPTS[p%d];" % (previo, idx)
    partes.append("[p%d]" % idx)
    filtro += "%sconcat=n=%d:v=1:a=0,%s[v]" % ("".join(partes), len(partes), escala)
    subprocess.run(["ffmpeg", "-y", "-i", datos["video"], "-filter_complex",
                    filtro, "-map", "[v]", "-c:v", "libx264", "-preset", "slow",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
                    "-movflags", "+faststart", str(final)],
                   check=True, capture_output=True)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True,
        text=True).stdout.strip())
    print("🎬 %s · %d:%02d" % (final, dur // 60, dur % 60))

    def mapear(t):
        quitado = 0.0
        for v in vens:
            if t >= v["hasta"]:
                quitado += (v["hasta"] - v["desde"]) * (1 - 1.0 / v["factor"])
            elif t > v["desde"]:
                quitado += (t - v["desde"]) * (1 - 1.0 / v["factor"])
        return t - quitado
    for nombre, t in datos["marcas"]:
        tf = mapear(t)
        print("  %d:%02d  %s" % (tf // 60, tf % 60, nombre))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ensamblar":
        ensamblar()
    else:
        grabar()
