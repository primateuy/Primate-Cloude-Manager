#!/usr/bin/env bash
#
# install_instance.sh — Monta UN Odoo (una instancia, por slug) en un servidor
# ya bootstrapeado (R3, multi-Odoo). NO toca a los Odoo que ya corren:
# - todo artefacto que crea/borra lleva el slug en el nombre;
# - nginx se valida (nginx -t) y se RECARGA, nunca se reinicia;
# - ante cualquier fallo, el trap desmonta SOLO lo creado en ESTA corrida
#   (clean-slate, D-R3.3) y los vecinos quedan como estaban.
#
# Tokens reemplazados por PCM (los valores van INLINE en los heredocs quoted:
# el script renderizado contiene el conf/site REAL — los goldens validan eso):
#   %%SLUG%%            identificador técnico único por servidor
#   %%ODOO_VERSION%%    17 / 18 / 19
#   %%ODOO_EDITION%%    community / enterprise
#   %%HTTP_PORT%%       puerto http propio
#   %%GEVENT_PORT%%     puerto gevent/websocket propio
#   %%WORKERS%%         workers del odoo.conf (0 = threaded, default multi-Odoo)
#   %%DB_HOST%% %%DB_PORT%% %%DB_NAME%%   conexión PostgreSQL
#   %%PG_USER%% %%PG_PASSWORD%%           credencial PROPIA de la instancia
#   %%DB_LOCAL%%        "1" si la BD vive en este servidor
#   %%DOMAIN%%          dominio de la instancia
#   %%ADMIN_PASSWORD%%  admin_passwd del conf
#   %%IS_DEFAULT%%      "1" solo para la primera instancia (default_server)
#
set -euo pipefail

SLUG="%%SLUG%%"
ODOO_VERSION="%%ODOO_VERSION%%"
DB_LOCAL="%%DB_LOCAL%%"
IS_DEFAULT="%%IS_DEFAULT%%"

ODOO_USER="odoo"
PCM_ROOT="/opt/pcm"
RUNTIME="${PCM_ROOT}/runtime/odoo-${ODOO_VERSION}"
INST_DIR="${PCM_ROOT}/instances/${SLUG}"
CONF="/etc/odoo/${SLUG}.conf"
UNIT="odoo-${SLUG}.service"
SITE="/etc/nginx/sites-available/pcm-${SLUG}.conf"
SITE_LINK="/etc/nginx/sites-enabled/pcm-${SLUG}.conf"
LOGROTATE="/etc/logrotate.d/odoo-${SLUG}"

log() { echo "[pcm-instance ${SLUG}] $*"; }
fail() { echo "$1"; rollback; }

# --- Rollback clean-slate (D-R3.3): SOLO artefactos de ESTA corrida ----------
CREATED_UNIT=0
CREATED_SITE=0
CREATED_DIRS=0
CREATED_USER=0
DB_WAS_ABSENT=0

rollback() {
    set +e
    log "FALLO: desmontando SOLO los artefactos de ${SLUG} (clean-slate)…"
    if [ "${CREATED_UNIT}" = "1" ]; then
        systemctl stop "${UNIT}" 2>/dev/null
        systemctl disable "${UNIT}" 2>/dev/null
        rm -f "/etc/systemd/system/${UNIT}"
        systemctl daemon-reload
    fi
    if [ "${CREATED_SITE}" = "1" ]; then
        rm -f "${SITE_LINK}" "${SITE}"
        nginx -t && systemctl reload nginx
    fi
    # La BD solo se dropea si NO existía antes de esta corrida (nació acá):
    # jamás se toca una base preexistente.
    if [ "${DB_WAS_ABSENT}" = "1" ] && [ "${DB_LOCAL}" = "1" ]; then
        sudo -u postgres dropdb --if-exists "%%DB_NAME%%"
    fi
    if [ "${CREATED_USER}" = "1" ]; then
        sudo -u postgres dropuser --if-exists "%%PG_USER%%"
    fi
    if [ "${CREATED_DIRS}" = "1" ]; then
        rm -rf "${INST_DIR}"
        rm -f "${CONF}" "${LOGROTATE}"
    fi
    echo "PCM_ROLLBACK_DONE"
    exit 1
}
trap rollback ERR

# --- 1. Guardas (nada se crea hasta pasarlas todas) ---------------------------
[ -f "${PCM_ROOT}/.bootstrap-v1" ] || \
    fail "PCM_ERR_NO_BOOTSTRAP: corré bootstrap_server.sh primero."

for puerto in %%HTTP_PORT%% %%GEVENT_PORT%%; do
    if ss -ltn "( sport = :${puerto} )" | grep -q LISTEN; then
        fail "PCM_ERR_PORT_BUSY: el puerto ${puerto} ya está en uso."
    fi
done

if [ -e "${INST_DIR}" ] || [ -e "${CONF}" ] || \
   [ -e "/etc/systemd/system/${UNIT}" ] || [ -e "${SITE}" ]; then
    fail "PCM_ERR_DIRTY_SLUG: hay residuos de ${SLUG}; limpiá antes de reintentar."
fi

libre_mb=$(df -m /opt --output=avail | tail -1 | tr -d ' ')
if [ "${libre_mb}" -lt 3000 ]; then
    fail "PCM_ERR_DISK: menos de 3 GB libres en /opt (${libre_mb} MB)."
fi

if [ "${DB_LOCAL}" = "1" ]; then
    if sudo -u postgres psql -lqt | cut -d'|' -f1 | grep -qw "%%DB_NAME%%"; then
        fail "PCM_ERR_DB_EXISTS: la base %%DB_NAME%% ya existe; no se toca."
    fi
    DB_WAS_ABSENT=1
fi

# --- 2. Snapshot de vecinos (defensa además del health-check del job) --------
VECINOS=$(systemctl list-units --type=service --state=running --plain --no-legend \
          'odoo-*' 2>/dev/null | awk '{print $1}' | grep -v "^${UNIT}$" || true)
log "Vecinos corriendo antes: ${VECINOS:-ninguno}"

# --- 3. Runtime compartido por versión (on-demand, D-R3.5) --------------------
mkdir -p "${PCM_ROOT}/runtime"
(
    flock -w 1800 9
    if [ ! -d "${RUNTIME}/venv" ]; then
        log "Instalando runtime Odoo ${ODOO_VERSION} (una vez por versión)…"
        sudo -u "${ODOO_USER}" git clone --depth 1 --branch "${ODOO_VERSION}.0" \
            https://github.com/odoo/odoo.git "${RUNTIME}/src"
        sudo -u "${ODOO_USER}" python3 -m venv "${RUNTIME}/venv"
        sudo -u "${ODOO_USER}" "${RUNTIME}/venv/bin/pip" install --upgrade pip wheel
        sudo -u "${ODOO_USER}" "${RUNTIME}/venv/bin/pip" install \
            -r "${RUNTIME}/src/requirements.txt"
    fi
) 9>"${PCM_ROOT}/runtime/.lock-${ODOO_VERSION}"

# --- 4. Credencial PostgreSQL PROPIA (D-R3.10; jamás se loguea) ---------------
if [ "${DB_LOCAL}" = "1" ]; then
    if ! sudo -u postgres psql -tc \
        "SELECT 1 FROM pg_roles WHERE rolname='%%PG_USER%%'" | grep -q 1; then
        sudo -u postgres psql -c \
            "CREATE USER \"%%PG_USER%%\" WITH CREATEDB PASSWORD '%%PG_PASSWORD%%'" \
            >/dev/null
        CREATED_USER=1
    fi
fi

# --- 5. Dirs + conf propio (el candado multi-tenant vive ACÁ) -----------------
mkdir -p "${INST_DIR}/data" "${INST_DIR}/addons" "${INST_DIR}/log" /etc/odoo
CREATED_DIRS=1

cat > "${CONF}" <<'CONF'
[options]
admin_passwd = %%ADMIN_PASSWORD%%
db_host = %%DB_HOST%%
db_port = %%DB_PORT%%
db_user = %%PG_USER%%
db_password = %%PG_PASSWORD%%
db_name = %%DB_NAME%%
db_filter = ^%%DB_NAME%%$
list_db = False
http_port = %%HTTP_PORT%%
gevent_port = %%GEVENT_PORT%%
workers = %%WORKERS%%
data_dir = /opt/pcm/instances/%%SLUG%%/data
addons_path = /opt/pcm/runtime/odoo-%%ODOO_VERSION%%/src/addons,/opt/pcm/instances/%%SLUG%%/addons
logfile = /opt/pcm/instances/%%SLUG%%/log/odoo.log
proxy_mode = True
CONF
chown "${ODOO_USER}:${ODOO_USER}" "${CONF}" "${INST_DIR}" -R
chmod 640 "${CONF}"

cat > "${LOGROTATE}" <<'ROTATE'
/opt/pcm/instances/%%SLUG%%/log/odoo.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
ROTATE

# --- 6. Unit systemd propio ----------------------------------------------------
cat > "/etc/systemd/system/${UNIT}" <<'UNIT_EOF'
[Unit]
Description=Odoo (%%SLUG%%)
After=network.target postgresql.service

[Service]
Type=simple
User=odoo
ExecStart=/opt/pcm/runtime/odoo-%%ODOO_VERSION%%/venv/bin/python3 /opt/pcm/runtime/odoo-%%ODOO_VERSION%%/src/odoo-bin -c /etc/odoo/%%SLUG%%.conf
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT_EOF
CREATED_UNIT=1
systemctl daemon-reload
systemctl enable --now "${UNIT}"

# --- 7. Sitio nginx propio (reload, NUNCA restart) -----------------------------
if [ "${IS_DEFAULT}" = "1" ]; then
    cat > "${SITE}" <<'NGINX'
server {
    listen 80 default_server;
    server_name %%DOMAIN%% _;
    location /websocket {
        proxy_pass http://127.0.0.1:%%GEVENT_PORT%%;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location / {
        proxy_pass http://127.0.0.1:%%HTTP_PORT%%;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
else
    cat > "${SITE}" <<'NGINX'
server {
    listen 80;
    server_name %%DOMAIN%%;
    location /websocket {
        proxy_pass http://127.0.0.1:%%GEVENT_PORT%%;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location / {
        proxy_pass http://127.0.0.1:%%HTTP_PORT%%;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
fi
CREATED_SITE=1
ln -sf "${SITE}" "${SITE_LINK}"
nginx -t
systemctl reload nginx

# --- 8. Salud de la nueva (la BD nace en el primer arranque; tarda) -----------
log "Esperando a que la instancia responda en 127.0.0.1:%%HTTP_PORT%%…"
salud=0
for _i in $(seq 1 60); do
    if curl -s -o /dev/null -m 5 "http://127.0.0.1:%%HTTP_PORT%%/web/login"; then
        salud=1; break
    fi
    sleep 5
done
if [ "${salud}" != "1" ]; then
    fail "PCM_ERR_INSTANCE_UNHEALTHY: la instancia nueva no respondió."
fi

# --- 9. SSL best-effort (D-R3.9) ------------------------------------------------
certbot --nginx -n --agree-tos --redirect \
    -m "ops@primate.uy" -d "%%DOMAIN%%" || \
    log "ADVERTENCIA: certbot falló (¿DNS aún no propagado?). Reintentar luego."

# --- 10. Re-verificar vecinos: si alguno cayó, esto es un bug y se ve ROJO -----
for unit in ${VECINOS}; do
    if ! systemctl is-active --quiet "${unit}"; then
        fail "PCM_ERR_NEIGHBOR_DOWN: ${unit} dejó de correr durante el install."
    fi
done

trap - ERR
log "Instancia ${SLUG} instalada y respondiendo."
echo "PCM_INSTALL_OK"
