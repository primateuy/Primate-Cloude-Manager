#!/usr/bin/env bash
#
# bootstrap_server.sh — Preparación ÚNICA del servidor para el layout multi-Odoo (R3).
#
# Corre UNA vez por servidor (idempotente y re-ejecutable: cada paso chequea antes
# de hacer). Instala lo de MÁQUINA: dependencias, usuario de servicio, PostgreSQL,
# nginx base, swap y el layout /opt/pcm. NO instala ningún Odoo: el runtime por
# versión lo trae on-demand el primer install_instance.sh que lo necesite.
#
# Tokens reemplazados por PCM:
#   %%SWAP_MB%%   tamaño del swapfile en MB (solo se crea si RAM < 4 GB y no hay swap)
#
# Marker: /opt/pcm/.bootstrap-v1 — install_instance.sh lo exige. Si este script
# cambia de forma incompatible, versionar el marker (v2) y re-correr el delta.
#
set -euo pipefail

SWAP_MB="%%SWAP_MB%%"
ODOO_USER="odoo"
PCM_ROOT="/opt/pcm"
MARKER="${PCM_ROOT}/.bootstrap-v1"
# El stdout de SSM (GetCommandInvocation) se TRUNCA a ~24KB (hallazgo real
# de B4: el apt se comía el marcador final). Todo lo verboso va a este log
# en el servidor; el stdout de SSM queda chico: log() + marcadores.
DETALLE="/var/log/pcm-bootstrap.log"

log() { echo "[pcm-bootstrap] $*"; }

if [ -f "${MARKER}" ]; then
    log "Bootstrap ya aplicado (${MARKER}); nada que hacer."
    echo "PCM_BOOTSTRAP_OK"
    exit 0
fi

# 1. Dependencias del sistema (las de máquina del install legacy) --------------
log "Instalando dependencias del sistema… (detalle en ${DETALLE})"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >>"${DETALLE}" 2>&1
apt-get install -y git python3-pip python3-venv build-essential \
    libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev libpq-dev \
    libjpeg-dev nginx certbot python3-certbot-nginx wkhtmltopdf \
    >>"${DETALLE}" 2>&1

# AWS CLI: lo usan backup/staging/restore (aws s3 cp en streaming). En Ubuntu
# 24.04 el paquete apt no tiene candidato -> snap (hallazgo real de Fase 8).
command -v aws >/dev/null 2>&1 || snap install aws-cli --classic \
    >>"${DETALLE}" 2>&1

# 2. Usuario de servicio (compartido en v1 — aislamiento por instancia es v2) --
if ! id "${ODOO_USER}" >/dev/null 2>&1; then
    log "Creando usuario de servicio ${ODOO_USER}…"
    useradd -m -d /home/${ODOO_USER} -U -r -s /bin/bash "${ODOO_USER}"
fi

# 3. PostgreSQL SIEMPRE (D-R3.4: también con RDS; un cluster idle es aceptable) -
log "Instalando PostgreSQL…"
apt-get install -y postgresql >>"${DETALLE}" 2>&1
systemctl enable --now postgresql

# 4. Swap (D-R3.7): multi-Odoo en máquinas chicas lo necesita ------------------
total_ram_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "${total_ram_mb}" -lt 4096 ] && ! swapon --show | grep -q .; then
    log "Creando swapfile de ${SWAP_MB} MB (RAM: ${total_ram_mb} MB)…"
    fallocate -l "${SWAP_MB}M" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
    sysctl -w vm.swappiness=10 >/dev/null
    grep -q "^vm.swappiness" /etc/sysctl.conf || echo "vm.swappiness=10" >> /etc/sysctl.conf
fi

# 5. Layout /opt/pcm ------------------------------------------------------------
log "Creando layout ${PCM_ROOT}…"
mkdir -p "${PCM_ROOT}/runtime" "${PCM_ROOT}/instances"
chown -R "${ODOO_USER}:${ODOO_USER}" "${PCM_ROOT}"

# 6. nginx base: fuera el sitio default (captura el acceso por IP). Seguro acá:
#    el bootstrap solo corre en servidores nuevos o ya multi-Odoo, nunca sobre
#    un legacy vivo (D-R3.2: los legacy quedan gated).
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# 7. Marker (al FINAL: si algo de arriba falló, el bootstrap NO quedó aplicado) -
touch "${MARKER}"
log "Bootstrap completado."
echo "PCM_BOOTSTRAP_OK"
