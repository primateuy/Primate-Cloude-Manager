#!/usr/bin/env bash
# =============================================================================
# gemelo_cloudpepper.sh — lo corre Daryl UNA vez desde su Mac.
#
# Recorre los servidores Cloudpepper de la lista por ssh, en SOLO LECTURA, y
# deja un .json por servidor (el gemelo exacto) en una carpeta local. Ese .json
# se sube en PCM → «Importar servidor de Cloudpepper».
#
# Solo lectura: el script de discovery viaja por la ENTRADA de ssh
# (`bash -s`) y el gemelo vuelve por la SALIDA. No se copia nada al servidor
# ni se escribe nada allá (tampoco .git/index ni .pyc: el script los apaga).
# Lo único que queda allá es lo de cualquier ssh: el login y la línea de sudo
# en los logs del sistema. ssh corre con BatchMode (sin preguntas) y `sudo -n`
# (sin pedir contraseña): si algo pidiera interacción, ese servidor falla y
# se sigue con el próximo.
#
# Lista de servidores: ~/.pcm_produccion/cloudpepper_servidores.txt (fuera del
# repo). Formato en infra/cloudpepper_servidores.example.
#
# Uso:
#   infra/gemelo_cloudpepper.sh                 # salida en ~/pcm_gemelos/<fecha>/
#   infra/gemelo_cloudpepper.sh <carpeta>       # salida donde digas
#   SERVIDORES=<archivo> infra/gemelo_cloudpepper.sh
# =============================================================================
set -uo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${AQUI}/../primate_cloud_manager/data/pcm_readonly_discovery.sh"
SERVIDORES="${SERVIDORES:-$HOME/.pcm_produccion/cloudpepper_servidores.txt}"
SALIDA="${1:-$HOME/pcm_gemelos/$(date +%F)}"
SSH_OPTS=(-T -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30
          -o StrictHostKeyChecking=accept-new)
# Para probar sin servidores: SSH_CMD=<comando que reemplaza a ssh>.
SSH_CMD="${SSH_CMD:-ssh}"

[ -r "$SCRIPT" ] || { echo "No encuentro $SCRIPT"; exit 2; }
[ -r "$SERVIDORES" ] || {
    echo "Falta la lista de servidores: $SERVIDORES"
    echo "Copiá infra/cloudpepper_servidores.example ahí y completala."
    exit 2; }
mkdir -p "$SALIDA"

decodificar() {   # stdin: salida cruda del discovery → stdout: JSON con sangría
    python3 -c '
import base64, gzip, json, sys
texto = sys.stdin.read()
partes = [l[len("PCM_TWIN_B64:"):] for l in texto.splitlines()
          if l.startswith("PCM_TWIN_B64:")]
if not partes or "PCM_TWIN_OK" not in texto:
    sys.exit("salida incompleta (sin PCM_TWIN_OK)")
twin = json.loads(gzip.decompress(base64.b64decode("".join(partes))).decode())
print(json.dumps(twin, ensure_ascii=False, indent=2, sort_keys=True))
'
}

RESUMEN=()
FALLAS=0
while read -r alias destino modo _resto; do
    case "$alias" in ""|\#*) continue ;; esac
    if [ -z "${destino:-}" ]; then
        RESUMEN+=("FALLA  $alias: falta el destino ssh en la lista"); FALLAS=$((FALLAS+1))
        continue
    fi
    case "${modo:-sudo}" in
        root) REMOTO="bash -s" ;;
        sudo) REMOTO="sudo -n bash -s" ;;
        *) RESUMEN+=("FALLA  $alias: modo «${modo}» (vale sudo o root)"); FALLAS=$((FALLAS+1)); continue ;;
    esac
    echo "== $alias ($destino, $modo)"
    crudo="$(mktemp)"
    inicio=$(date +%s)
    "$SSH_CMD" "${SSH_OPTS[@]}" "$destino" "$REMOTO" < "$SCRIPT" > "$crudo" 2> "$SALIDA/$alias.err"
    rc=$?
    if [ "$rc" -eq 0 ] && decodificar < "$crudo" > "$SALIDA/$alias.json" 2>> "$SALIDA/$alias.err"; then
        n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["instances"]))' "$SALIDA/$alias.json")
        RESUMEN+=("OK     $alias: $n instancia(s) Odoo · $(( $(date +%s) - inicio )) s · $SALIDA/$alias.json")
        [ -s "$SALIDA/$alias.err" ] || rm -f "$SALIDA/$alias.err"
    else
        rm -f "$SALIDA/$alias.json"
        RESUMEN+=("FALLA  $alias: ssh rc=$rc — detalle en $SALIDA/$alias.err")
        FALLAS=$((FALLAS+1))
    fi
    rm -f "$crudo"
done < "$SERVIDORES"

echo
echo "RESUMEN (salida en $SALIDA)"
for r in "${RESUMEN[@]}"; do echo "  $r"; done
if [ "$FALLAS" -eq 0 ]; then
    echo "TODO OK — subí cada .json en PCM → Importar servidor de Cloudpepper"
else
    echo "HAY PROBLEMAS ($FALLAS)"
    exit 1
fi
