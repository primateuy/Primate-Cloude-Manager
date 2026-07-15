#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lectura del odoo.conf para el panel de instancia (Bloque B3).

Corre EN LA INSTANCIA por SSM (como root). Emite SOLO la allowlist (parámetros
editables + read-only de display) y el hash del archivo completo. Las
credenciales (``db_password``/``admin_passwd``) NO están en la allowlist: no se
leen ni se emiten (se OMITEN, no se enmascaran). El hash se calcula sobre el
archivo entero pero solo el hash sale de la instancia (nunca el contenido).

Salida (una línea por dato, parseada por el job):
    PCM_HASH:<sha256 del archivo>
    PCM_VAL:<clave>=<valor>        (solo claves de la allowlist presentes)
    PCM_ERROR:no_conf              (si el archivo no existe/legible)
"""
import hashlib
import re
import sys

# R4-B2: la ruta del conf es POR INSTANCIA (token; legacy = el viejo
# /etc/odoo/odoo.conf que la instancia trae en su campo conf_path).
CONF = "%%CONF_PATH%%"

# Editables (se muestran y se pueden guardar). Ver el modelo para el detalle.
EDITABLE = [
    "proxy_mode", "list_db", "workers", "max_cron_threads",
    "limit_time_cpu", "limit_time_real", "limit_request",
    "limit_memory_soft", "limit_memory_hard", "log_level",
]
# Read-only (se muestran para contexto; error acá NO debe impedir el arranque
# → por eso son read-only en v1: http_interface/http_port/addons_path/
# server_wide_modules/data_dir mal puestos dejan Odoo sin levantar).
READONLY = [
    "db_host", "db_port", "db_user", "db_name", "data_dir",
    "http_port", "http_interface", "addons_path", "server_wide_modules",
]
# db_password / admin_passwd EXCLUIDOS a propósito: nunca en la allowlist.
ALLOW = set(EDITABLE + READONLY)


def main():
    try:
        with open(CONF, "rb") as handle:
            raw = handle.read()
    except OSError:
        print("PCM_ERROR:no_conf")
        return
    print("PCM_HASH:%s" % hashlib.sha256(raw).hexdigest())
    for line in raw.decode("utf-8", "replace").splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if match and match.group(1) in ALLOW:
            print("PCM_VAL:%s=%s" % (match.group(1), match.group(2)))


if __name__ == "__main__":
    main()
    sys.exit(0)
