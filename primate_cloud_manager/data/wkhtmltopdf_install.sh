#!/usr/bin/env bash
#
# wkhtmltopdf_install.sh — instala wkhtmltopdf desde el RELEASE oficial de
# GitHub (wkhtmltopdf/packaging). El paquete de los repos de Ubuntu viene SIN
# el Qt parcheado: headers/footers rotos en los PDF — por eso esta versión
# pisa a la de apt (instala en /usr/local/bin, que gana en el PATH).
#
# Idempotente: si la versión pedida ya está activa (con "patched qt"), no toca
# nada. Sirve tanto en el montaje como aplicado a un servidor ya montado.
#
# Tokens:
#   %%WKHTML_VERSION%%  versión del release, ej. 0.12.6.1-3 (el asset es el
#                       .deb de jammy, que instala limpio en noble/24.04).
#
set -euo pipefail

VERSION="%%WKHTML_VERSION%%"
# Lo que `wkhtmltopdf --version` reporta es la parte ANTES del guion del
# release: 0.12.6.1-3 -> "0.12.6.1 (with patched qt)".
BASE="${VERSION%%-*}"
DETALLE="/var/log/pcm-wkhtmltopdf.log"

log() { echo "[pcm-wkhtml] $*"; }

actual=$(wkhtmltopdf --version 2>/dev/null || true)
if echo "${actual}" | grep -qF "${BASE}" && \
   echo "${actual}" | grep -qi "patched qt"; then
    log "Ya instalada la versión pedida; nada que hacer."
    echo "PCM_WKHTML_OK:${actual}"
    exit 0
fi

ARCH=$(dpkg --print-architecture)
URL="https://github.com/wkhtmltopdf/packaging/releases/download/${VERSION}/wkhtmltox_${VERSION}.jammy_${ARCH}.deb"
TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

log "Descargando ${URL} (detalle en ${DETALLE})…"
curl -fsSL -o "${TMP}/wkhtmltox.deb" "${URL}"

# apt resuelve las dependencias del .deb local (fuentes, xrender, …).
log "Instalando el paquete…"
if ! apt-get install -y "${TMP}/wkhtmltox.deb" >>"${DETALLE}" 2>&1; then
    echo "PCM_WKHTML_FAIL:apt-get no pudo instalar el paquete (ver ${DETALLE})"
    exit 1
fi

actual=$(wkhtmltopdf --version 2>/dev/null || true)
if ! echo "${actual}" | grep -qF "${BASE}" || \
   ! echo "${actual}" | grep -qi "patched qt"; then
    echo "PCM_WKHTML_FAIL:${actual:-wkhtmltopdf no responde} (se esperaba ${BASE} con patched qt)"
    exit 1
fi
log "Verificado: ${actual}"
echo "PCM_WKHTML_OK:${actual}"
