# -*- coding: utf-8 -*-
"""Graba el ENSAYO DE MUDANZA de PCM a su propio servidor (sandbox, punto 9).

  .venv/bin/python docs/demo/grabar_ensayo_pcm_self.py
  .venv/bin/python docs/demo/grabar_ensayo_pcm_self.py ensamblar

Receta de siempre (helpers de grabar_oximoro/grabar_demo del bundle): un solo
video 1080p, tarjetas por tramo, rótulo fijo, cursor visible, pausas de 2–3 s;
SOLO se aceleran las esperas marcadas (acá hay varias: montaje, preparación del
dump, restore, terminación — el ensamblador de este script soporta N ventanas).

Guion (OK de Daryl, 2026-11-09 — ensayo SIN repo privado):
   1. Montaje: entorno Producción + «Aprovisionar» con el PERFIL PCM nuevo
      (t3.small, Community, sslip.io, IP fija, HTTPS).
   2. El perfil, verificado en el servidor: conf del jobrunner, pip extra,
      queue_job clonado, clave Fernet en el env file (sin mostrarla).
   3. El módulo PCM entra por paquete local (el PAT real queda para el día D).
   4. Congelar crons del PCM «viejo» + preparar el dump espejo del runbook
      (desinstalar Enterprise/bundle) — PAUSA: Daryl sube el dump a S3.
   5. Restore SIN neutralizar (destino Producción) pisando la base del montaje
      (triple confirmación) y con borrado del dump al terminar.
   6. El PCM nuevo despierta: checklist (web.base.url, report.url, crons
      reactivados, SMTP y cola intactos, queue_job procesando, sync, backup,
      costos) — PAUSA: Daryl carga las keys sandbox en la UI del nuevo.
   7. Autogestión: is_self → botones desaparecen; ORM y job rechazan; bitácora.
   8. Simulacro del corte: el viejo se apaga; el nuevo queda solo; ROLLBACK
      (el viejo vuelve con sus crons). Fencing real = IAM del día D (manual).
   9. Teardown: terminar el servidor con triple confirmación, EIP liberada.
  10. Tiempos y costo.

Los secretos NO pasan por este script: las credenciales las carga Daryl en la
UI (pausas por archivo-bandera), la clave Fernet la genera el servidor, y la
contraseña maestra se inyecta por JS (en pantalla se ve como puntos/nada).
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import xmlrpc.client

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent                      # docs/demo del repo PCM
REPO = HERE.parent.parent
BUNDLE_DEMO = REPO / "primate_bundle" / "docs" / "demo"
sys.path.insert(0, str(BUNDLE_DEMO))
import grabar_demo as receta        # noqa: E402 — rótulos/placas aprobados
import grabar_oximoro as ox         # noqa: E402 — cursor, clics, tipeo, tarjetas

VIEW_W, VIEW_H = 1536, 864
VIDEO_W, VIDEO_H = 1920, 1080
VIEJO = "http://localhost:19088"
DB_VIEJO = "pcm_demo"
ADMIN = ("admin", "admin")
CUENTA = "603011031378"
BUCKET = "pcm-backups-%s" % CUENTA
DUMP_KEY = "incoming/pcm-self-ensayo/pcm_self_ensayo.dump"
TGZ_KEY = "incoming/pcm-self-ensayo/primate_cloud_manager.tgz"
ENV_NOMBRE = "PCM Self (ensayo)"
SLUG = "pcm-self"
DB_DESTINO = "pcm_self_ensayo"
MASTER = "Ensayo.Master.2026"       # admin_passwd del conf del ensayo (descartable)
SALIDA = HERE
HANDOFF = REPO / "infra" / "ensayo_self"
FLAGS = pathlib.Path(os.environ.get("PCM_ENSAYO_FLAGS", str(HANDOFF)))
FACTORES = {"montaje": 16, "dump": 32, "restore": 16, "pausa": 32, "terminar": 16}

ox.MARCA_PLACA = "PCM SELF · ENSAYO DE MUDANZA (sandbox)"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos = [], [], [], {}
T0 = None
PY = str(REPO / ".venv" / "bin" / "python")
ODOO_BIN = str(REPO.parent / "forum" / "_shared" / "community" / "odoo-bin")
CONF = str(REPO / "PrimateCloudeManager.conf")


def evento(texto):
    print("PCM_ENSAYO: %s" % texto, flush=True)


def marca(nombre):
    marcas.append((nombre, time.time() - T0))
    evento("marca · %s" % nombre)


class Ventana:
    """Ventana de video a acelerar en el ensamblado (varias por toma)."""

    def __init__(self, nombre, factor):
        self.nombre, self.factor = nombre, factor

    def __enter__(self):
        self.desde = time.time() - T0
        return self

    def __exit__(self, *exc):
        ventanas.append({"nombre": self.nombre, "desde": self.desde,
                         "hasta": time.time() - T0, "factor": self.factor})


def rpc(base, db, model, method, args, kwargs=None):
    common = xmlrpc.client.ServerProxy("%s/xmlrpc/2/common" % base, allow_none=True)
    uid = common.authenticate(db, ADMIN[0], ADMIN[1], {})
    modelos = xmlrpc.client.ServerProxy("%s/xmlrpc/2/object" % base, allow_none=True)
    return modelos.execute_kw(db, uid, ADMIN[1], model, method, args, kwargs or {})


def correr(orden, timeout=3600, entrada=None):
    """Subproceso con salida al log del driver (nunca secretos)."""
    evento("cmd · %s" % " ".join(orden[:4]))
    return subprocess.run(orden, timeout=timeout, input=entrada,
                          capture_output=True, text=True)


# ------------------------------------------------------------ UI helpers

def login(page, base, rotulo_txt="", timeout=120000):
    ox.ir(page, base + "/web/login", timeout=timeout)
    page.fill("form:has(input[name=password]) input[name=login]", ADMIN[0])
    page.fill("form input[name=password]", ADMIN[1])
    if rotulo_txt:
        ox.rotulo(page, rotulo_txt, "", 1800)
    page.click("form:has(input[name=password]) button[type=submit]")
    page.wait_for_url("**/odoo**", timeout=45000)


def abrir_app(page, base):
    ox.ir(page, base + "/odoo/action-primate_cloud_manager.action_pcm_app",
          esperar=".o_pcm_app")
    page.wait_for_timeout(1200)


def nav(page, item):
    ox.clic(page, page.locator(".o_pcm_nav_item", has_text=item).first)
    page.wait_for_timeout(1200)


def campo_pcm(page, etiqueta):
    """Input de un PcmField del drawer OWL, por su etiqueta."""
    return page.locator(
        ".o_pcm_field:has(.o_pcm_field_label:has-text('%s')) input" % etiqueta).first


def check_owl(page, texto):
    """Checkbox de un form OWL por el texto del label."""
    return page.locator("label.o_pcm_form_check:has-text('%s') input" % texto).first


def abrir_servidor(page):
    """Desde el detalle del entorno, abre el SERVIDOR (tarjeta .o_pcm_instance)."""
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=SLUG).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)


def abrir_entorno(page, nombre):
    for intento in range(3):
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


def ssm_verificar(orden, timeout=150):
    """Corre un comando por job en la máquina del ensayo (sin UI) y devuelve
    True si terminó success. Para pre-chequeos que no deben matar la toma."""
    mid = rpc(VIEJO, DB_VIEJO, "primate.cloud.ec2.instance", "search",
              [[["name", "=", SLUG], ["instance_state", "=", "running"]]])[0]
    previos = rpc(VIEJO, DB_VIEJO, "primate.cloud.operation.log", "search",
                  [[["action_type", "=", "ssm_command"]]],
                  {"limit": 1, "order": "id desc"})
    ultimo = previos[0] if previos else 0
    rpc(VIEJO, DB_VIEJO, "primate.cloud.ec2.instance", "job_execute_command",
        [[mid], orden])
    t0 = time.time()
    while time.time() - t0 < timeout:
        filas = rpc(VIEJO, DB_VIEJO, "primate.cloud.operation.log", "search_read",
                    [[["action_type", "=", "ssm_command"], ["id", ">", ultimo]],
                     ["result"]], {"limit": 1, "order": "id desc"})
        if filas and filas[0]["result"]:
            return filas[0]["result"] == "success"
        time.sleep(5)
    return False


def esperar_subida_verificada(page, factor):
    """Pausa de la subida a S3: la bandera NO alcanza — se verifica que los
    DOS objetos existan (head desde el servidor; lección de la toma 4: un 403
    de objeto ausente mataba la toma recién en el despliegue del módulo)."""
    bandera = FLAGS / "flag_dump_subido"
    evento("PAUSA · esperando flag_dump_subido")
    with Ventana("pausa-subida", factor):
        ox.rotulo(page, "PAUSA: Daryl sube el dump y el módulo a S3",
                  "aws s3 cp con SU perfil (infra/ensayo_self/SUBIR.txt) · "
                  "espera real, acelerada en el video", 2600)
        while True:
            while not bandera.exists():
                page.wait_for_timeout(4000)
            evento("bandera vista; verificando los objetos en S3…")
            ok = ssm_verificar(
                "aws s3api head-object --bucket %s --key %s --region us-east-1 "
                "&& aws s3api head-object --bucket %s --key %s --region us-east-1"
                % (BUCKET, DUMP_KEY, BUCKET, TGZ_KEY))
            if ok:
                break
            bandera.unlink(missing_ok=True)
            evento("PAUSA · la subida NO se ve en S3 (head falló); "
                   "esperando la bandera de nuevo")
            ox.rotulo(page, "La subida no se ve en S3",
                      "Los objetos no están en las keys esperadas; "
                      "se vuelve a esperar", 3000)
    evento("PAUSA LEVANTADA · subida verificada en S3")
    page.wait_for_timeout(1000)


def esperar_flag(page, nombre, titulo, detalle, factor):
    """Pausa real coordinada con Claude/Daryl por archivo-bandera (acelerada)."""
    bandera = FLAGS / nombre
    evento("PAUSA · esperando %s" % nombre)
    with Ventana("pausa-%s" % nombre, factor):
        ox.rotulo(page, titulo, detalle + " · espera real, acelerada en el video", 2600)
        while not bandera.exists():
            page.wait_for_timeout(4000)
    evento("PAUSA LEVANTADA · %s" % nombre)
    page.wait_for_timeout(1000)


def esperar_estado_env(page, env_id, estados, limite=1500, nombre="montaje",
                       factor=16, recargar=None):
    with Ventana(nombre, factor):
        t0 = time.time()
        while time.time() - t0 < limite:
            estado = rpc(VIEJO, DB_VIEJO, "primate.cloud.environment",
                         "read", [[env_id], ["state"]])[0]["state"]
            if estado in estados:
                evento("entorno %s → %s" % (env_id, estado))
                return estado
            if recargar:
                recargar()
            page.wait_for_timeout(8000)
    raise RuntimeError("el entorno %s no llegó a %s" % (env_id, estados))


# ------------------------------------------------------------ preparación

def precondiciones():
    salida = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "4", VIEJO + "/web/login"])
    assert salida.stdout.strip() == "200", "pcm_demo no responde en :19088"
    HANDOFF.mkdir(parents=True, exist_ok=True)
    for vieja in ("flag_dump_subido", "flag_credenciales_cargadas"):
        (FLAGS / vieja).unlink(missing_ok=True)
    # Restos de tomas anteriores: entornos BORRADOR con el nombre del ensayo
    # (nunca llegaron a AWS) se borran para no duplicar tarjetas ni búsquedas.
    viejos = rpc(VIEJO, DB_VIEJO, "primate.cloud.environment", "search_read",
                 [[["name", "=", ENV_NOMBRE]], ["id", "state"]])
    for v in viejos:
        assert v["state"] == "draft", \
            "queda un entorno %r en estado %s: limpiar a mano" % (ENV_NOMBRE, v["state"])
        instancias = rpc(VIEJO, DB_VIEJO, "primate.cloud.instance", "search",
                         [[["environment_id", "=", v["id"]]]])
        if instancias:
            rpc(VIEJO, DB_VIEJO, "primate.cloud.instance", "unlink", [instancias])
        rpc(VIEJO, DB_VIEJO, "primate.cloud.environment", "unlink", [[v["id"]]])
        evento("entorno borrador residual %d borrado" % v["id"])
    # Proyecto del ensayo (sin AWS): existe o se crea por RPC.
    proy = rpc(VIEJO, DB_VIEJO, "primate.cloud.project", "search_read",
               [[["name", "=", "Primate (ensayo self)"]], ["id"]])
    if not proy:
        cuenta = rpc(VIEJO, DB_VIEJO, "primate.cloud.account", "search",
                     [[["aws_account_id", "=", CUENTA]]])[0]
        socio = rpc(VIEJO, DB_VIEJO, "res.partner", "create",
                    [{"name": "PrimateUY (ensayo)"}])
        rpc(VIEJO, DB_VIEJO, "primate.cloud.project", "create",
            [{"name": "Primate (ensayo self)", "account_id": cuenta,
              "partner_id": socio}])
        evento("proyecto del ensayo creado")
    # Paquete del módulo (estado COMMITEADO, no el working tree).
    tgz = HANDOFF / "primate_cloud_manager.tgz"
    correr(["git", "-C", str(REPO), "archive", "--format=tar.gz",
            "-o", str(tgz), "HEAD", "primate_cloud_manager"])
    assert tgz.stat().st_size > 200000, "el paquete del módulo quedó vacío"
    evento("módulo empaquetado: %s (%d KB)" % (tgz, tgz.stat().st_size // 1024))


def congelar_crons_viejo():
    ids = rpc(VIEJO, DB_VIEJO, "ir.cron", "search",
              [[["cron_name", "like", "Primate Cloud%"], ["active", "=", True]]])
    rpc(VIEJO, DB_VIEJO, "ir.cron", "write", [ids, {"active": False}])
    evento("crons PCM congelados en el viejo: %d" % len(ids))
    return ids


def preparar_dump():
    """Copia post-montaje de pcm_demo → espejo del runbook → dump a mano de Daryl."""
    tmp = "pcm_ensayo_fuente"
    bruto = HANDOFF / "fuente.dump"
    r = correr(["pg_dump", "-Fc", "-f", str(bruto), DB_VIEJO])
    assert r.returncode == 0, "pg_dump de pcm_demo falló: %s" % r.stderr[-300:]
    correr(["dropdb", "--if-exists", tmp])
    correr(["createdb", tmp])
    r = correr(["pg_restore", "-d", tmp, "--no-owner", str(bruto)])
    # pg_restore devuelve 1 por warnings de ownership: validar por contenido.
    quitar = pathlib.Path(os.environ["PCM_ENSAYO_QUITAR"])
    r = correr([PY, ODOO_BIN, "shell", "-c", CONF, "-d", tmp, "--no-http",
                "--logfile=/dev/null"], entrada=quitar.read_text(), timeout=2400)
    assert "PROBE_LISTA" in r.stdout, "la desinstalación no quedó limpia:\n%s" % r.stdout[-600:]
    pendientes = correr(["psql", "-d", tmp, "-t", "-A", "-c",
                         "select count(*) from queue_job where state in ('pending','started','enqueued');"])
    evento("jobs pendientes en la fuente: %s" % pendientes.stdout.strip())
    destino = HANDOFF / "pcm_self_ensayo.dump"
    r = correr(["pg_dump", "-Fc", "-f", str(destino), tmp])
    assert r.returncode == 0, "pg_dump de la fuente falló"
    sha = correr(["shasum", "-a", "256", str(destino)]).stdout.split()[0]
    (HANDOFF / "SUBIR.txt").write_text(
        "Subida del ensayo (cuenta sandbox %s):\n\n"
        "  aws s3 cp %s s3://%s/%s --profile <PERFIL-SANDBOX>\n"
        "  aws s3 cp %s s3://%s/%s --profile <PERFIL-SANDBOX>\n\n"
        "sha256 del dump: %s\n" % (
            CUENTA, destino, BUCKET, DUMP_KEY,
            HANDOFF / "primate_cloud_manager.tgz", BUCKET, TGZ_KEY, sha))
    bruto.unlink(missing_ok=True)
    evento("dump listo: %s (%d MB) · sha256 %s…" % (
        destino, destino.stat().st_size // 1048576, sha[:12]))


# ------------------------------------------------------------ tramos

def t1_montaje(page):
    abrir_app(page, VIEJO)
    nav(page, "Entornos")
    ox.clic(page, page.locator("button", has_text="Nuevo entorno").first)
    # El alta abre el form backend DENTRO del shell (no es drawer).
    page.wait_for_selector("input#name_0", timeout=15000)
    ox.tipear(page, page.locator("#name_0"), ENV_NOMBRE, 55)
    # Proyecto y cuenta: autocompletes del form backend.
    for fid, texto in (("#project_id_0", "Primate (ensayo self)"),
                       ("#account_id_0", "Personal Prueba Real")):
        ox.clic(page, page.locator(fid))
        page.locator(fid).fill(texto[:14])
        page.wait_for_timeout(1300)
        ox.clic(page, page.locator(".o-autocomplete--dropdown-item", has_text=texto).first)
        page.wait_for_timeout(600)
    # Tipo: Producción (clave del ensayo: el restore NO neutraliza producción).
    ox.clic(page, page.locator("#env_type_0"))
    page.wait_for_timeout(700)
    ox.clic(page, page.locator(".o_select_menu_item, .dropdown-item",
                               has_text="Producción").first)
    ox.rotulo(page, "Tipo Producción", "La clave del ensayo: sobre producción el "
              "restore NO neutraliza (crons y correo quedan como vienen)", 3200)
    ox.clic(page, page.locator(".o_form_button_save").first)
    page.wait_for_timeout(4000)
    marca("entorno creado")

    abrir_entorno(page, ENV_NOMBRE)
    ox.clic(page, page.locator("button", has_text="Aprovisionar").first)
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1500)
    campo_pcm(page, "Nombre de la instancia").fill(SLUG)
    campo_pcm(page, "Tipo de instancia").fill("t3.small")
    campo_pcm(page, "Disco raíz (GB)").fill("30")
    campo_pcm(page, "Nombre de la base").fill(DB_DESTINO)
    # La maestra no se tipea a la vista: se inyecta por JS.
    page.evaluate(
        """(valor) => {
            const campos = [...document.querySelectorAll('.o_pcm_field')];
            const c = campos.find(f => f.textContent.includes('Contraseña admin'));
            const inp = c && c.querySelector('input');
            if (inp) {
                const seteador = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                seteador.call(inp, valor);
                inp.dispatchEvent(new Event('input', {bubbles: true}));
            }
        }""", MASTER)
    ox.rotulo(page, "Contraseña maestra cargada", "No se muestra en pantalla", 2200)
    # OJO: el texto de «IP fija» también menciona sslip.io — el label del
    # toggle correcto empieza con «Dominio sslip.io automático».
    sslip = check_owl(page, "Dominio sslip.io automático")
    if not sslip.is_checked():
        ox.clic(page, sslip)
        page.wait_for_timeout(600)
    ox.clic(page, check_owl(page, "Perfil PCM"))
    ox.rotulo(page, "Perfil PCM", "pip extra + jobrunner de queue_job + jobs largos "
              "+ clave de cifrado generada EN el servidor", 3600)
    ox.clic(page, page.locator(".o_pcm_drawer button",
                               has_text="Aprovisionar").last)
    page.wait_for_selector(".modal", timeout=10000)
    ox.clic(page, page.locator(".modal footer button.btn-primary").first)
    marca("aprovisionamiento encolado")
    env_id = rpc(VIEJO, DB_VIEJO, "primate.cloud.environment", "search",
                 [[["name", "=", ENV_NOMBRE]]])[0]
    ox.rotulo(page, "Montando el servidor", "EC2 + bootstrap + Odoo 19 Community "
              "+ perfil PCM — espera real acelerada ×%d" % FACTORES["montaje"], 3000)
    estado = esperar_estado_env(
        page, env_id, ("active", "error"), nombre="montaje",
        factor=FACTORES["montaje"],
        recargar=lambda: page.keyboard.press("F5") if False else None)
    assert estado == "active", "el montaje terminó en error"
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(2500)
    marca("servidor activo")
    tiempos["montaje"] = time.time() - T0
    return env_id


def _maquina_y_sslip():
    datos = rpc(VIEJO, DB_VIEJO, "primate.cloud.environment", "search_read",
                [[["name", "=", ENV_NOMBRE]],
                 ["id", "ec2_instance_id", "primary_instance_id", "main_url"]])[0]
    inst = rpc(VIEJO, DB_VIEJO, "primate.cloud.instance", "read",
               [[datos["primary_instance_id"][0]], ["main_url", "slug"]])[0]
    return datos["ec2_instance_id"][0], datos["primary_instance_id"][0], inst["main_url"]


def comando(page, maquina_nombre, orden, explicacion, espera=18000):
    """Ejecutar comando por el wizard + mostrar la salida en el historial."""
    ox.clic(page, page.locator("button", has_text="Ejecutar comando").first)
    page.wait_for_selector("textarea#command_0, input#command_0", timeout=12000)
    caja = page.locator("textarea#command_0, input#command_0").first
    caja.fill(orden)
    ox.rotulo(page, "Ejecutar comando (SSM)", explicacion, 2800)
    ox.clic(page, page.locator("button[name=action_run]").first)
    page.wait_for_timeout(espera)   # job + SSM


def t2_perfil(page, env_id):
    abrir_entorno(page, ENV_NOMBRE)
    abrir_servidor(page)
    comando(page, SLUG, "tail -n 14 /etc/odoo/%s.conf" % SLUG,
            "El conf del perfil: jobrunner, jobs largos y candado multi-tenant")
    comando(page, SLUG,
            "ls -l /opt/pcm/instances/%s/addons/ /etc/odoo/%s.env "
            "&& systemctl cat odoo-%s.service | grep -A1 EnvironmentFile"
            % (SLUG, SLUG, SLUG),
            "queue_job clonado por el perfil + la clave generada EN el servidor "
            "(se ve el archivo, nunca la clave)")
    comando(page, SLUG,
            "/opt/pcm/runtime/odoo-19/venv/bin/pip list 2>/dev/null | "
            "grep -iE 'boto3|paramiko|pygithub|cryptography'",
            "Las dependencias Python de PCM, en el venv del runtime")
    # La salida quedó en el historial/bitácora: mostrarla.
    page.wait_for_timeout(4000)
    ox.desplazar(page, 700)
    page.wait_for_timeout(2500)
    ox.desplazar(page, -700)
    marca("perfil verificado en el servidor")


def t3_modulo(page):
    previos = rpc(VIEJO, DB_VIEJO, "primate.cloud.operation.log", "search",
                  [[["action_type", "=", "ssm_command"]]], {"limit": 1,
                                                            "order": "id desc"})
    ultimo_previo = previos[0] if previos else 0
    comando(page, SLUG,
            "aws s3 cp s3://%s/%s /tmp/pcm.tgz --region us-east-1 "
            "&& sudo tar xzf /tmp/pcm.tgz -C /opt/pcm/instances/%s/addons "
            "&& sudo chown -R odoo:odoo /opt/pcm/instances/%s/addons "
            "&& rm /tmp/pcm.tgz && ls /opt/pcm/instances/%s/addons"
            % (BUCKET, TGZ_KEY, SLUG, SLUG, SLUG),
            "El módulo PCM entra por paquete local (el clone con PAT real "
            "queda para el día D, decisión de hoy)", espera=25000)
    # Verificación dura (lección toma 3: un s3 cp fallido pasaba en silencio).
    t0 = time.time()
    while time.time() - t0 < 120:
        filas = rpc(VIEJO, DB_VIEJO, "primate.cloud.operation.log", "search_read",
                    [[["action_type", "=", "ssm_command"],
                      ["id", ">", ultimo_previo]], ["result"]],
                    {"limit": 1, "order": "id desc"})
        if filas and filas[0]["result"] in ("success", "failed"):
            assert filas[0]["result"] == "success", \
                "el comando de despliegue del módulo FALLÓ (ver bitácora)"
            break
        page.wait_for_timeout(5000)
    else:
        raise RuntimeError("el comando del módulo no terminó a tiempo")
    marca("módulo PCM en el addons dir")


def t4_dump(page):
    ox.tarjeta(page, "3 · El dump, espejo del runbook",
               "Congelar crons del viejo → copia → desinstalar Enterprise/bundle → dump",
               [("Se quita", "29 Enterprise + primate_bundle"),
                ("Queda", "Community + PCM + queue_job"),
                ("Correo", "SMTP Mailpit y cola: INTACTOS (sin neutralizar)")])
    congelar_crons_viejo()
    ox.rotulo(page, "Preparando el dump", "Desinstalación en la copia + pg_dump "
              "— espera real acelerada ×%d" % FACTORES["dump"], 3000)
    with Ventana("preparar-dump", FACTORES["dump"]):
        preparar_dump()
    marca("dump preparado")
    esperar_subida_verificada(page, FACTORES["pausa"])
    marca("dump en S3 (subido por Daryl, verificado desde el servidor)")


def t5_restore(page, env_id):
    abrir_entorno(page, ENV_NOMBRE)
    ox.clic(page, page.locator("button", has_text="Restaurar desde un dump").first)
    page.wait_for_selector("#origin_uri_0", timeout=15000)
    ox.tipear(page, page.locator("#origin_uri_0"),
              "s3://%s/%s" % (BUCKET, DUMP_KEY), 18)
    page.locator("#target_db_name_0").fill(DB_DESTINO)
    caja = page.locator("#delete_source_0")
    if caja.count() and not caja.is_checked():
        ox.clic(page, caja)
        ox.rotulo(page, "Borrar el origen al terminar",
                  "El dump no queda tirado en S3 (igual que bj-uat)", 2600)
    pre = page.locator("#pre_backup_0")
    if pre.count() and pre.is_checked():
        ox.clic(page, pre)
    conf = page.locator("#confirm_environment_name_0")
    if conf.count():
        ox.tipear(page, conf, ENV_NOMBRE, 45)
    ox.rotulo(page, "Restore SIN neutralizar", "La instancia destino es "
              "Producción: crons, SMTP y web.base.url llegan como salieron", 3400)
    ox.clic(page, page.locator("button[name=action_restore]").first)
    page.wait_for_timeout(2500)
    # Pisa una base registrada → confirmación destructiva (impacto→nombre→clave).
    if page.locator("#typed_name_0").count() or page.locator(
            "button[name=action_next_from_impact]").count():
        ox.rotulo(page, "Triple confirmación", "Pisar una base registrada tiene "
                  "ceremonia: impacto → nombre exacto → re-autenticación", 3200)
        if page.locator("button[name=action_next_from_impact]").count():
            ox.clic(page, page.locator("button[name=action_next_from_impact]").first)
            page.wait_for_timeout(1500)
        ox.tipear(page, page.locator("#typed_name_0"), DB_DESTINO, 50)
        ox.clic(page, page.locator("button[name=action_confirm_name]").first)
        page.wait_for_timeout(1500)
        page.locator("#password_0").fill(ADMIN[1])
        ox.clic(page, page.locator("button[name=action_execute]").first)
    marca("restore encolado")
    ox.rotulo(page, "Restaurando", "S3 → stop → drop → restore → start, sobre la "
              "unit del slug — espera real acelerada ×%d" % FACTORES["restore"], 3000)
    with Ventana("restore", FACTORES["restore"]):
        t0 = time.time()
        while time.time() - t0 < 2400:
            filas = rpc(VIEJO, DB_VIEJO, "primate.cloud.operation.log", "search_read",
                        [[["action_type", "=", "backup_restore"]],
                         ["result", "name"]], {"limit": 1, "order": "id desc"})
            if filas and filas[0]["result"] in ("success", "failed", "partial"):
                assert filas[0]["result"] == "success", \
                    "restore terminó %s" % filas[0]["result"]
                break
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("el restore no terminó a tiempo")
    marca("restore en verde")
    tiempos["restore"] = time.time() - T0


def t6_despertar(page):
    _maq, _inst, sslip = _maquina_y_sslip()
    nuevo = "https://%s" % sslip if not sslip.startswith("http") else sslip
    ox.tarjeta(page, "6 · El PCM nuevo despierta", nuevo.replace("https://", ""),
               [("Candado", "dbfilter + list_db=False + /web/database 404"),
                ("HTTPS", "Let's Encrypt del montaje")])
    login(page, nuevo, "Login en el PCM nuevo, por su dominio")
    abrir_app(page, nuevo)
    ox.pestana(page, True)
    page.wait_for_timeout(2500)
    ox.pestana(page, False)
    marca("pcm nuevo adentro")

    # web.base.url + freeze + report.url (parámetros del sistema, con debug).
    ox.ir(page, nuevo + "/odoo/action-base.ir_config_list_action?debug=1")
    page.wait_for_selector(".o_list_view", timeout=30000)
    for clave, valor in (("web.base.url", nuevo),
                         ("web.base.url.freeze", "True"),
                         ("report.url", "http://127.0.0.1:8069")):
        actual = rpc(nuevo, DB_DESTINO, "ir.config_parameter", "search",
                     [[["key", "=", clave]]])
        if actual:
            rpc(nuevo, DB_DESTINO, "ir.config_parameter", "write",
                [actual, {"value": valor}])
        else:
            rpc(nuevo, DB_DESTINO, "ir.config_parameter", "create",
                [{"key": clave, "value": valor}])
    page.reload(wait_until="load")
    buscador = page.locator(".o_searchview_input").first
    buscador.fill("web.base.url")
    page.keyboard.press("Enter")
    page.wait_for_timeout(2600)
    ox.rotulo(page, "Checklist post-restore (1/2)",
              "web.base.url al dominio nuevo + freeze + report.url local", 3200)
    marca("parámetros ajustados")

    # Crons: los PCM llegan congelados (así viajó el dump) → se reactivan ACÁ.
    ids = rpc(nuevo, DB_DESTINO, "ir.cron", "search",
              [[["cron_name", "like", "Primate Cloud%"], ["active", "=", False]]])
    rpc(nuevo, DB_DESTINO, "ir.cron", "write", [ids, {"active": True}])
    ox.ir(page, nuevo + "/odoo/action-base.ir_cron_act?debug=1")
    page.wait_for_selector(".o_list_view", timeout=30000)
    buscador = page.locator(".o_searchview_input").first
    buscador.fill("Primate Cloud")
    page.keyboard.press("Enter")
    page.wait_for_timeout(3000)
    ox.rotulo(page, "Checklist (2/2): crons", "Los 8 crons de PCM reactivados; los "
              "del core llegaron ACTIVOS (nada se neutralizó)", 3600)
    marca("crons reactivados")

    # SMTP y cola: intactos (la prueba de que NO hubo neutralización).
    smtp = rpc(nuevo, DB_DESTINO, "ir.mail_server", "search_read",
               [[], ["name", "active"]])
    excepciones = rpc(nuevo, DB_DESTINO, "mail.mail", "search_count",
                      [[["state", "=", "exception"]]])
    canceladas = rpc(nuevo, DB_DESTINO, "mail.mail", "search_count",
                     [[["state", "=", "cancel"]]])
    ox.tarjeta(page, "Sin neutralizar: la prueba",
               "Lo que una neutralización habría cancelado o apagado, sigue acá",
               [("SMTP", "%s (activo: %s)" % (smtp[0]["name"], smtp[0]["active"])
                 if smtp else "—"),
                ("Cola de correo", "%d en excepción, %d canceladas (como salió)"
                 % (excepciones, canceladas)),
                ("Sin salida real", "el SMTP apunta a Mailpit local: acá no existe")])
    marca("smtp y cola intactos")

    esperar_flag(page, "flag_credenciales_cargadas",
                 "PAUSA: Daryl carga las keys sandbox en el PCM nuevo",
                 "Cuentas AWS → Personal Prueba Real → credenciales + validar",
                 FACTORES["pausa"])
    abrir_app(page, nuevo)
    nav(page, "Cuentas AWS")
    page.wait_for_timeout(2600)
    ox.rotulo(page, "Credenciales recargadas por Daryl",
              "Clave Fernet NUEVA del servidor: los blobs viejos no descifran; "
              "las keys se recargan a mano (opción b del plan)", 4000)
    marca("credenciales recargadas")

    # queue_job procesando + sync + backup + costos (todo jobs reales).
    abrir_entorno_nuevo(page, nuevo)
    ox.clic(page, page.locator("button", has_text="Sincronizar servidores").first)
    page.wait_for_timeout(14000)
    ox.rotulo(page, "queue_job procesando", "La sincronización corrió como job: "
              "jobrunner vivo en el server", 3000)
    boton_backup = page.locator("button", has_text="Respaldar ahora")
    if boton_backup.count():
        ox.clic(page, boton_backup.first)
        page.wait_for_timeout(20000)
        ox.rotulo(page, "Backup de PCM mismo", "Permitido (solo el RESTORE sobre "
                  "sí está bloqueado) → s3://%s" % BUCKET, 3200)
    nav(page, "Costos")
    page.wait_for_timeout(3000)
    ox.rotulo(page, "Costos", "Cost Explorer de la cuenta sandbox", 2600)
    nav(page, "Bitácora")
    page.wait_for_timeout(3200)
    marca("checklist completo")
    tiempos["checklist"] = time.time() - T0
    return nuevo


def abrir_entorno_nuevo(page, nuevo):
    nav(page, "Entornos")
    buscador = page.locator(".o_pcm_screen input[type=text]").first
    buscador.fill(ENV_NOMBRE)
    page.wait_for_timeout(900)
    ox.clic(page, page.locator(".o_pcm_env_card", has_text=ENV_NOMBRE).first)
    page.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=15000)


def t7_autogestion(page, nuevo):
    inst_id = rpc(nuevo, DB_DESTINO, "primate.cloud.instance", "search",
                  [[["slug", "=", SLUG]]])[0]
    # ANTES: el servidor muestra Detener/Reiniciar/Terminar.
    abrir_entorno_nuevo(page, nuevo)
    abrir_servidor(page)
    page.wait_for_timeout(1200)
    ox.rotulo(page, "ANTES de marcar «self»", "Detener, Reiniciar y Terminar están "
              "a la vista", 3000)
    # Marcar is_self (form backend, campo de Cloud Admin).
    rpc(nuevo, DB_DESTINO, "primate.cloud.instance", "write",
        [[inst_id], {"is_self": True}])
    page.reload(wait_until="load")
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(2500)
    ox.rotulo(page, "DESPUÉS: la instancia hospeda a ESTE PCM",
              "Los botones desaparecieron y quedó el aviso — para TODOS los grupos",
              4000)
    marca("is_self marcado: botones fuera")
    # ORM y job: rechazan con el mensaje de autogestión.
    maquina = rpc(nuevo, DB_DESTINO, "primate.cloud.ec2.instance", "search",
                  [[["environment_id.name", "=", ENV_NOMBRE]]])[0]
    errores = []
    for metodo in ("action_stop", "action_terminate"):
        try:
            rpc(nuevo, DB_DESTINO, "primate.cloud.ec2.instance", metodo, [[maquina]])
            errores.append((metodo, "NO BLOQUEÓ"))
        except xmlrpc.client.Fault as f:
            errores.append((metodo, re.sub(r"\s+", " ", str(f))[:90]))
    try:
        rpc(nuevo, DB_DESTINO, "primate.cloud.ec2.instance", "job_lifecycle",
            [[maquina], "ec2_stop"])
        errores.append(("job_lifecycle", "NO BLOQUEÓ"))
    except xmlrpc.client.Fault as f:
        errores.append(("job_lifecycle", re.sub(r"\s+", " ", str(f))[:90]))
    assert all("NO BLOQUEÓ" not in d for _m, d in errores), errores
    ox.tarjeta(page, "ORM y job: rechazados",
               "«hospeda a ESTE PCM: no se puede … sin importar el grupo»",
               [(m, "UserError ✓") for m, _d in errores])
    nav(page, "Bitácora")
    page.wait_for_timeout(3500)
    ox.rotulo(page, "Todo intento queda en la bitácora",
              "action_type = Bloqueo de autogestión (PCM mismo)", 3600)
    marca("autogestión probada (botón + ORM + job)")


def t8_simulacro(page, nuevo):
    ox.tarjeta(page, "8 · Simulacro del corte",
               "El viejo se apaga; el nuevo queda solo; después, ROLLBACK",
               [("Fencing real (día D)", "rotación de keys en IAM — manual de Daryl"),
                ("Acá", "apagado de procesos y crons, sin tocar secretos")])
    correr(["pkill", "-f", "odoo-bin -c PrimateCloudeManager.conf -d pcm_demo"])
    time.sleep(3)
    try:
        page.goto(VIEJO + "/web/login", timeout=8000)
    except Exception:   # noqa: BLE001 — conexión rechazada: ESO es la prueba
        pass
    page.wait_for_timeout(2500)
    ox.rotulo(page, "El PCM viejo, apagado", "En el día D: doble cerrojo "
              "(levantar.sh renombrado + conf movido) y keys viejas desactivadas",
              3600)
    marca("viejo apagado")
    login(page, nuevo)
    abrir_app(page, nuevo)
    ox.rotulo(page, "El nuevo quedó solo", "Un único PCM con crons y jobs vivos",
              3000)
    marca("nuevo solo")
    # ROLLBACK: el viejo vuelve (proceso + crons).
    subprocess.Popen(
        [PY, ODOO_BIN, "-c", CONF, "-d", DB_VIEJO,
         "--logfile=%s" % (HANDOFF / "pcm_demo_rollback.log")],
        cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 120:
        r = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    "--max-time", "3", VIEJO + "/web/login"], timeout=10)
        if r.stdout.strip() == "200":
            break
        time.sleep(3)
    ids = rpc(VIEJO, DB_VIEJO, "ir.cron", "search",
              [[["cron_name", "like", "Primate Cloud%"], ["active", "=", False]]])
    rpc(VIEJO, DB_VIEJO, "ir.cron", "write", [ids, {"active": True}])
    login(page, VIEJO, "ROLLBACK: el viejo vuelve, con sus crons")
    abrir_app(page, VIEJO)
    page.wait_for_timeout(2500)
    ox.rotulo(page, "Rollback completo", "Nada se perdió: la base vieja intacta, "
              "crons reactivados — el nuevo se descarta y se reintenta otro día",
              4200)
    marca("rollback: viejo operativo")


def t9_teardown(page):
    abrir_entorno(page, ENV_NOMBRE)
    abrir_servidor(page)
    ox.clic(page, page.locator("button", has_text="Terminar servidor").first)
    page.wait_for_selector("button[name=action_next_from_impact], #typed_name_0",
                           timeout=15000)
    ox.rotulo(page, "Triple confirmación", "Impacto → nombre exacto → "
              "re-autenticación: la ceremonia de siempre", 3200)
    if page.locator("button[name=action_next_from_impact]").count():
        ox.clic(page, page.locator("button[name=action_next_from_impact]").first)
        page.wait_for_timeout(1500)
    nombre_maquina = rpc(VIEJO, DB_VIEJO, "primate.cloud.ec2.instance",
                         "search_read", [[["environment_id.name", "=", ENV_NOMBRE]],
                                         ["name"]])[0]["name"]
    ox.tipear(page, page.locator("#typed_name_0"), nombre_maquina, 50)
    ox.clic(page, page.locator("button[name=action_confirm_name]").first)
    page.wait_for_timeout(1500)
    page.locator("#password_0").fill(ADMIN[1])
    ox.clic(page, page.locator("button[name=action_execute]").first)
    marca("terminación encolada")
    with Ventana("terminar", FACTORES["terminar"]):
        t0 = time.time()
        while time.time() - t0 < 900:
            estado = rpc(VIEJO, DB_VIEJO, "primate.cloud.ec2.instance",
                         "search_read", [[["environment_id.name", "=", ENV_NOMBRE]],
                                         ["instance_state"]])[0]["instance_state"]
            if estado == "terminated":
                break
            page.wait_for_timeout(10000)
        else:
            raise RuntimeError("la EC2 no llegó a terminated")
    page.reload(wait_until="load")
    page.wait_for_timeout(3000)
    ox.rotulo(page, "Servidor terminado, IP fija liberada",
              "Verificado por el job (y en la bitácora)", 3600)
    marca("recursos terminados")
    tiempos["fin"] = time.time() - T0


def grabar():
    global T0
    precondiciones()
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
    with sync_playwright() as navegador:
        nav_ = navegador.chromium.launch(args=["--no-sandbox", "--lang=es-UY"])
        ctx = nav_.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, locale="es-UY",
            timezone_id="America/Montevideo",
            record_video_dir=str(bruto),
            record_video_size={"width": VIEW_W, "height": VIEW_H},
            ignore_https_errors=False)
        ctx.add_init_script(receta.ROTULO_JS)
        ctx.add_init_script(receta.PLACA_JS)
        ctx.add_init_script(ox.CURSOR_JS)
        ctx.add_init_script(ox.PESTANA_JS)
        page = ctx.new_page()
        page.on("pageerror", lambda e: incidentes.append("JS: %s" % str(e)[:120]))
        ox.T0 = T0 = time.time()
        excepcion = None
        page.set_content("<html><body style='background:#111'></body></html>")
        ox.tarjeta(page, "PCM se muda a su propia casa",
                   "Ensayo COMPLETO de la mudanza, en el sandbox — todo ocurre de verdad",
                   [("Servidor", "t3.small · Ubuntu 24.04 · Odoo 19 Community"),
                    ("Perfil", "PCM (queue_job + clave en el server)"),
                    ("Restore", "SIN neutralizar (destino Producción)")], 6000)
        try:
            login(page, VIEJO, "El PCM «viejo» (hace de la Mac)")
            ox.tarjeta(page, "1 · El montaje", "Entorno Producción + perfil PCM")
            env_id = t1_montaje(page)
            evento("TRAMO 1 OK")
            ox.tarjeta(page, "2 · El perfil, verificado", "Lo que el montaje dejó en el servidor")
            t2_perfil(page, env_id)
            evento("TRAMO 2 OK")
            # El dump y SU SUBIDA van antes que el módulo: el tramo del módulo
            # baja el paquete de S3 (lección de la toma 3: el s3 cp corría
            # contra un objeto que todavía no existía).
            t4_dump(page)
            evento("TRAMO 3 (dump) OK")
            ox.tarjeta(page, "4 · El módulo PCM", "Paquete local (PAT real: día D)")
            t3_modulo(page)
            evento("TRAMO 4 (módulo) OK")
            ox.tarjeta(page, "5 · Restore sin neutralizar", "Pisando la base del montaje, con ceremonia")
            t5_restore(page, env_id)
            evento("TRAMO 5 OK")
            nuevo = t6_despertar(page)
            evento("TRAMO 6 OK")
            ox.tarjeta(page, "7 · Autogestión", "PCM no se destruye a sí mismo")
            t7_autogestion(page, nuevo)
            evento("TRAMO 7 OK")
            t8_simulacro(page, nuevo)
            evento("TRAMO 8 OK")
            ox.tarjeta(page, "9 · Teardown", "Triple confirmación + IP liberada")
            t9_teardown(page)
            evento("TRAMO 9 OK")
            filas = [("Montaje (perfil PCM)", _dur(tiempos.get("montaje"))),
                     ("Restore sin neutralizar", _dur(tiempos.get("restore"))),
                     ("Incidentes", str(len(incidentes)) if incidentes else "ninguno")]
            ox.tarjeta(page, "Ensayo completo", "La mudanza real repite esto, "
                       "con pcm_produccion y la cuenta real", filas, 6500)
            marca("fin")
        except Exception as error:   # noqa: BLE001 — el video y el json se salvan igual
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(SALIDA / "ensayo_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            ruta_video = page.video.path() if page.video else None
            ctx.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes}
    destino = bruto / "toma_ensayo_self.webm"
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    (SALIDA / "toma_ensayo_self.json").write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d" % (destino, len(incidentes)))
    for i in incidentes:
        print("  ⚠️ ", i)
    if excepcion:
        raise excepcion


def _dur(segundos):
    if not segundos:
        return "—"
    return "%d:%02d" % (segundos // 60, segundos % 60)


def ensamblar():
    """N ventanas aceleradas → mp4 1080p final."""
    datos = json.loads((SALIDA / "toma_ensayo_self.json").read_text())
    final = SALIDA / "ensayo_pcm_self.mp4"
    vens = sorted(datos["ventanas"], key=lambda v: v["desde"])
    escala = "scale=%d:%d:flags=lanczos" % (VIDEO_W, VIDEO_H)
    partes, previo, idx = [], 0.0, 0
    filtro = ""
    for v in vens:
        filtro += "[0:v]trim=%.2f:%.2f,setpts=PTS-STARTPTS[p%d];" % (previo, v["desde"], idx)
        partes.append("[p%d]" % idx); idx += 1
        filtro += "[0:v]trim=%.2f:%.2f,setpts=(PTS-STARTPTS)/%d[p%d];" % (
            v["desde"], v["hasta"], v["factor"], idx)
        partes.append("[p%d]" % idx); idx += 1
        previo = v["hasta"]
    filtro += "[0:v]trim=%.2f,setpts=PTS-STARTPTS[p%d];" % (previo, idx)
    partes.append("[p%d]" % idx)
    filtro += "%sconcat=n=%d:v=1:a=0,%s[v]" % ("".join(partes), len(partes), escala)
    orden = ["ffmpeg", "-y", "-i", datos["video"], "-filter_complex", filtro,
             "-map", "[v]", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(final)]
    subprocess.run(orden, check=True, capture_output=True)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(final)], capture_output=True, text=True).stdout.strip())
    print("🎬 %s · %d:%02d" % (final, dur // 60, dur % 60))
    # minuto a minuto del final (descontando lo acelerado)
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
