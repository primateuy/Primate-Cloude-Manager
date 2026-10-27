#!/usr/bin/env bash
# =============================================================================
# subir_dump.sh — lo corre Daryl desde su Mac. Sube el dump neutralizado al
# bucket pcm-dumps-<cuenta> (prefijo incoming/) con el perfil de aws cli
# pcm-prod-provision, mostrando el progreso, y verifica tamaño y sha256.
#
# Una sola vez, configurar el perfil con las claves del usuario pcm-prod-prov:
#   aws configure --profile pcm-prod-provision
#     AWS Access Key ID:      <la del usuario pcm-prod-prov>
#     AWS Secret Access Key:  <la del usuario pcm-prod-prov>
#     Default region name:    us-east-1
#     Default output format:  json
#
# Uso:
#   infra/subir_dump.sh <archivo.zip> [cliente]      (cliente: barraca-julia)
#
# Verificación:
#   - antes de subir: el sha256 local tiene que coincidir con el SHA256SUMS.txt
#     que dejó el generador al lado del zip;
#   - durante: aws cli manda un checksum SHA256 por parte y S3 rechaza la que
#     no coincida;
#   - después: head-object confirma tamaño exacto y el sha256 completo que
#     viaja como metadato (x-amz-meta-sha256).
# =============================================================================
set -euo pipefail

PERFIL="${PERFIL:-pcm-prod-provision}"
# Usuario IAM dueño de las keys del perfil: si responde otro, se frena.
USUARIO_ESPERADO="${USUARIO_ESPERADO:-pcm-prod-prov}"
REGION="us-east-1"
ARCHIVO="${1:?uso: infra/subir_dump.sh <archivo.zip> [cliente]}"
CLIENTE="${2:-barraca-julia}"
[ -f "$ARCHIVO" ] || { echo "No existe $ARCHIVO"; exit 2; }
NOMBRE="$(basename "$ARCHIVO")"

CUENTA="$(aws sts get-caller-identity --profile "$PERFIL" --query Account --output text)"
USUARIO="$(aws sts get-caller-identity --profile "$PERFIL" --query Arn --output text)"
echo "Respondió: $USUARIO"
case "$USUARIO" in
    *:user/"$USUARIO_ESPERADO"|*:user/*/"$USUARIO_ESPERADO") echo "usuario: $USUARIO_ESPERADO ✓" ;;
    *) echo "El perfil $PERFIL no es el usuario $USUARIO_ESPERADO ($USUARIO): ¿keys cruzadas? Freno."; exit 2 ;;
esac
BUCKET="pcm-dumps-${CUENTA}"
KEY="incoming/${CLIENTE}/${NOMBRE}"
DESTINO="s3://${BUCKET}/${KEY}"

TAMANO=$(stat -f%z "$ARCHIVO" 2>/dev/null || stat -c%s "$ARCHIVO")
echo "Archivo: $ARCHIVO ($(awk -v b="$TAMANO" 'BEGIN{printf "%.2f GB", b/1073741824}'))"
echo "Destino: $DESTINO  (cuenta $CUENTA, perfil $PERFIL)"

echo "Calculando sha256 local…"
SHA=$(shasum -a 256 "$ARCHIVO" | awk '{print $1}')
SUMS="$(dirname "$ARCHIVO")/SHA256SUMS.txt"
if [ -f "$SUMS" ]; then
    ESPERADO=$(awk -v n="$NOMBRE" '$2 == n {print $1}' "$SUMS" | tail -1)
    if [ -n "$ESPERADO" ] && [ "$ESPERADO" != "$SHA" ]; then
        echo "El sha256 local ($SHA) NO coincide con $SUMS ($ESPERADO). Freno."
        exit 1
    fi
    [ -n "$ESPERADO" ] && echo "sha256 = SHA256SUMS.txt ✓"
fi
echo "sha256: $SHA"

# Partes grandes y paralelas: 11 GB en partes de 64 MB = ~180 partes.
export AWS_PROFILE="$PERFIL"
aws configure set s3.multipart_chunksize 64MB --profile "$PERFIL"
aws configure set s3.max_concurrent_requests 16 --profile "$PERFIL"

echo "Subiendo (el progreso lo muestra aws cli)…"
aws s3 cp "$ARCHIVO" "$DESTINO" --region "$REGION" --sse AES256 \
    --checksum-algorithm SHA256 --metadata "sha256=${SHA}"

echo "Verificando en S3…"
read -r REMOTO_TAM REMOTO_SHA < <(aws s3api head-object --region "$REGION" \
    --bucket "$BUCKET" --key "$KEY" \
    --query '[ContentLength, Metadata.sha256]' --output text)
OK=1
[ "$REMOTO_TAM" = "$TAMANO" ] && echo "tamaño: $REMOTO_TAM bytes ✓" \
    || { echo "tamaño: S3 $REMOTO_TAM ≠ local $TAMANO ✗"; OK=0; }
[ "$REMOTO_SHA" = "$SHA" ] && echo "sha256: ✓" \
    || { echo "sha256: S3 $REMOTO_SHA ≠ local $SHA ✗"; OK=0; }
[ "$OK" = 1 ] || { echo "HAY PROBLEMAS: no restaurar este archivo."; exit 1; }

echo
echo "LISTO. Ruta para PCM → «Restaurar desde un dump…» (Key S3 manual):"
echo "  $DESTINO"
