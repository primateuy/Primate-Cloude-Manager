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
#   %%PCM_PROFILE%%     "1" si la instancia hospeda un PCM (perfil reutilizable):
#                       pip extra en el venv del runtime, conf con jobrunner de
#                       queue_job + jobs largos, y clave Fernet generada EN el
#                       servidor (EnvironmentFile; nunca pasa por stdout/SSM).
#
set -euo pipefail

SLUG="%%SLUG%%"
ODOO_VERSION="%%ODOO_VERSION%%"
DB_LOCAL="%%DB_LOCAL%%"
IS_DEFAULT="%%IS_DEFAULT%%"
PCM_PROFILE="%%PCM_PROFILE%%"

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
CREATED_ENV=0
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
    # El env file (clave Fernet) solo se borra si nació en ESTA corrida: una
    # clave preexistente puede tener credenciales reales cifradas detrás.
    if [ "${CREATED_ENV}" = "1" ]; then
        rm -f "/etc/odoo/${SLUG}.env"
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
# El stdout de SSM se trunca a ~24KB (hallazgo real de B4): el detalle de
# clone/pip va a un log en el servidor; el stdout queda para log()+marcadores.
RUNTIME_LOG="/var/log/pcm-runtime-odoo-${ODOO_VERSION}.log"
(
    flock -w 1800 9
    if [ ! -d "${RUNTIME}/venv" ]; then
        log "Instalando runtime Odoo ${ODOO_VERSION} (una vez por versión; detalle en ${RUNTIME_LOG})…"
        sudo -u "${ODOO_USER}" git clone --depth 1 --branch "${ODOO_VERSION}.0" \
            https://github.com/odoo/odoo.git "${RUNTIME}/src" \
            >>"${RUNTIME_LOG}" 2>&1
        sudo -u "${ODOO_USER}" python3 -m venv "${RUNTIME}/venv" \
            >>"${RUNTIME_LOG}" 2>&1
        sudo -u "${ODOO_USER}" "${RUNTIME}/venv/bin/pip" install --upgrade pip wheel \
            >>"${RUNTIME_LOG}" 2>&1
        sudo -u "${ODOO_USER}" "${RUNTIME}/venv/bin/pip" install \
            -r "${RUNTIME}/src/requirements.txt" >>"${RUNTIME_LOG}" 2>&1
    fi
) 9>"${PCM_ROOT}/runtime/.lock-${ODOO_VERSION}"

# --- 3b. Perfil PCM: dependencias Python de PCM en el venv del runtime -------
#     Idempotente y fuera del lock del runtime: corre también cuando el
#     runtime ya existía (pip resuelve "ya instalado" en segundos).
if [ "${PCM_PROFILE}" = "1" ]; then
    log "Perfil PCM: instalando dependencias Python (detalle en ${RUNTIME_LOG})…"
    sudo -u "${ODOO_USER}" "${RUNTIME}/venv/bin/pip" install \
        boto3 botocore paramiko cryptography PyGithub \
        >>"${RUNTIME_LOG}" 2>&1
fi

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
dbfilter = ^%%DB_NAME%%$
list_db = False
http_port = %%HTTP_PORT%%
gevent_port = %%GEVENT_PORT%%
workers = %%WORKERS%%
data_dir = /opt/pcm/instances/%%SLUG%%/data
addons_path = /opt/pcm/runtime/odoo-%%ODOO_VERSION%%/src/addons,/opt/pcm/instances/%%SLUG%%/addons
logfile = /opt/pcm/instances/%%SLUG%%/log/odoo.log
proxy_mode = True
CONF

# Perfil PCM: jobrunner de queue_job + jobs largos (lección bj-uat: con el
# limit_time_real default Odoo se reinicia a mitad de un restore y queue_job
# lo relanza). Las claves sueltas siguen bajo [options]; [queue_job] va al
# final a propósito.
if [ "${PCM_PROFILE}" = "1" ]; then
    cat >> "${CONF}" <<'CONFPCM'
server_wide_modules = base,web,queue_job
limit_time_real = 14400
limit_time_real_cron = -1
max_cron_threads = 2

[queue_job]
channels = root:2
CONFPCM
fi
chown "${ODOO_USER}:${ODOO_USER}" "${CONF}" "${INST_DIR}" -R
chmod 640 "${CONF}"

# --- 5b. Perfil PCM: queue_job ANTES del primer arranque ----------------------
#     El conf del perfil lleva server_wide_modules = base,web,queue_job: sin el
#     módulo en el addons_path, Odoo no arranca y el health check de abajo
#     fallaría siempre. El clone es del repo PÚBLICO de OCA; el módulo entra
#     como symlink al tope del addons dir (los módulos se cargan de PRIMER
#     nivel, mismo criterio que el deploy del companion).
if [ "${PCM_PROFILE}" = "1" ]; then
    log "Perfil PCM: clonando OCA queue_job ${ODOO_VERSION}.0 (detalle en ${RUNTIME_LOG})…"
    sudo -u "${ODOO_USER}" mkdir -p "${INST_DIR}/repos"
    sudo -u "${ODOO_USER}" git clone --depth 1 --branch "${ODOO_VERSION}.0" \
        https://github.com/OCA/queue "${INST_DIR}/repos/oca-queue" \
        >>"${RUNTIME_LOG}" 2>&1
    sudo -u "${ODOO_USER}" ln -s "../repos/oca-queue/queue_job" \
        "${INST_DIR}/addons/queue_job"
fi

# --- 5c. Perfil PCM: clave Fernet propia, generada EN el servidor -------------
#     La clave NO pasa por stdout ni por el comando SSM (historial ~30 días):
#     se genera acá y aterriza directo en el env file (root:odoo, 640) que la
#     unit carga vía EnvironmentFile. Si el archivo ya existe, NO se pisa
#     (puede tener credenciales reales cifradas detrás).
ENVF="/etc/odoo/${SLUG}.env"
if [ "${PCM_PROFILE}" = "1" ] && [ ! -f "${ENVF}" ]; then
    umask 077
    printf 'PRIMATE_CLOUD_FERNET_KEY=%s\n' \
        "$("${RUNTIME}/venv/bin/python3" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
        > "${ENVF}"
    umask 022
    chown root:"${ODOO_USER}" "${ENVF}"
    chmod 640 "${ENVF}"
    CREATED_ENV=1
    log "Perfil PCM: clave de cifrado generada en ${ENVF} (no viaja por SSM)."
fi

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
# El «-» hace opcional el env file: solo existe con el perfil PCM (clave
# Fernet); en el resto de las instancias la línea es inocua.
EnvironmentFile=-/etc/odoo/%%SLUG%%.env
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
    # El administrador de bases NO se publica (además de list_db = False).
    location ^~ /web/database {
        return 404;
    }
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
        proxy_set_header X-Forwarded-Host $host;
    }
}
NGINX
else
    cat > "${SITE}" <<'NGINX'
server {
    listen 80;
    server_name %%DOMAIN%%;
    # El administrador de bases NO se publica (además de list_db = False).
    location ^~ /web/database {
        return 404;
    }
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
        proxy_set_header X-Forwarded-Host $host;
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

# --- 9. SSL: ya NO corre acá (D-R3.9 endurecida). El certificado lo emite el
#     paso _provision_ssl del modelo, DESPUÉS de confirmar la propagación del
#     DNS (Route53 INSYNC + dig contra los NS autoritativos): un certbot acá
#     corría a ciegas antes del DNS y fallaba siempre en un montaje fresco.

# --- 10. Re-verificar vecinos: si alguno cayó, esto es un bug y se ve ROJO -----
for unit in ${VECINOS}; do
    if ! systemctl is-active --quiet "${unit}"; then
        fail "PCM_ERR_NEIGHBOR_DOWN: ${unit} dejó de correr durante el install."
    fi
done

trap - ERR
log "Instancia ${SLUG} instalada y respondiendo."
echo "PCM_INSTALL_OK"
