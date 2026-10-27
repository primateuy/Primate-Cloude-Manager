#!/usr/bin/env bash
# =============================================================================
# verificar.sh — verificación de SOLO LECTURA de los permisos de PCM
# (Primate Cloud Manager, el módulo de Odoo con el que Primate gobierna sus
# servidores Odoo en AWS). Para pegar en AWS CloudShell de la cuenta, en
# us-east-1, DESPUÉS de desplegar el stack pcm-prerequisitos.yaml.
#
# QUÉ NO HACE: no crea, modifica ni borra nada. Usa sólo lecturas
# (sts get-caller-identity, ec2 describe-*, rds describe-*) y el Policy
# Simulator de IAM (simulate-principal-policy), que EVALÚA las policies de un
# usuario contra una acción hipotética sin ejecutarla.
#
# CONTEXTO (para leerlo sin el resto del repo):
#   - La etiqueta «primate:managed_by = pcm» es la LLAVE de propiedad: PCM sólo
#     puede operar recursos que la tengan, y la etiqueta sólo se puede poner en
#     el mismo llamado que crea el recurso. Ponérsela o sacársela a algo que
#     ya existe está prohibido con un Deny explícito.
#   - pcm-prod-ro   (policy pcm-prod-readonly): lee el inventario y corre SÓLO
#     el documento SSM de lectura «pcm-readonly-discovery».
#   - pcm-prod-prov (policy pcm-prod-provision): crea servidores NUEVOS, siempre
#     con la llave, y opera sólo esos. Sube dumps sólo a incoming/ del bucket
#     pcm-dumps-<cuenta>.
#
# LAS PRUEBAS:
#   1. Regiones con EC2 (cuántas instancias hay en cada una). Informativa.
#   2. Recursos EC2/RDS que YA tienen la llave (cualquier mayúscula). Antes del
#      primer montaje con PCM tiene que dar vacío: si no, alguien la puso a
#      mano y ese recurso quedaría operable por pcm-prod-prov.
#   3. pcm-prod-ro no puede modificar nada; sí lee y corre el documento de lectura.
#   4. pcm-prod-prov no toca instancias SIN la llave.
#   5. pcm-prod-prov no agrega ni borra la llave en recursos existentes.
#   6. pcm-prod-prov sí crea una instancia CON la llave (y no sin ella), opera
#      las suyas, sube dumps sólo a incoming/ de su bucket de ESTA cuenta y
#      sólo lee el parámetro público de la AMI de Ubuntu.
#   7. pcm-ssm-role (el rol de las EC2 de PCM): lee/escribe sólo en los
#      buckets pcm-<uso>-<cuenta> de ESTA cuenta.
#
# Las pruebas de IAM usan ARNs hipotéticos (i-0000000000ajena0, «otro-bucket»):
# el simulador no necesita que existan. «denegado» = implicitDeny o
# explicitDeny; «permitido» = allowed.
#
# QUIÉN LO CORRE: un administrador de la cuenta, en CloudShell (necesita
# iam:SimulatePrincipalPolicy y los Describe de EC2/RDS). Bash 3.2+.
# Al final: OK/FALLA por prueba y «TODO OK» (exit 0) o «HAY PROBLEMAS» (exit 1).
#
# Uso:  bash verificar.sh            (nombres por defecto: pcm-prod-ro, pcm-prod-prov,
#                                     pcm-ssm-role, pcm-readonly-discovery)
#       SUFIJO=-cfn bash verificar.sh  (si el stack se desplegó con el parámetro Sufijo)
#       REGION=us-east-1 bash verificar.sh
# =============================================================================
set -uo pipefail

SUFIJO="${SUFIJO:-}"
RO_USER="${RO_USER:-pcm-prod-ro${SUFIJO}}"
PROV_USER="${PROV_USER:-pcm-prod-prov${SUFIJO}}"
ROL="${ROL:-pcm-ssm-role${SUFIJO}}"
REGION="${REGION:-us-east-1}"
LLAVE="primate:managed_by"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
if [ -z "$ACCOUNT" ] || [ "$ACCOUNT" = "None" ]; then
    echo "No pude leer la cuenta (aws sts get-caller-identity). ¿Estás en CloudShell?"
    exit 2
fi
ARN_RO="arn:aws:iam::${ACCOUNT}:user/${RO_USER}"
ARN_PROV="arn:aws:iam::${ACCOUNT}:user/${PROV_USER}"
ARN_ROL="arn:aws:iam::${ACCOUNT}:role/${ROL}"
INST_AJENA="arn:aws:ec2:${REGION}:${ACCOUNT}:instance/i-0000000000ajena0"
INST_NUEVA="arn:aws:ec2:${REGION}:${ACCOUNT}:instance/*"
DOC_LECTURA="arn:aws:ssm:${REGION}:${ACCOUNT}:document/pcm-readonly-discovery${SUFIJO}"
DOC_SHELL="arn:aws:ssm:${REGION}::document/AWS-RunShellScript"
BUCKET="pcm-dumps-${ACCOUNT}${SUFIJO}"
# El simulador no completa aws:ResourceAccount solo: se le pasa, la propia y una
# ajena. La ajena es un número de 12 dígitos distinto, derivado de la propia
# (sólo existe dentro del simulador: no se consulta ninguna otra cuenta).
CUENTA_AJENA="$(printf '%s' "$ACCOUNT" | tr '0-9' '1-90')"
CTX_CUENTA_PROPIA=("ContextKeyName=aws:ResourceAccount,ContextKeyValues=${ACCOUNT},ContextKeyType=string")
CTX_CUENTA_AJENA=("ContextKeyName=aws:ResourceAccount,ContextKeyValues=${CUENTA_AJENA},ContextKeyType=string")

CTX_CON_LLAVE=("ContextKeyName=aws:ResourceTag/${LLAVE},ContextKeyValues=pcm,ContextKeyType=string")
CTX_PIDE_LLAVE=("ContextKeyName=aws:TagKeys,ContextKeyValues=${LLAVE},ContextKeyType=stringList"
                "ContextKeyName=aws:RequestTag/${LLAVE},ContextKeyValues=pcm,ContextKeyType=string")
CTX_PIDE_LLAVE_MAYUS=("ContextKeyName=aws:TagKeys,ContextKeyValues=Primate:Managed_By,ContextKeyType=stringList")

RESUMEN=()
FALLAS=0

linea() { printf '%s\n' "------------------------------------------------------------------"; }

anotar() {   # anotar <OK|FALLA> <nombre> [detalle]
    local estado="$1" nombre="$2" detalle="${3:-}"
    RESUMEN+=("$(printf '%-6s %s' "$estado" "$nombre")")
    [ "$estado" = "OK" ] || FALLAS=$((FALLAS + 1))
    printf '  → %s%s\n' "$estado" "${detalle:+: $detalle}"
}

# simular <arn-usuario> <recurso> <"acc1 acc2 ..."> [contextos...]
# Imprime «acción<TAB>decisión» por línea. Devuelve !=0 si la API falla.
simular() {
    local usuario="$1" recurso="$2" acciones="$3"; shift 3
    local args=(iam simulate-principal-policy --policy-source-arn "$usuario"
                --resource-arns "$recurso" --output text
                --query 'EvaluationResults[].[EvalActionName,EvalDecision]'
                --action-names)
    # shellcheck disable=SC2206
    args+=($acciones)
    if [ "$#" -gt 0 ]; then
        args+=(--context-entries "$@")
    fi
    aws "${args[@]}" 2>&1
}

# prueba <nombre> <permitido|denegado> <arn-usuario> <recurso> <"acciones"> [contextos...]
prueba() {
    local nombre="$1" esperado="$2" usuario="$3" recurso="$4" acciones="$5"; shift 5
    local salida rc malas
    salida="$(simular "$usuario" "$recurso" "$acciones" "$@")"; rc=$?
    echo "  · ${nombre}"
    if [ "$rc" -ne 0 ] || ! grep -qE $'\t(allowed|implicitDeny|explicitDeny)$' <<<"$salida"; then
        anotar FALLA "$nombre" "el simulador no respondió: $(head -c 300 <<<"$salida")"
        return
    fi
    malas="$(awk -F'\t' -v e="$esperado" '
        { ok = (e == "permitido") ? ($2 == "allowed") : ($2 != "allowed")
          if (!ok) printf "%s=%s ", $1, $2 }' <<<"$salida")"
    if [ -n "$malas" ]; then
        anotar FALLA "$nombre" "esperado ${esperado}; dio ${malas}"
    else
        anotar OK "$nombre"
    fi
}

echo "Cuenta: ${ACCOUNT} · usuarios: ${RO_USER} / ${PROV_USER} · región de las pruebas: ${REGION}"
linea

# --- 1. Regiones con EC2 ------------------------------------------------------
echo "1. Regiones con EC2"
REGIONES="$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text 2>/dev/null)"
if [ -z "$REGIONES" ]; then
    anotar FALLA "1. listar regiones" "describe-regions no respondió"
else
    CON_EC2=""
    for r in $REGIONES; do
        n="$(aws ec2 describe-instances --region "$r" \
             --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo "?")"
        if [ "$n" != "0" ]; then
            printf '  %-16s %s instancia(s)\n' "$r" "$n"
            CON_EC2="${CON_EC2} ${r}"
        fi
    done
    [ -n "$CON_EC2" ] || echo "  (ninguna región con instancias)"
    anotar OK "1. regiones con EC2:${CON_EC2:- ninguna}"
fi
linea

# --- 2. Recursos que YA tienen la llave ----------------------------------------
echo "2. Recursos que YA tienen la etiqueta ${LLAVE} (cualquier mayúscula)"
ETIQUETADOS=""
for r in $REGIONES; do
    ec2="$(aws ec2 describe-tags --region "$r" --output text \
           --query 'Tags[].[ResourceType,ResourceId,Key,Value]' 2>/dev/null \
           | grep -i "$LLAVE" | sed "s/^/${r}  /")"
    rds="$(aws rds describe-db-instances --region "$r" --output text \
           --query "DBInstances[].[DBInstanceIdentifier,join(',',TagList[].Key)]" 2>/dev/null \
           | grep -i "$LLAVE" | sed "s/^/${r}  rds  /")"
    for x in "$ec2" "$rds"; do
        [ -n "$x" ] && ETIQUETADOS="${ETIQUETADOS}${x}"$'\n'
    done
done
if [ -z "$ETIQUETADOS" ]; then
    echo "  (ninguno)"
    anotar OK "2. ningún recurso tiene la llave todavía"
else
    printf '%s' "$ETIQUETADOS" | sed 's/^/  /'
    anotar FALLA "2. hay recursos con la llave" \
        "si todavía no montaste nada con PCM, alguien la puso a mano: FRENAR"
fi
linea

# --- 3. pcm-prod-ro no modifica nada -------------------------------------------
echo "3. ${RO_USER}: no puede modificar nada"
prueba "3a. EC2: crear, ciclo de vida, etiquetas, red y discos" denegado "$ARN_RO" "*" \
    "ec2:RunInstances ec2:StartInstances ec2:StopInstances ec2:RebootInstances ec2:TerminateInstances ec2:ModifyInstanceAttribute ec2:CreateTags ec2:DeleteTags ec2:CreateSecurityGroup ec2:AuthorizeSecurityGroupIngress ec2:RevokeSecurityGroupIngress ec2:AllocateAddress ec2:AssociateAddress ec2:ReleaseAddress ec2:CreateSnapshot ec2:DeleteVolume ec2:DetachVolume"
prueba "3b. RDS, Route 53, S3 e IAM" denegado "$ARN_RO" "*" \
    "rds:ModifyDBInstance rds:DeleteDBInstance rds:RebootDBInstance route53:ChangeResourceRecordSets route53:CreateHostedZone s3:PutObject s3:DeleteObject s3:CreateBucket iam:CreateUser iam:AttachUserPolicy iam:PutUserPolicy iam:CreateAccessKey"
prueba "3c. SSM: sesiones, shell y documentos" denegado "$ARN_RO" "*" \
    "ssm:StartSession ssm:CreateDocument ssm:UpdateDocument ssm:DeleteDocument ssm:PutParameter"
prueba "3d. SSM: shell arbitrario (AWS-RunShellScript)" denegado "$ARN_RO" "$DOC_SHELL" \
    "ssm:SendCommand"
prueba "3e. control: sí lee y corre SOLO el documento de lectura" permitido "$ARN_RO" "$DOC_LECTURA" \
    "ssm:SendCommand"
prueba "3f. control: sí lee el inventario" permitido "$ARN_RO" "*" \
    "ec2:DescribeInstances ec2:DescribeTags rds:DescribeDBInstances"
linea

# --- 4. pcm-prod-prov no toca instancias sin la llave ----------------------------
echo "4. ${PROV_USER}: no toca instancias SIN la llave"
prueba "4a. ciclo de vida de una instancia ajena" denegado "$ARN_PROV" "$INST_AJENA" \
    "ec2:StartInstances ec2:StopInstances ec2:RebootInstances ec2:TerminateInstances ec2:ModifyInstanceAttribute"
prueba "4b. shell (SendCommand) sobre una instancia ajena" denegado "$ARN_PROV" "$INST_AJENA" \
    "ssm:SendCommand"
linea

# --- 5. pcm-prod-prov no agrega ni borra la llave en existentes --------------------
echo "5. ${PROV_USER}: no agrega ni borra la llave en recursos existentes"
prueba "5a. ponerle la llave a una instancia ajena" denegado "$ARN_PROV" "$INST_AJENA" \
    "ec2:CreateTags" "${CTX_PIDE_LLAVE[@]}"
prueba "5b. ponerle la llave con otras mayúsculas" denegado "$ARN_PROV" "$INST_AJENA" \
    "ec2:CreateTags" "${CTX_PIDE_LLAVE_MAYUS[@]}"
prueba "5c. re-etiquetar con la llave algo propio" denegado "$ARN_PROV" "$INST_AJENA" \
    "ec2:CreateTags" "${CTX_PIDE_LLAVE[@]}" "${CTX_CON_LLAVE[@]}"
prueba "5d. borrar etiquetas (incluida la llave)" denegado "$ARN_PROV" "$INST_AJENA" \
    "ec2:DeleteTags" "ContextKeyName=aws:TagKeys,ContextKeyValues=${LLAVE},ContextKeyType=stringList" \
    "${CTX_CON_LLAVE[@]}"
linea

# --- 6. pcm-prod-prov sí crea con la llave --------------------------------------
echo "6. ${PROV_USER}: sí crea una instancia CON la llave"
prueba "6a. RunInstances con primate:managed_by=pcm" permitido "$ARN_PROV" "$INST_NUEVA" \
    "ec2:RunInstances" "${CTX_PIDE_LLAVE[@]}"
prueba "6b. la etiqueta dentro de la creación (CreateAction=RunInstances)" permitido "$ARN_PROV" "$INST_NUEVA" \
    "ec2:CreateTags" "${CTX_PIDE_LLAVE[@]}" \
    "ContextKeyName=ec2:CreateAction,ContextKeyValues=RunInstances,ContextKeyType=string"
prueba "6c. RunInstances SIN la llave" denegado "$ARN_PROV" "$INST_NUEVA" \
    "ec2:RunInstances" "ContextKeyName=aws:RequestTag/primate:client,ContextKeyValues=x,ContextKeyType=string"
prueba "6d. opera su propia instancia (con la llave)" permitido "$ARN_PROV" "$INST_AJENA" \
    "ec2:StartInstances ec2:StopInstances" "${CTX_CON_LLAVE[@]}"
prueba "6e. sube el dump a incoming/ de su bucket" permitido "$ARN_PROV" \
    "arn:aws:s3:::${BUCKET}/incoming/cliente/dump.zip" "s3:PutObject" \
    "${CTX_CUENTA_PROPIA[@]}"
prueba "6f. no sube a otro bucket" denegado "$ARN_PROV" \
    "arn:aws:s3:::otro-bucket/incoming/dump.zip" "s3:PutObject" \
    "${CTX_CUENTA_PROPIA[@]}"
prueba "6g. no sube a un bucket con el mismo nombre de OTRA cuenta" denegado "$ARN_PROV" \
    "arn:aws:s3:::${BUCKET}/incoming/cliente/dump.zip" "s3:PutObject" \
    "${CTX_CUENTA_AJENA[@]}"
prueba "6h. lee el parámetro público de la AMI de Ubuntu" permitido "$ARN_PROV" \
    "arn:aws:ssm:${REGION}::parameter/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id" \
    "ssm:GetParameter"
prueba "6i. no lee parámetros de la cuenta (SecureString)" denegado "$ARN_PROV" \
    "arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/cualquier/clave" "ssm:GetParameter"
linea

# --- 7. pcm-ssm-role: sólo los buckets de PCM de esta cuenta --------------------
echo "7. ${ROL}: lee y escribe sólo en los buckets de PCM de ESTA cuenta"
prueba "7a. lee y escribe en pcm-backups-<cuenta>" permitido "$ARN_ROL" \
    "arn:aws:s3:::pcm-backups-${ACCOUNT}${SUFIJO}/pcm-backups/x.dump" \
    "s3:GetObject s3:PutObject" "${CTX_CUENTA_PROPIA[@]}"
prueba "7b. no escribe en un bucket que no es de PCM" denegado "$ARN_ROL" \
    "arn:aws:s3:::otro-bucket/x.dump" "s3:PutObject" "${CTX_CUENTA_PROPIA[@]}"
prueba "7c. no escribe en un bucket pcm-* de OTRA cuenta" denegado "$ARN_ROL" \
    "arn:aws:s3:::pcm-backups-${ACCOUNT}${SUFIJO}/x.dump" "s3:PutObject" \
    "${CTX_CUENTA_AJENA[@]}"
linea

echo "RESUMEN"
for r in "${RESUMEN[@]}"; do echo "  $r"; done
linea
if [ "$FALLAS" -eq 0 ]; then
    echo "TODO OK"
else
    echo "HAY PROBLEMAS (${FALLAS})"
    exit 1
fi
