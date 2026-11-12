# -*- coding: utf-8 -*-
"""Demo CORTA del Monitoreo CloudWatch (pedido INCO) — sandbox, video 1080p.

  .venv/bin/python docs/demo/grabar_demo_cw.py
  .venv/bin/python docs/demo/grabar_demo_cw.py ensamblar

Guion:
  1. Montaje efímero (t3.small, Community, sslip.io) con la opción
     «Monitoreo CloudWatch» ACTIVADA en el wizard (default heredado).
  2. Ficha del entorno: sección Monitoreo con el agente ACTIVO + bitácora.
  3. Métricas REALES en CloudWatch: se espera el primer datapoint de
     mem_used_percent / disk_used_percent (CLI local, perfil pcm-sandbox)
     y se muestran los valores en una tarjeta.
  4. Desactivar (el servicio se detiene, sin desinstalar) → Reactivar
     (= activar sobre un servidor YA montado).
  5. Teardown con triple confirmación.

Secretos: ninguno pasa por acá (la maestra se inyecta por JS; las métricas
se leen con el perfil aws local de solo-operación).
"""
import json
import pathlib
import subprocess
import sys
import time
import xmlrpc.client
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
BUNDLE_DEMO = REPO / "primate_bundle" / "docs" / "demo"
sys.path.insert(0, str(BUNDLE_DEMO))
import grabar_demo as receta        # noqa: E402
import grabar_oximoro as ox         # noqa: E402

VIEW_W, VIEW_H = 1536, 864
VIDEO_W, VIDEO_H = 1920, 1080
VIEJO = "http://localhost:19088"
DB = "pcm_demo"
ADMIN = ("admin", "admin")
PERFIL_AWS = "pcm-sandbox"
ENV_NOMBRE = "Monitoreo CW (prueba)"
MAQ = "cw-demo"
DB_DESTINO = "cw_demo"
MASTER = "Demo.Master.2026"
CONFIG_TXT = "config de INCO embebida (archivo local, sin Parameter Store)"
SALIDA = HERE
FACTORES = {"montaje": 16, "agente": 8, "metricas": 16, "toggle": 8,
            "terminar": 16}

ox.MARCA_PLACA = "PCM · MONITOREO CLOUDWATCH (pedido INCO · sandbox)"
receta.SALIDA = SALIDA
marcas, ventanas, incidentes, tiempos = [], [], [], {}
T0 = None


def evento(texto):
    print("PCM_DEMO_CW: %s" % texto, flush=True)


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


def rpc(model, method, args, kwargs=None):
    common = xmlrpc.client.ServerProxy(VIEJO + "/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(DB, ADMIN[0], ADMIN[1], {})
    modelos = xmlrpc.client.ServerProxy(VIEJO + "/xmlrpc/2/object", allow_none=True)
    return modelos.execute_kw(DB, uid, ADMIN[1], model, method, args, kwargs or {})


def correr(orden, timeout=600):
    evento("cmd · %s" % " ".join(orden[:4]))
    return subprocess.run(orden, timeout=timeout, capture_output=True, text=True)


# ------------------------------------------------------------ UI helpers

def login(page, rotulo_txt=""):
    ox.ir(page, VIEJO + "/web/login", timeout=120000)
    page.fill("form:has(input[name=password]) input[name=login]", ADMIN[0])
    page.fill("form input[name=password]", ADMIN[1])
    if rotulo_txt:
        ox.rotulo(page, rotulo_txt, "", 1800)
    page.click("form:has(input[name=password]) button[type=submit]")
    page.wait_for_url("**/odoo**", timeout=45000)


def abrir_app(page):
    ox.ir(page, VIEJO + "/odoo/action-primate_cloud_manager.action_pcm_app",
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


def abrir_entorno(page):
    for _ in range(3):
        nav(page, "Entornos")
        page.locator(".o_pcm_screen input[type=text]").first.fill(ENV_NOMBRE)
        page.wait_for_timeout(1200)
        tarjeta = page.locator(".o_pcm_env_card", has_text=ENV_NOMBRE).filter(
            has_not_text="terminado")
        if tarjeta.count():
            ox.clic(page, tarjeta.first)
            page.wait_for_selector(".o_pcm_detalle .o_pcm_hero", timeout=15000)
            page.wait_for_timeout(900)
            return
        page.wait_for_timeout(2500)
    raise RuntimeError("no apareció la tarjeta del entorno %r" % ENV_NOMBRE)


def entorno_campos(env_id, campos):
    return rpc("primate.cloud.environment", "read", [[env_id], campos])[0]


def esperar(page, condicion, limite, nombre, factor, recargar_cada=0):
    """Espera acelerada hasta que condicion() devuelva truthy."""
    with Ventana(nombre, factor):
        t0 = time.time()
        vueltas = 0
        while time.time() - t0 < limite:
            valor = condicion()
            if valor:
                return valor
            vueltas += 1
            if recargar_cada and vueltas % recargar_cada == 0:
                page.reload(wait_until="load")
            page.wait_for_timeout(8000)
    raise RuntimeError("espera agotada: %s" % nombre)


# ------------------------------------------------------------ tramos

def precondiciones():
    salida = correr(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "4", VIEJO + "/web/login"])
    assert salida.stdout.strip() == "200", "pcm_demo no responde en :19088"
    viejos = rpc("primate.cloud.environment", "search_read",
                 [[["name", "=", ENV_NOMBRE]], ["id", "state"]])
    for v in viejos:
        assert v["state"] == "draft", \
            "queda un entorno %r en estado %s: limpiar a mano" % (ENV_NOMBRE, v["state"])
        instancias = rpc("primate.cloud.instance", "search",
                         [[["environment_id", "=", v["id"]]]])
        if instancias:
            rpc("primate.cloud.instance", "unlink", [instancias])
        rpc("primate.cloud.environment", "unlink", [[v["id"]]])
    # El teardown necesita el grupo destructivo (prestado; se quita al final).
    grupo = rpc("ir.model.data", "check_object_reference",
                ["primate_cloud_manager", "group_cloud_destructive"])[1]
    rpc("res.users", "write", [[2], {"group_ids": [[4, grupo]]}])
    evento("precondiciones OK (grupo destructivo prestado: %s)" % grupo)
    return grupo


def t1_montaje(page):
    nav(page, "Entornos")
    ox.clic(page, page.locator("button", has_text="Nuevo entorno").first)
    page.wait_for_selector("input#name_0", timeout=15000)
    ox.tipear(page, page.locator("input#name_0"), ENV_NOMBRE, 40)
    proy = page.locator("div[name=project_id] input").first
    proy.click()
    page.wait_for_timeout(600)
    proy.fill("Primate (ensayo self)")
    page.wait_for_timeout(1200)
    page.keyboard.press("Enter")
    tipo = page.locator("div[name=env_type] input, select#env_type_0").first
    try:
        tipo.click()
        page.wait_for_timeout(400)
        page.locator("li", has_text="Producción").first.click()
    except Exception:   # noqa: BLE001 — select nativo
        page.select_option("select#env_type_0", label="Producción")
    ox.clic(page, page.locator(".o_form_button_save").first)
    page.wait_for_timeout(3500)
    marca("entorno creado")

    abrir_entorno(page)
    ox.clic(page, page.locator("button", has_text="Aprovisionar").first)
    page.wait_for_selector(".o_pcm_field", timeout=15000)
    page.wait_for_timeout(1500)
    campo_pcm(page, "Nombre de la instancia").fill(MAQ)
    campo_pcm(page, "Tipo de instancia").fill("t3.small")
    campo_pcm(page, "Disco raíz (GB)").fill("30")
    campo_pcm(page, "Nombre de la base").fill(DB_DESTINO)
    page.evaluate(
        """(valor) => {
            const campos = [...document.querySelectorAll('.o_pcm_field')];
            const c = campos.find(f => f.textContent.includes('Contraseña admin'));
            const inp = c && c.querySelector('input');
            if (inp) {
                inp.value = valor;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
            }
        }""", MASTER)
    sslip = check_owl(page, "Dominio sslip.io automático")
    if sslip.count() and not sslip.is_checked():
        ox.clic(page, sslip)
    monitoreo = check_owl(page, "Monitoreo CloudWatch")
    assert monitoreo.count(), "no está el checkbox de Monitoreo CloudWatch"
    if not monitoreo.is_checked():
        ox.clic(page, monitoreo)
    ox.rotulo(page, "La opción nueva, ACTIVADA por default",
              "El agente de AWS se instala al final del montaje con la "
              "config que mantiene INCO (parámetro SSM)", 4200)
    marca("wizard con monitoreo activado")
    ox.clic(page, page.locator("button", has_text="Aprovisionar").last)
    page.wait_for_timeout(2500)
    marca("aprovisionamiento encolado")
    env_id = rpc("primate.cloud.environment", "search",
                 [[["name", "=", ENV_NOMBRE]]])[0]
    ox.rotulo(page, "Montando el servidor",
              "EC2 + bootstrap + Odoo 19 Community — espera real acelerada ×%d"
              % FACTORES["montaje"], 3000)
    esperar(page, lambda: entorno_campos(env_id, ["state"])["state"] == "active",
            1800, "montaje", FACTORES["montaje"], recargar_cada=8)
    evento("entorno %d → active" % env_id)
    marca("servidor activo")
    evento("TRAMO 1 OK")
    return env_id


def t2_agente_activo(page, env_id):
    # El paso 6 del montaje instala el agente; puede venir unos segundos atrás.
    def estado_final():
        valor = entorno_campos(env_id, ["cw_agent_state"])["cw_agent_state"]
        return valor if valor in ("active", "error") else None
    esperar(page, estado_final, 600, "agente", FACTORES["agente"])
    datos = entorno_campos(env_id, ["cw_agent_state", "cw_agent_error"])
    assert datos["cw_agent_state"] == "active", \
        "el agente no quedó activo: %s" % datos["cw_agent_error"]
    abrir_entorno(page)
    page.wait_for_timeout(1000)
    ox.desplazar(page, 900)
    page.wait_for_timeout(2000)
    ox.rotulo(page, "Agente CloudWatch: ACTIVO",
              "Instalado por SSM (.deb de Ubuntu) — %s" % CONFIG_TXT, 4200)
    ox.desplazar(page, -900)
    nav(page, "Bitácora")
    page.wait_for_timeout(2500)
    ox.rotulo(page, "Todo en la bitácora",
              "Tipo «Monitoreo CloudWatch»: activaciones, errores y health",
              3200)
    marca("agente activo en la ficha")
    evento("TRAMO 2 OK")


def _dimensiones(metric, instancia_aws):
    """Las dimensiones REALES de la métrica en CloudWatch (disk lleva
    path/device/fstype además de InstanceId; mem solo InstanceId)."""
    r = correr(["aws", "cloudwatch", "list-metrics", "--namespace", "CWAgent",
                "--metric-name", metric,
                "--dimensions", "Name=InstanceId,Value=%s" % instancia_aws,
                "--region", "us-east-1", "--profile", PERFIL_AWS])
    if r.returncode != 0:
        return None
    metricas = json.loads(r.stdout).get("Metrics") or []
    return metricas[0]["Dimensions"] if metricas else None


def _metricas(instancia_aws):
    """Primer datapoint de mem/disk del namespace CWAgent (CLI local)."""
    dims = {m: _dimensiones(m, instancia_aws)
            for m in ("mem_used_percent", "disk_used_percent")}
    if not all(dims.values()):
        return None     # todavía no aparecieron en list-metrics
    fin = datetime.now(timezone.utc)
    inicio = fin - timedelta(minutes=20)
    consulta = [
        {"Id": "mem", "MetricStat": {"Metric": {
            "Namespace": "CWAgent", "MetricName": "mem_used_percent",
            "Dimensions": dims["mem_used_percent"]},
            "Period": 60, "Stat": "Average"}},
        {"Id": "disk", "MetricStat": {"Metric": {
            "Namespace": "CWAgent", "MetricName": "disk_used_percent",
            "Dimensions": dims["disk_used_percent"]},
            "Period": 60, "Stat": "Average"}},
    ]
    r = correr(["aws", "cloudwatch", "get-metric-data",
                "--region", "us-east-1", "--profile", PERFIL_AWS,
                "--start-time", inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--end-time", fin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--metric-data-queries", json.dumps(consulta)])
    if r.returncode != 0:
        evento("get-metric-data falló: %s" % r.stderr[-200:])
        return None
    datos = json.loads(r.stdout)["MetricDataResults"]
    valores = {d["Id"]: d["Values"] for d in datos}
    if valores.get("mem") and valores.get("disk"):
        return {"mem": valores["mem"][0], "disk": valores["disk"][0]}
    return None


def t3_metricas(page, env_id):
    maquina = rpc("primate.cloud.ec2.instance", "search_read",
                  [[["environment_id", "=", env_id]], ["aws_instance_id"]])[0]
    ox.rotulo(page, "Esperando las primeras métricas",
              "El agente publica cada 60 s; CloudWatch tarda un par de "
              "minutos en mostrarlas — espera real acelerada ×%d"
              % FACTORES["metricas"], 3400)
    datos = esperar(page, lambda: _metricas(maquina["aws_instance_id"]),
                    900, "metricas", FACTORES["metricas"])
    evento("métricas en CloudWatch: mem %.1f%% · disk %.1f%%"
           % (datos["mem"], datos["disk"]))
    ox.tarjeta(page, "Las métricas LLEGARON a CloudWatch",
               "Namespace CWAgent · instancia %s" % maquina["aws_instance_id"],
               [("RAM usada", "%.1f %%" % datos["mem"]),
                ("Disco / usado", "%.1f %%" % datos["disk"]),
                ("Config", CONFIG_TXT)], 6000)
    marca("métricas verificadas en CloudWatch")
    evento("TRAMO 3 OK")


def t4_toggle(page, env_id):
    abrir_entorno(page)
    ox.desplazar(page, 900)
    page.wait_for_timeout(1200)
    ox.clic(page, page.get_by_role("button", name="Desactivar", exact=True).first)
    page.wait_for_timeout(2000)
    esperar(page, lambda: entorno_campos(env_id, ["cw_agent_state"])
            ["cw_agent_state"] == "disabled",
            300, "toggle-off", FACTORES["toggle"])
    page.reload(wait_until="load")
    page.wait_for_timeout(2000)
    ox.desplazar(page, 900)
    ox.rotulo(page, "Desactivado: el servicio se DETIENE",
              "Sin desinstalar — reactivar es instantáneo", 3200)
    marca("monitoreo desactivado")
    ox.clic(page, page.get_by_role("button", name="Activar", exact=True).first)
    page.wait_for_timeout(2000)
    esperar(page, lambda: entorno_campos(env_id, ["cw_agent_state"])
            ["cw_agent_state"] == "active",
            300, "toggle-on", FACTORES["toggle"])
    page.reload(wait_until="load")
    page.wait_for_timeout(2000)
    ox.desplazar(page, 900)
    ox.rotulo(page, "Reactivado sobre el servidor YA montado",
              "Mismo botón de la ficha; el agente vuelve a ACTIVO", 3200)
    ox.desplazar(page, -900)
    marca("monitoreo reactivado")
    evento("TRAMO 4 OK")


def t5_teardown(page, env_id, grupo):
    abrir_entorno(page)
    ox.clic(page, page.locator(".o_pcm_instance .o_pcm_line_name",
                               has_text=MAQ).first)
    page.wait_for_selector(".o_pcm_detalle", timeout=20000)
    page.wait_for_timeout(1500)
    ox.clic(page, page.locator("button", has_text="Terminar servidor").first)
    page.wait_for_selector("button[name=action_next_from_impact], #typed_name_0",
                           timeout=15000)
    if page.locator("button[name=action_next_from_impact]").count():
        ox.clic(page, page.locator("button[name=action_next_from_impact]").first)
        page.wait_for_timeout(1500)
    nombre = rpc("primate.cloud.ec2.instance", "search_read",
                 [[["environment_id", "=", env_id]], ["name"]])[0]["name"]
    ox.tipear(page, page.locator("#typed_name_0"), nombre, 50)
    ox.clic(page, page.locator("button[name=action_confirm_name]").first)
    page.wait_for_timeout(1500)
    page.locator("#password_0").fill(ADMIN[1])
    ox.clic(page, page.locator("button[name=action_execute]").first)
    marca("terminación encolada")

    def terminado():
        fila = rpc("primate.cloud.ec2.instance", "search_read",
                   [[["environment_id", "=", env_id]],
                    ["id", "instance_state"]])[0]
        if fila["instance_state"] == "terminated":
            return True
        rpc("primate.cloud.ec2.instance", "action_sync_from_aws", [[fila["id"]]])
        return False
    esperar(page, terminado, 900, "terminar", FACTORES["terminar"])
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    ox.rotulo(page, "Servidor terminado",
              "Recurso efímero: nada queda corriendo en la cuenta", 3400)
    rpc("primate.cloud.environment", "write", [[env_id]],
        {"name": ENV_NOMBRE + " — demo (terminado)"})
    rpc("res.users", "write", [[2], {"group_ids": [[3, grupo]]}])
    marca("recursos terminados")
    evento("TRAMO 5 OK")


def grabar():
    global T0
    grupo = precondiciones()
    bruto = SALIDA / "bruto"
    bruto.mkdir(exist_ok=True)
    with sync_playwright() as navegador:
        nav_ = navegador.chromium.launch(args=["--no-sandbox", "--lang=es-UY"])
        ctx = nav_.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, locale="es-UY",
            timezone_id="America/Montevideo",
            record_video_dir=str(bruto),
            record_video_size={"width": VIEW_W, "height": VIEW_H})
        ctx.add_init_script(receta.ROTULO_JS)
        ctx.add_init_script(receta.PLACA_JS)
        ctx.add_init_script(ox.CURSOR_JS)
        page = ctx.new_page()
        page.on("pageerror", lambda e: incidentes.append("JS: %s" % str(e)[:120]))
        ox.T0 = T0 = time.time()
        excepcion = None
        page.set_content("<html><body style='background:#111'></body></html>")
        ox.tarjeta(page, "Monitoreo CloudWatch en servidores PCM",
                   "Pedido INCO: RAM y disco con el agente de AWS y la config "
                   "de un parámetro SSM — demo real en el sandbox",
                   [("Servidor", "t3.small efímero · Ubuntu 24.04"),
                    ("Config", "la de INCO, embebida (archivo local)"),
                    ("Métricas", "mem_used_percent · disk_used_percent")], 6000)
        try:
            login(page, "PCM en el sandbox")
            abrir_app(page)
            ox.tarjeta(page, "1 · Montaje con la opción activada",
                       "El wizard la trae por default")
            env_id = t1_montaje(page)
            ox.tarjeta(page, "2 · El agente quedó ACTIVO",
                       "Estado en la ficha + bitácora")
            t2_agente_activo(page, env_id)
            ox.tarjeta(page, "3 · Las métricas llegan",
                       "Verificación REAL contra CloudWatch")
            t3_metricas(page, env_id)
            ox.tarjeta(page, "4 · Desactivar y reactivar",
                       "Sobre el servidor ya montado, sin desinstalar")
            t4_toggle(page, env_id)
            ox.tarjeta(page, "5 · Teardown", "Triple confirmación")
            t5_teardown(page, env_id, grupo)
            filas = [("Montaje + agente", "OK"),
                     ("Métricas en CloudWatch", "OK"),
                     ("Incidentes", str(len(incidentes)) if incidentes else "ninguno")]
            ox.tarjeta(page, "Demo completa",
                       "bj-uat se activa igual: un botón en la ficha", filas, 6000)
            marca("fin")
        except Exception as error:   # noqa: BLE001 — el video se salva igual
            excepcion = error
            incidentes.append("ABORTADO: %s" % str(error)[:300])
            evento("ERROR · %s" % str(error)[:300])
            try:
                page.screenshot(path=str(SALIDA / "demo_cw_fallo.png"))
            except Exception:   # noqa: BLE001
                pass
        finally:
            ruta_video = page.video.path() if page.video else None
            ctx.close()
            nav_.close()
    datos = {"video": str(ruta_video), "marcas": marcas, "ventanas": ventanas,
             "tiempos": tiempos, "incidentes": incidentes}
    destino = bruto / "toma_demo_cw.webm"
    if ruta_video:
        pathlib.Path(ruta_video).replace(destino)
        datos["video"] = str(destino)
    (SALIDA / "toma_demo_cw.json").write_text(json.dumps(datos, indent=1))
    evento("FIN · video crudo: %s · incidentes: %d" % (destino, len(incidentes)))
    for i in incidentes:
        print("  ⚠️ ", i)
    if excepcion:
        raise excepcion


def ensamblar():
    datos = json.loads((SALIDA / "toma_demo_cw.json").read_text())
    final = SALIDA / "demo_monitoreo_cloudwatch.mp4"
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
