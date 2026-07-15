#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aplica cambios al odoo.conf preservando TODO lo demás (Bloque B3).

Corre EN LA INSTANCIA por SSM (como root). NO reinicia Odoo: el job orquesta el
restart / health check / rollback. Pasos:

  1. CAS de concurrencia: si el hash actual != el esperado, aborta (``stale``).
  2. Valida los edits contra la allowlist + tipos (capa 2, defensa en profundidad).
  3. Edición QUIRÚRGICA por líneas: reemplaza solo el valor de las claves editadas,
     preservando indentación, separador, comentarios, orden y TODA clave no
     mostrada (``db_password`` incluido). Reescribe de forma atómica (tmp + rename)
     conservando owner/permiso del conf.
  4. Backup previo con permisos 600 (contiene db_password en claro) y rotación que
     conserva los más nuevos SIN borrar el recién creado (lo usa el rollback).

Tokens que el job reemplaza antes de enviar (base64/hex → no rompen el literal
ni permiten inyección):
    %%EDITS_B64%%       base64 de un JSON {clave: valor}
    %%EXPECTED_HASH%%   sha256 del conf que el panel leyó al abrir

Salida (marcadores parseados por el job):
    PCM_RESULT:applied|stale|invalid
    PCM_ERROR:<clave|motivo>            (en invalid)
    PCM_OLD:<clave>=<valor viejo>       (por cada clave editada; vacío si no existía)
    PCM_BAK:<ruta del backup>           (en applied; lo usa el rollback)
"""
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

# R4-B2: la ruta del conf es POR INSTANCIA (token; legacy = el viejo
# /etc/odoo/odoo.conf que la instancia trae en su campo conf_path).
CONF = "%%CONF_PATH%%"
BAK_KEEP = 5
EDITS_B64 = "%%EDITS_B64%%"
EXPECTED_HASH = "%%EXPECTED_HASH%%"
# Claves que un flujo INTERNO de PCM (p. ej. addon-add tocando addons_path)
# habilita además de la allowlist de usuario. Vacío en el guardado de usuario.
EXTRA_ALLOW = "%%EXTRA_ALLOW%%"


def emit(tag, value=""):
    print("PCM_%s:%s" % (tag, value))


def _int(low, high):
    def validate(raw):
        try:
            num = int(str(raw))
        except (TypeError, ValueError):
            return None
        return str(num) if low <= num <= high else None
    return validate


def _bool(raw):
    text = str(raw)
    if text in ("True", "true", "1"):
        return "True"
    if text in ("False", "false", "0"):
        return "False"
    return None


def _enum(options):
    def validate(raw):
        return str(raw) if str(raw) in options else None
    return validate


# Allowlist de EDITABLES (misma que el modelo; duplicada a propósito = capa 2).
ALLOW = {
    "proxy_mode": _bool,
    "list_db": _bool,
    "workers": _int(0, 64),
    "max_cron_threads": _int(0, 16),
    "limit_time_cpu": _int(0, 86400),
    "limit_time_real": _int(0, 86400),
    "limit_request": _int(0, 2000000),
    "limit_memory_soft": _int(0, 2 ** 40),
    "limit_memory_hard": _int(0, 2 ** 40),
    "log_level": _enum({"debug", "info", "warn", "error", "critical",
                        "debug_sql", "debug_rpc"}),
}


def main():
    try:
        with open(CONF, "rb") as handle:
            raw = handle.read()
    except OSError:
        emit("RESULT", "invalid")
        emit("ERROR", "no_conf")
        return

    # 1. CAS: el conf no cambió desde que el panel lo abrió.
    if hashlib.sha256(raw).hexdigest() != EXPECTED_HASH:
        emit("RESULT", "stale")
        return

    # 2. Validación de los edits (allowlist + tipos + coherencia).
    try:
        edits = json.loads(base64.b64decode(EDITS_B64))
    except Exception:  # noqa: BLE001
        emit("RESULT", "invalid")
        emit("ERROR", "bad_payload")
        return
    # Claves internas vouched por PCM (addons_path): validador permisivo (str no
    # vacío); las paga el flujo que las pasa, nunca el editor de usuario.
    extra = {k for k in EXTRA_ALLOW.split(",") if k}
    clean = {}
    for key, value in edits.items():
        if key in extra:
            if not str(value).strip():
                emit("RESULT", "invalid")
                emit("ERROR", key)
                return
            clean[key] = str(value)
            continue
        if key not in ALLOW:
            emit("RESULT", "invalid")
            emit("ERROR", key)
            return
        checked = ALLOW[key](value)
        if checked is None:
            emit("RESULT", "invalid")
            emit("ERROR", key)
            return
        clean[key] = checked
    if "limit_memory_soft" in clean and "limit_memory_hard" in clean:
        if int(clean["limit_memory_hard"]) < int(clean["limit_memory_soft"]):
            emit("RESULT", "invalid")
            emit("ERROR", "limit_memory_hard")
            return

    # 3. Edición quirúrgica por líneas (preserva TODO lo no editado).
    remaining = dict(clean)
    out = []
    for line in raw.decode("utf-8").splitlines(keepends=True):
        newline = ""
        body = line
        if body.endswith("\n"):
            body = body[:-1]
            newline = "\n"
            if body.endswith("\r"):
                body = body[:-1]
                newline = "\r\n"
        match = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*=\s*)(.*)$", body)
        if match and match.group(2) in remaining:
            key = match.group(2)
            emit("OLD", "%s=%s" % (key, match.group(4)))
            out.append("%s%s%s%s%s" % (match.group(1), key, match.group(3),
                                       remaining.pop(key), newline or "\n"))
        else:
            out.append(line)
    # Claves nuevas (no estaban en el archivo) → al final de [options].
    for key, value in remaining.items():
        emit("OLD", "%s=" % key)
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("%s = %s\n" % (key, value))
    new_raw = "".join(out).encode("utf-8")

    # 4. Backup 600 + escritura atómica preservando owner/permiso del conf.
    stat = os.stat(CONF)
    backup = "%s.pcm-bak-%s" % (CONF, time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(CONF, backup)
    os.chmod(backup, 0o600)
    emit("BAK", backup)

    handle_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONF))
    with os.fdopen(handle_fd, "wb") as tmp_handle:
        tmp_handle.write(new_raw)
        tmp_handle.flush()
        os.fsync(tmp_handle.fileno())
    os.chown(tmp, stat.st_uid, stat.st_gid)
    os.chmod(tmp, stat.st_mode & 0o777)
    os.replace(tmp, CONF)

    # Rotación: conservar los BAK_KEEP más nuevos, NUNCA el recién creado.
    backups = sorted(glob.glob("%s.pcm-bak-*" % CONF))
    for old_backup in backups[:-BAK_KEEP]:
        if old_backup != backup:
            try:
                os.remove(old_backup)
            except OSError:
                pass

    # Puerto HTTP del conf REESCRITO (read-only, pero se lee del archivo que
    # vamos a servir, no un default): el job hace el health check contra él.
    port = "8069"
    for line in new_raw.decode("utf-8").splitlines():
        port_match = re.match(r"^\s*http_port\s*=\s*(\d+)", line)
        if port_match:
            port = port_match.group(1)
            break
    emit("HTTP_PORT", port)

    # Estado HTTP ANTES del reinicio (Odoo sigue corriendo la config vieja): si
    # servía antes pero no después, el cambio rompió el serving → el job NO lo
    # degrada, hace rollback. Si ya no servía (proxy_mode/socket), 'degraded' es
    # legítimo. Se sondea acá (mismo SSM), no se confía en un estado externo.
    import urllib.request  # noqa: PLC0415
    http_was = "no"
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:%s/web/health" % port, timeout=3).read(1)
        http_was = "ok"
    except Exception:  # noqa: BLE001
        http_was = "no"
    emit("HTTP_WAS", http_was)
    emit("RESULT", "applied")


if __name__ == "__main__":
    main()
    sys.exit(0)
