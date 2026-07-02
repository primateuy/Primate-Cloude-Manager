#!/usr/bin/env bash
#
# install_odoo.sh — Aprovisionamiento de un entorno Odoo en una EC2 nueva.
#
# Plantilla versionada (Fase 4). El job de aprovisionamiento reemplaza los
# tokens %%...%% con los parámetros del entorno y la corre vía SSM.
# Es idempotente en lo posible y deja la instalación lista detrás de nginx + SSL.
#
# Tokens reemplazados por primate.cloud.environment.job_provision():
#   %%ODOO_VERSION%%    versión de Odoo (17 / 18 / 19)
#   %%ODOO_EDITION%%    community / enterprise
#   %%DB_HOST%%         host PostgreSQL (localhost para local_pg, endpoint RDS)
#   %%DB_PORT%%         puerto PostgreSQL (5432)
#   %%DB_NAME%%         nombre de la base
#   %%DB_USER%%         usuario PostgreSQL
#   %%DB_PASSWORD%%     contraseña PostgreSQL
#   %%DB_LOCAL%%        "1" si la base es PostgreSQL local en esta EC2, si no "0"
#   %%DOMAIN%%          dominio principal (ej.: forum.primate.cloud)
#   %%ADMIN_PASSWORD%%  admin_passwd del odoo.conf
#
set -euo pipefail

ODOO_VERSION="%%ODOO_VERSION%%"
ODOO_EDITION="%%ODOO_EDITION%%"
DB_HOST="%%DB_HOST%%"
DB_PORT="%%DB_PORT%%"
DB_NAME="%%DB_NAME%%"
DB_USER="%%DB_USER%%"
DB_PASSWORD="%%DB_PASSWORD%%"
DB_LOCAL="%%DB_LOCAL%%"
DOMAIN="%%DOMAIN%%"
ADMIN_PASSWORD="%%ADMIN_PASSWORD%%"

ODOO_HOME="/opt/odoo"
ODOO_USER="odoo"
ODOO_CONF="/etc/odoo/odoo.conf"

log() { echo "[pcm-install] $*"; }

# 1. Dependencias del sistema -------------------------------------------------
log "Instalando dependencias del sistema…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3-pip python3-venv build-essential \
    libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev libpq-dev \
    libjpeg-dev nginx certbot python3-certbot-nginx wkhtmltopdf

# 2. Usuario de servicio ------------------------------------------------------
if ! id "${ODOO_USER}" >/dev/null 2>&1; then
    log "Creando usuario de servicio ${ODOO_USER}…"
    useradd -m -d "${ODOO_HOME}" -U -r -s /bin/bash "${ODOO_USER}"
fi

# 3. PostgreSQL local (solo si la base vive en esta EC2) ----------------------
if [ "${DB_LOCAL}" = "1" ]; then
    log "Instalando PostgreSQL local…"
    apt-get install -y postgresql
    systemctl enable --now postgresql
    sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" \
        | grep -q 1 || sudo -u postgres psql -c \
        "CREATE USER ${DB_USER} WITH CREATEDB PASSWORD '${DB_PASSWORD}';"
fi

# 4. Código de Odoo -----------------------------------------------------------
log "Clonando Odoo ${ODOO_VERSION} (${ODOO_EDITION})…"
sudo -u "${ODOO_USER}" git clone --depth 1 --branch "${ODOO_VERSION}.0" \
    https://github.com/odoo/odoo.git "${ODOO_HOME}/odoo" || true

sudo -u "${ODOO_USER}" python3 -m venv "${ODOO_HOME}/venv"
sudo -u "${ODOO_USER}" "${ODOO_HOME}/venv/bin/pip" install --upgrade pip wheel
sudo -u "${ODOO_USER}" "${ODOO_HOME}/venv/bin/pip" install \
    -r "${ODOO_HOME}/odoo/requirements.txt"

# 5. odoo.conf ----------------------------------------------------------------
log "Escribiendo ${ODOO_CONF}…"
mkdir -p /etc/odoo /var/log/odoo
cat > "${ODOO_CONF}" <<CONF
[options]
admin_passwd = ${ADMIN_PASSWORD}
db_host = ${DB_HOST}
db_port = ${DB_PORT}
db_user = ${DB_USER}
db_password = ${DB_PASSWORD}
db_name = ${DB_NAME}
addons_path = ${ODOO_HOME}/odoo/addons
logfile = /var/log/odoo/odoo.log
proxy_mode = True
CONF
chown "${ODOO_USER}:${ODOO_USER}" "${ODOO_CONF}"
chmod 640 "${ODOO_CONF}"

# 6. Servicio systemd ---------------------------------------------------------
log "Configurando servicio systemd…"
cat > /etc/systemd/system/odoo.service <<UNIT
[Unit]
Description=Odoo
After=network.target postgresql.service

[Service]
Type=simple
User=${ODOO_USER}
ExecStart=${ODOO_HOME}/venv/bin/python3 ${ODOO_HOME}/odoo/odoo-bin -c ${ODOO_CONF}
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now odoo

# 7. nginx como proxy reverso -------------------------------------------------
log "Configurando nginx para ${DOMAIN}…"
# default_server: responde también al acceso por IP cruda (sin Host).
cat > "/etc/nginx/sites-available/${DOMAIN}" <<NGINX
server {
    listen 80 default_server;
    server_name ${DOMAIN} _;
    location / {
        proxy_pass http://127.0.0.1:8069;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
# Quitar el sitio default de nginx (si no, captura el acceso por IP).
rm -f /etc/nginx/sites-enabled/default
ln -sf "/etc/nginx/sites-available/${DOMAIN}" "/etc/nginx/sites-enabled/${DOMAIN}"
nginx -t && systemctl reload nginx

# 8. Certificado SSL (certbot) ------------------------------------------------
log "Solicitando certificado SSL para ${DOMAIN}…"
certbot --nginx -n --agree-tos --redirect \
    -m "ops@primate.uy" -d "${DOMAIN}" || \
    log "ADVERTENCIA: certbot falló (¿DNS aún no propagado?). Reintentar luego."

log "Aprovisionamiento completado para ${DOMAIN}."
