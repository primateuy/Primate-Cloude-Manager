#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery de instancias Odoo en un servidor importado (F2).

=============================================================================
CONTRATO: SÓLO LECTURA. Este script corre por SSM, como root, en servidores
ADOPTADOS: servidores productivos de clientes que PCM no creó. No escribe
nada, no instala nada, no reinicia nada y no cambia ningún archivo, servicio,
base ni permiso.

Todo lo que hace es LEER:
  - la lista de units de systemd  (systemctl list-units / show)
  - el odoo.conf de cada unit     (sólo las claves de la allowlist)
  - la versión de Odoo            (se lee release.py como texto, no se importa)
  - la lista de bases             (SELECT sobre pg_database)

Cualquier cambio que haga falta en uno de estos servidores tiene que pasar
primero por «Habilitar la gestión». Si alguna vez se agrega una línea que
escriba, este script deja de ser apto para servidores adoptados.
=============================================================================

Las credenciales NO se leen: ``db_password`` y ``admin_passwd`` no están en la
allowlist, así que se OMITEN (no se enmascaran). No se ejecuta el binario de
Odoo para saber la versión, porque eso correría código del cliente; se lee
``odoo/release.py`` como texto.

Salida: una sola línea, ``PCM_DISCOVERY_JSON:<json>``. Las listas van acotadas
para no pasar el tope de 24.000 caracteres de ``get_command_invocation``.
"""
import json
import os
import re
import subprocess

MAX_UNITS = 30
MAX_DBS = 150

# Allowlist del odoo.conf: lo necesario para registrar la instancia. Sin
# db_password ni admin_passwd, a propósito.
CONF_KEYS = ["data_dir", "db_user", "db_name", "db_host", "db_port",
             "http_port", "xmlrpc_port", "addons_path", "dbfilter"]


def correr(cmd, timeout=20):
    """Ejecuta un comando de LECTURA y devuelve (código, stdout)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return res.returncode, res.stdout
    except Exception as error:  # noqa: BLE001 - se informa, no se aborta
        return -1, str(error)


def leer_conf(path):
    """Lee SÓLO las claves de la allowlist de un odoo.conf."""
    datos = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for linea in fh:
                m = re.match(r"^\s*([a-z_]+)\s*=\s*(.*?)\s*$", linea)
                if m and m.group(1) in CONF_KEYS:
                    datos[m.group(1)] = m.group(2)
    except OSError as error:
        return None, str(error)
    return datos, None


def version_odoo(exec_start):
    """Versión mayor de Odoo leyendo release.py como TEXTO (no se importa)."""
    for token in exec_start.split():
        if not re.search(r"(odoo-bin|openerp-server|odoo\.py)$", token):
            continue
        base = os.path.dirname(token.strip("\"'"))
        for rel in ("odoo/release.py", "openerp/release.py"):
            ruta = os.path.join(base, rel)
            try:
                with open(ruta, encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"version_info\s*=\s*\(\s*(?:'saas~)?(\d+)",
                                  fh.read())
                if m:
                    return m.group(1)
            except OSError:
                continue
    return None


def units_odoo():
    """Units de systemd cuyo nombre menciona odoo, con su conf y estado."""
    codigo, salida = correr(["systemctl", "list-units", "--type=service",
                             "--all", "--no-legend", "--plain"])
    if codigo != 0:
        return [], "systemctl no disponible: %s" % salida[:200]
    nombres = [ln.split()[0] for ln in salida.splitlines()
               if ln.strip() and re.search(r"odoo|openerp", ln.split()[0], re.I)]
    units = []
    for nombre in nombres[:MAX_UNITS]:
        _c, props = correr(["systemctl", "show", nombre, "-p", "ExecStart",
                            "-p", "ActiveState", "-p", "User"])
        info = dict(ln.split("=", 1) for ln in props.splitlines() if "=" in ln)
        exec_start = info.get("ExecStart", "")
        # ExecStart viene como "{ path=... ; argv[]=... ; ... }": el argv manda.
        argv = re.search(r"argv\[\]=([^;]*)", exec_start)
        linea = argv.group(1).strip() if argv else exec_start
        conf = re.search(r"(?:-c|--config)(?:=|\s+)(\S+)", linea)
        unit = {
            "unit": nombre,
            "active": info.get("ActiveState", ""),
            "user": info.get("User", ""),
            "exec": linea[:300],
            "conf_path": conf.group(1).strip("\"'") if conf else None,
            # El puerto puede venir por argumento y pisar el del conf.
            "port_arg": (lambda p: p.group(1) if p else None)(re.search(
                r"--(?:http|xmlrpc)-port(?:=|\s+)(\d+)", linea)),
            "version": version_odoo(linea),
        }
        if unit["conf_path"]:
            datos, error = leer_conf(unit["conf_path"])
            unit["conf"] = datos
            unit["conf_error"] = error
        units.append(unit)
    return units, None


def bases():
    """Bases no-template con su dueño (para asociarlas a su instancia)."""
    codigo, salida = correr([
        "sudo", "-u", "postgres", "psql", "-tAF|", "-c",
        "SELECT d.datname, pg_get_userbyid(d.datdba) FROM pg_database d "
        "WHERE NOT d.datistemplate AND d.datname <> 'postgres' "
        "ORDER BY d.datname"])
    if codigo != 0:
        return [], "no se pudieron listar las bases: %s" % salida[:200]
    filas = []
    for ln in salida.splitlines()[:MAX_DBS]:
        if "|" in ln:
            nombre, dueno = ln.split("|", 1)
            filas.append({"name": nombre.strip(), "owner": dueno.strip()})
    return filas, None


def main():
    units, error_units = units_odoo()
    dbs, error_dbs = bases()
    print("PCM_DISCOVERY_JSON:" + json.dumps({
        "units": units, "units_error": error_units,
        "databases": dbs, "databases_error": error_dbs,
    }, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
