#!/usr/bin/env bash
# =============================================================================
# armar_entregable_inco.sh — arma entregables/inco/pcm_permisos_<fecha>.zip
# con lo que revisa INCO: las dos plantillas, verificar.sh y README_INCO.md
# (también en PDF).
#
# Antes de cerrar el zip, verifica que las plantillas estén al día con el
# repo (generar.py --check) y que NINGÚN archivo tenga números de cuenta,
# IPs, access keys, correos ni nombres de clientes. Si algo aparece, no arma
# el zip.
#
# Uso (en la raíz del repo):  infra/armar_entregable_inco.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
"$PY" infra/cloudformation/generar.py --check

ARCHIVOS=(
    infra/cloudformation/pcm-prerequisitos.yaml
    infra/cloudformation/pcm-ssm-readonly.yaml
    infra/cloudshell/verificar.sh
    infra/README_INCO.md
)
# Nombres de clientes y proyectos que nunca van a un tercero.
CLIENTES='barraca|julia|bj[-_]|campera|redomon|innobyt|forum|sagui|ox[ií]moro|odontolog|primate\.com\.uy'
PATRONES=(
    "[0-9]{12}"                                   # número de cuenta AWS
    "([0-9]{1,3}\.){3}[0-9]{1,3}"                 # IPv4
    "AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"           # access keys
    "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}" # correos
    "$CLIENTES"
)
PROBLEMAS=0
for f in "${ARCHIVOS[@]}"; do
    [ -f "$f" ] || { echo "falta $f"; exit 2; }
    for p in "${PATRONES[@]}"; do
        if hallado="$(grep -n -i -E "$p" "$f")"; then
            echo "✗ $f — patrón «$p»:"
            printf '%s\n' "$hallado" | head -5 | sed 's/^/    /'
            PROBLEMAS=$((PROBLEMAS + 1))
        fi
    done
done
if [ "$PROBLEMAS" -ne 0 ]; then
    echo "NO se arma el zip: hay $PROBLEMAS hallazgo(s)."
    exit 1
fi
echo "✓ sin números de cuenta, IPs, keys, correos ni nombres de clientes"

FECHA="$(date +%F)"
DESTINO="entregables/inco"
ZIP="$DESTINO/pcm_permisos_${FECHA}.zip"
mkdir -p "$DESTINO"
rm -f "$ZIP"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/pcm_permisos_${FECHA}"
for f in "${ARCHIVOS[@]}"; do
    cp "$f" "$TMP/pcm_permisos_${FECHA}/"
done
chmod 755 "$TMP/pcm_permisos_${FECHA}/verificar.sh"
# El README también en PDF (mismo contenido, ya revisado arriba).
"$PY" infra/readme_a_pdf.py infra/README_INCO.md \
    "$TMP/pcm_permisos_${FECHA}/README_INCO.pdf"
echo "✓ README_INCO.pdf"
(cd "$TMP" && zip -q -r -X "pcm_permisos_${FECHA}.zip" "pcm_permisos_${FECHA}")
mv "$TMP/pcm_permisos_${FECHA}.zip" "$ZIP"
echo "✓ $ZIP"
unzip -l "$ZIP" | sed -n '4,8p'
shasum -a 256 "$ZIP"
