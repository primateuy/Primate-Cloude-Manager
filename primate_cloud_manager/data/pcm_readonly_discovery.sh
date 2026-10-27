#!/usr/bin/env bash
# pcm_readonly_discovery.sh — Gemelo exacto (punto 3, 2026-10-22).
#
# SOLO LECTURA por construcción: comandos fijos de consulta, SIN parámetros,
# sin escribir NADA en el servidor (la salida va por stdout, gzip+base64).
# Es el ÚNICO contenido del documento SSM pcm-readonly-discovery con el que
# la policy pcm-prod-readonly permite SendCommand: aunque la credencial se
# filtre, con ella no se puede ejecutar otra cosa en los servidores.
#
# Salida: líneas  PCM_TWIN_B64:<base64(gzip(json))>  + PCM_TWIN_OK con el
# gemelo: sistema, y por cada Odoo (conf de /etc/odoo/*.conf, /etc/odoo.conf
# Y los que usan las units systemd, sin duplicados): conf enmascarado,
# addons_path en orden, unit systemd, core/enterprise (versión+commit),
# python, repos git (remote, rama, SHA, submódulos, sucio/no pusheado), base
# (versión PG, tamaño, filestore), nginx (vhosts, dominios, certificados y
# vencimiento), crons del sistema con los secretos tapados. El pip freeze va
# UNA vez por venv (twin_format 2), no por instancia.
#
# Corre `du` sobre cada filestore y `pip freeze` por venv: en un servidor
# grande conviene correrlo fuera de horario.
#
# Por ssh, desde la máquina del operador (el script viaja por stdin, no se
# copia al servidor, y el gemelo vuelve por stdout):
#   ssh <host> 'sudo bash -s' < pcm_readonly_discovery.sh > gemelo.txt
set -uo pipefail
export LC_ALL=C
# Lecturas que escriben como efecto lateral, apagadas: `git status` refresca
# .git/index (y toma index.lock) y Python deja .pyc al importar pip.
export GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

python3 - <<'PCM_TWIN_PY'
import base64
import configparser
import glob
import gzip
import json
import os
import re
import subprocess


def sh(cmd, cwd=None, timeout=30):
    """Comando FIJO de consulta; cualquier fallo devuelve ''."""
    try:
        out = subprocess.run(
            cmd, shell=True, cwd=cwd, timeout=timeout,
            capture_output=True, text=True)
        return (out.stdout or "").strip()
    except Exception:
        return ""


# Claves del odoo.conf cuyo valor se tapa (subcadena, sin mayúsculas).
MASCARA = ("password", "passwd", "secret", "token", "key", "api")

# Secretos en crontabs: VAR=valor con nombre sospechoso, --password[=| ]x,
# -p<pass> pegado (estilo mysql), y usuario:clave@ en URLs.
_CRON_SECRETOS = [
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:PASS|PASSWD|PASSWORD|SECRET|TOKEN|KEY|API)"
                r"[A-Z0-9_]*\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1******"),
    (re.compile(r"(?i)(--(?:password|passwd|pass|token|secret|api[-_]?key)"
                r"(?:=|\s+))(\S+)"), r"\1******"),
    (re.compile(r"(?<=\s)-p(?!\s)\S+"), "-p******"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1******@"),
]


def tapar_secretos(texto):
    """Crontab con los valores sensibles tapados (la forma queda visible)."""
    for patron, reemplazo in _CRON_SECRETOS:
        texto = patron.sub(reemplazo, texto or "")
    return texto


def leer_conf(path):
    conf = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        conf.read(path)
    except Exception:
        return {}, {}
    opts = dict(conf["options"]) if conf.has_section("options") else {}
    enmascarado = {
        k: ("******" if any(m in k.lower() for m in MASCARA)
            and v not in ("", "False") else v)
        for k, v in sorted(opts.items())}
    return opts, enmascarado


def git_info(path):
    if not os.path.isdir(os.path.join(path, ".git")) and not os.path.isfile(
            os.path.join(path, ".git")):
        return None
    info = {
        "path": path,
        "remote_url": sh("git -c safe.directory='*' config --get remote.origin.url", cwd=path),
        "branch": sh("git -c safe.directory='*' rev-parse --abbrev-ref HEAD", cwd=path),
        "commit_sha": sh("git -c safe.directory='*' rev-parse HEAD", cwd=path),
        "commit_date": sh("git -c safe.directory='*' log -1 --format=%cI", cwd=path),
        "submodules": [l.strip() for l in sh(
            "git -c safe.directory='*' submodule status", cwd=path
        ).splitlines() if l.strip()],
    }
    porcelain = sh("git -c safe.directory='*' status --porcelain", cwd=path)
    info["dirty_files"] = len([l for l in porcelain.splitlines() if l.strip()])
    ahead = sh("git -c safe.directory='*' rev-list --count @{u}..HEAD", cwd=path)
    info["unpushed_commits"] = int(ahead) if ahead.isdigit() else None
    info["alert_local_changes"] = bool(
        info["dirty_files"] or (info["unpushed_commits"] or 0) > 0)
    return info


def version_odoo(src_dir):
    rel = os.path.join(src_dir, "odoo", "release.py")
    data = {"path": src_dir}
    if os.path.isfile(rel):
        txt = open(rel, encoding="utf-8", errors="replace").read()
        m = re.search(r"version_info\s*=\s*\(([^)]*)\)", txt)
        v = re.search(r"^version\s*=\s*['\"]([^'\"]+)", txt, re.M)
        data["release"] = (v.group(1) if v else (m.group(1) if m else "?"))
    g = git_info(src_dir)
    if g:
        data.update({"commit_sha": g["commit_sha"], "branch": g["branch"]})
    return data


def argv_de(execstart):
    """El comando real de un ExecStart de systemd: viene como
    «{ path=… ; argv[]=python odoo-bin -c x.conf ; … }»; si no trae argv[],
    es la línea tal cual."""
    m = re.search(r"argv\[\]=([^;]*)", execstart or "")
    return (m.group(1) if m else (execstart or "")).strip()


def conf_de_argv(trozos):
    """El conf de un comando de Odoo: -c X, --config X o --config=X."""
    for i, t in enumerate(trozos):
        if t in ("-c", "--config") and i + 1 < len(trozos):
            return trozos[i + 1]
        if t.startswith("--config="):
            return t.split("=", 1)[1]
    return ""


def units_odoo():
    """{conf_path real: datos de la unit} de las units systemd de Odoo.

    Mismo criterio que el inventario F2 (units cuyo nombre contiene «odoo»),
    y el conf sale del ExecStart: así entra un Odoo con el conf fuera de
    /etc/odoo. Se consulta UNA vez por servidor."""
    mapa = {}
    for unit in sh("systemctl list-units --type=service --all --no-legend "
                   "--plain '*odoo*'").splitlines():
        name = unit.split()[0] if unit.split() else ""
        if not name:
            continue
        execstart = sh("systemctl show -p ExecStart --value %s" % name)
        trozos = argv_de(execstart).split()
        conf = conf_de_argv(trozos)
        if not conf:
            continue
        mapa[os.path.realpath(conf)] = {
            "unit": name,
            "python_bin": next((t for t in trozos if "python" in t), ""),
            "odoo_bin": next((t for t in trozos if "odoo-bin" in t
                              or t.endswith("odoo")), ""),
            "active": sh("systemctl is-active %s" % name),
            "enabled": sh("systemctl is-enabled %s" % name)}
    return mapa


def confs_a_leer(units, base_dir="/etc"):
    """Unión, sin duplicados (por ruta real), de los conf de las ubicaciones
    de siempre (/etc/odoo/*.conf, /etc/odoo.conf) y los que usan las units
    systemd: un Odoo con el conf en /opt/x/odoo.conf no se pierde."""
    candidatos = sorted(glob.glob(os.path.join(base_dir, "odoo", "*.conf")))
    if os.path.isfile(os.path.join(base_dir, "odoo.conf")):
        candidatos.append(os.path.join(base_dir, "odoo.conf"))
    candidatos += sorted(units)
    confs, vistos = [], set()
    for c in candidatos:
        real = os.path.realpath(c)
        if real not in vistos and os.path.isfile(real):
            vistos.add(real)
            confs.append(c)
    return confs


def es_enterprise(ruta):
    """Una ruta de addons es el enterprise de Odoo si trae web_enterprise
    (o se llama exactamente «enterprise»), no por contener la palabra."""
    return (os.path.isdir(os.path.join(ruta, "web_enterprise"))
            or os.path.basename(ruta) == "enterprise")


def nginx_info():
    sitios = []
    for site in sorted(glob.glob("/etc/nginx/sites-enabled/*")):
        txt = ""
        try:
            txt = open(site, encoding="utf-8", errors="replace").read()
        except Exception:
            pass
        nombres = re.findall(r"server_name\s+([^;]+);", txt)
        certs = re.findall(r"ssl_certificate\s+([^;]+);", txt)
        upstreams = re.findall(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)", txt)
        cert_info = []
        for cert in {c.strip() for c in certs}:
            end = sh("openssl x509 -enddate -noout -in %s" % cert)
            cert_info.append({"path": cert.strip(),
                              "not_after": end.replace("notAfter=", "")})
        sitios.append({"site": os.path.basename(site),
                       "server_names": " ".join(n.strip() for n in nombres),
                       "proxy_ports": sorted(set(upstreams)),
                       "certificates": cert_info})
    return sitios


def db_info(opts):
    db = opts.get("db_name") or ""
    if not db or db in ("False",):
        return {}
    size = sh("sudo -u postgres psql -tAc "
              "\"SELECT pg_database_size('%s')\"" % db)
    return {
        "db_name": db,
        "pg_version": sh("sudo -u postgres psql -tAc 'SHOW server_version'"),
        "db_size_bytes": int(size) if size.isdigit() else None,
    }


def filestore_info(opts):
    data_dir = opts.get("data_dir") or ""
    db = opts.get("db_name") or ""
    ruta = os.path.join(data_dir, "filestore", db) if data_dir and db else ""
    if not ruta or not os.path.isdir(ruta):
        return {"path": ruta or None}
    du = sh("du -sb %s" % ruta, timeout=120).split()
    return {"path": ruta,
            "size_bytes": int(du[0]) if du and du[0].isdigit() else None}


twin = {
    "twin_format": 2,
    "host": sh("hostname"),
    "system": {
        "os": sh(". /etc/os-release && echo $PRETTY_NAME"),
        "kernel": sh("uname -r"),
        "packages": {p: sh("dpkg-query -W -f='${Version}' %s 2>/dev/null" % p)
                     for p in ("nginx", "postgresql", "certbot",
                               "python3", "wkhtmltopdf")},
        "cron_system": tapar_secretos(sh("cat /etc/crontab 2>/dev/null")),
        "cron_d": sorted(os.path.basename(f)
                         for f in glob.glob("/etc/cron.d/*")),
        "cron_root": tapar_secretos(sh("crontab -l -u root 2>/dev/null")),
    },
    "instances": [],
    # pip freeze UNA vez por venv (python_bin): varias instancias suelen
    # compartirlo y es lo que más pesa en la salida de SSM (24.000 caracteres).
    "venvs": {},
}

UNITS = units_odoo()
for conf_path in confs_a_leer(UNITS):
    opts, masked = leer_conf(conf_path)
    addons = [p for p in (opts.get("addons_path") or "").split(",") if p]
    unit = UNITS.get(os.path.realpath(conf_path), {})
    venv_python = unit.get("python_bin") or ""
    repos, core, enterprise = [], None, None
    for ruta in addons:
        ruta = ruta.strip().rstrip("/")
        base = ruta
        # El core suele entrar como <src>/addons y <src>/odoo/addons.
        if base.endswith("/addons"):
            padre = os.path.dirname(base)
            if os.path.isfile(os.path.join(padre, "odoo-bin")):
                core = core or version_odoo(padre)
                continue
            abuelo = os.path.dirname(padre)
            if padre.endswith("/odoo") and os.path.isfile(
                    os.path.join(abuelo, "odoo-bin")):
                core = core or version_odoo(abuelo)
                continue
        # Una sola ruta es «enterprise»; cualquier otra (aunque su nombre
        # contenga la palabra) es un repo más.
        if enterprise is None and es_enterprise(base):
            g = git_info(base)
            if g:
                enterprise = g
                continue
        g = git_info(base)
        if g:
            repos.append(g)
        else:
            repos.append({"path": base, "remote_url": None,
                          "alert_local_changes": False,
                          "note": "sin git (carpeta suelta)"})
    if venv_python and venv_python not in twin["venvs"]:
        twin["venvs"][venv_python] = {
            "version": sh("%s --version" % venv_python),
            "pip_freeze": sh("%s -m pip freeze 2>/dev/null" % venv_python,
                             timeout=120)}
    twin["instances"].append({
        "conf_path": conf_path,
        "conf_masked": masked,
        "addons_path": addons,
        "workers": opts.get("workers"),
        "limits": {k: v for k, v in opts.items() if k.startswith("limit_")},
        "service": unit,
        "odoo_core": core,
        "odoo_enterprise": enterprise,
        "python": {"bin": venv_python},
        "database": db_info(opts),
        "filestore": filestore_info(opts),
        "repos": repos,
    })

twin["nginx"] = nginx_info()
blob = base64.b64encode(gzip.compress(
    json.dumps(twin, ensure_ascii=False).encode())).decode()
# En trozos de 4000 para esquivar límites de línea; el parser los re-une.
for i in range(0, len(blob), 4000):
    print("PCM_TWIN_B64:%s" % blob[i:i + 4000])
print("PCM_TWIN_OK")
PCM_TWIN_PY
