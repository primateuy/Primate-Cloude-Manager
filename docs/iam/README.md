# IAM de la cuenta productiva para PCM

Dos policies, en **dos usuarios IAM distintos** y **dos cuentas en PCM**:

| Usuario IAM | Policy | Cuenta en PCM | Cuándo |
|---|---|---|---|
| `pcm-prod-ro` | `pcm-prod-readonly.json` | «Prod — solo lectura» (normal) | Desde el enlace: inventario, importación y gemelo. |
| `pcm-prod-prov` | `pcm-prod-provision.json` | «Prod — aprovisionamiento», con **Solo aprovisionamiento** marcado | Recién para montar Barraca Julia. |

### Despliegue: CloudFormation (recomendado, 2026-10-27)

`infra/cloudformation/pcm-prerequisitos.yaml` crea todo en un stack, sin
reemplazar nada a mano (pseudo-parámetros): documento SSM, rol/instance
profile `pcm-ssm-role`, bucket `pcm-dumps-<cuenta>`, las dos managed policies
y los dos usuarios (cada uno con SOLO su policy, sin consola, sin access
keys). Se **genera** desde estos JSON con `infra/cloudformation/generar.py`
(no se edita a mano; `--check` falla si quedó vieja). Al generar se quitan los
statements opcionales que dependen de algo que la plantilla no crea:
`DnsSoloNombresDePCMEnLaZonaReal` (sin Route 53) y
`SalidaDelDiscoveryPorS3Opcional`. Verificación posterior:
`infra/cloudshell/verificar.sh` en CloudShell.

`pcm-prod-provision` incluye `SubirDumpsSoloAlBucketDePCM`: `s3:PutObject` y
`s3:AbortMultipartUpload` **solo** sobre `pcm-dumps-<cuenta>/incoming/*`, para
que `infra/subir_dump.sh` suba el dump con ese mismo usuario (perfil
`pcm-prod-provision`).

### Por qué dos usuarios y no las dos policies en uno

IAM autoriza `ssm:SendCommand` evaluando **cada recurso por separado** (el
documento y la instancia). La readonly necesita permitir `SendCommand` sobre
`instance/*` (para correr el documento de lectura en los importados, que no
tienen etiqueta), y la de provisión necesita permitir el documento
`AWS-RunShellScript`. **En un mismo usuario se combinan**: el documento lo
autoriza una y la instancia importada la otra → shell arbitrario sobre un
servidor importado. Un `Deny` no lo puede cerrar sin una condición por nombre
de documento, que IAM no ofrece. Separados, cada usuario es seguro solo:

- `pcm-prod-ro` no puede usar ningún documento salvo `pcm-readonly-discovery`.
- `pcm-prod-prov` solo puede hacer `SendCommand` sobre instancias con
  `ssm:resourceTag/primate:managed_by = pcm`: un importado no la tiene.

En PCM, la cuenta de aprovisionamiento lleva **Solo aprovisionamiento**: su
sync ignora toda EC2 sin la etiqueta de PCM y no trae DNS ni RDS (lo ajeno lo
observa SOLO la cuenta de solo lectura), y la importación de servidores
queda bloqueada para ella.

## Etiqueta de propiedad

Las condiciones usan la etiqueta que PCM ya pone en todo lo que crea:
`primate:managed_by = pcm` (helper `build_resource_tags`, en instancia,
volumen, security group y Elastic IP). Un servidor importado **no** la tiene.

La etiqueta es la **llave de seguridad**: si alguien pudiera ponérsela a una
instancia importada, `pcm-prod-prov` la podría operar. Por eso, en **las dos**
policies:

- `ec2:CreateTags` **solo** se permite dentro de la creación
  (`ec2:CreateAction` = `RunInstances` / `CreateVolume` /
  `CreateSecurityGroup` / `AllocateAddress`): PCM etiqueta todo con
  `TagSpecifications` en el mismo llamado que crea el recurso.
- `NadieTocaLaLlaveDePropiedadEnRecursosExistentes`: **Deny explícito** de
  `CreateTags` y `DeleteTags` cuando la clave `primate:managed_by` está en el
  pedido (comparación sin mayúsculas: `Primate:Managed_By` también cuenta,
  porque IAM compara así las claves de `aws:ResourceTag`) y no es una creación.
- `NuncaBorrarEtiquetas`: Deny de todo `DeleteTags`. Un `DeleteTags` sin
  claves borra **todas**, y ahí `aws:TagKeys` no viene en el pedido: la
  condición de arriba sola no lo frenaría.

Además, el cerrojo de frontera de PCM bloquea en código cualquier
`create_tags`/`delete_tags` que toque la llave, sobre cualquier recurso.

**Costo asumido:** después de creada una máquina, PCM ya no puede
re-etiquetarla en producción. El re-tag de `primate:client_id` al cambiar
el cliente de un servidor y el botón «Re-etiquetar recursos» fallan con
AccessDenied. Ninguno de los dos corta nada (queda el error en la bitácora),
pero la atribución de costos de ese servidor queda con el cliente original
hasta que un admin la corrija a mano.

## Placeholders a reemplazar

| Placeholder | Valor |
|---|---|
| `<ACCOUNT_ID>` | ID de la cuenta productiva |
| `<ZONE_ID>` | Hosted Zone ID real del dominio (ej. `Z0…`) |
| `<NOMBRES_DNS_DE_PCM>` | Nombre(s) que crea PCM, normalizados en minúsculas, con comodín si hace falta. Para mañana: el dominio de Barraca Julia (ej. `barraca-julia.primate.cloud`). **Solo** esos nombres se pueden crear/actualizar/borrar. |
| `<BUCKET_SALIDA_DISCOVERY>` | Opcional: bucket para la salida completa del discovery (si no se usa, borrar ese statement). |

## Paso único (lo hace un admin, no PCM): crear el documento SSM

El documento es el **único** comando que PCM puede ejecutar en un servidor
importado. No acepta parámetros y no escribe nada. Se genera de los scripts
de solo lectura del repo (un test verifica que el JSON esté sincronizado):

```bash
aws ssm create-document \
  --name pcm-readonly-discovery \
  --document-type Command \
  --document-format JSON \
  --content file://primate_cloud_manager/data/ssm/pcm-readonly-discovery.json \
  --region <REGION>          # una vez por región con servidores
```

## Verificación con el IAM Policy Simulator

El simulador **no lee las etiquetas reales** de las instancias: la etiqueta
se pasa como clave de contexto. Para un servidor importado se simula
**sin** la clave (no la tiene) — es exactamente su situación real.

### 1. Lo importado no se toca (correr con `pcm-prod-ro` Y con `pcm-prod-prov`)

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names ec2:StopInstances ec2:StartInstances ec2:RebootInstances \
                 ec2:TerminateInstances ec2:ModifyInstanceAttribute \
                 ec2:CreateTags ec2:DeleteTags \
  --resource-arns arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<ID_IMPORTADA> \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
```

Esperado: **`explicitDeny`** con `pcm-prod-ro` (sus Deny explícitos) e
**`implicitDeny`** con `pcm-prod-prov` (solo permite recursos con la
etiqueta de PCM).

```bash
# Shell arbitrario sobre el importado: denegado
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names ssm:SendCommand \
  --resource-arns arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<ID_IMPORTADA> \
  --query "EvaluationResults[].[EvalResourceName,EvalDecision]" --output table
```

Esperado con `pcm-prod-prov`: **`implicitDeny`** para la instancia
importada (solo `SendCommand` sobre instancias con
`ssm:resourceTag/primate:managed_by=pcm`). Con `pcm-prod-ro` la instancia
figura permitida **pero ningún documento salvo el de lectura** lo está:
simular también el recurso `arn:aws:ssm:<REGION>::document/AWS-RunShellScript`
→ **`implicitDeny`**, así que el comando completo se rechaza.

```bash
# DNS del cliente importado: denegado
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names route53:ChangeResourceRecordSets \
  --resource-arns arn:aws:route53:::hostedzone/<ZONE_ID> \
  --context-entries \
    "ContextKeyName=route53:ChangeResourceRecordSetsNormalizedRecordNames,ContextKeyValues=erp.cliente-importado.com,ContextKeyType=stringList" \
    "ContextKeyName=route53:ChangeResourceRecordSetsActions,ContextKeyValues=DELETE,ContextKeyType=stringList" \
    "ContextKeyName=route53:ChangeResourceRecordSetsRecordTypes,ContextKeyValues=A,ContextKeyType=stringList" \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
```

Esperado: **`implicitDeny`**.

### 2. Lo permitido funciona

```bash
# El discovery de solo lectura sobre el importado: permitido
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names ssm:SendCommand \
  --resource-arns arn:aws:ssm:<REGION>:<ACCOUNT_ID>:document/pcm-readonly-discovery \
                  arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<ID_IMPORTADA> \
  --query "EvaluationResults[].[EvalResourceName,EvalDecision]" --output table

# Detener una máquina de PCM (con la etiqueta): permitido (solo con provision)
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names ec2:StopInstances \
  --resource-arns arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<ID_DE_PCM> \
  --context-entries "ContextKeyName=aws:ResourceTag/primate:managed_by,ContextKeyValues=pcm,ContextKeyType=string" \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
```

Esperado: **`allowed`**.

### 3. Nadie se apropia de lo importado poniéndole la llave

El simulador no recibe la lista real de `Tags` del pedido: `aws:TagKeys` se
pasa como clave de contexto, y es justo lo que mira el Deny.

```bash
# Ponerle primate:managed_by a una instancia existente: denegado
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/<pcm-prod-ro|pcm-prod-prov> \
  --action-names ec2:CreateTags ec2:DeleteTags \
  --resource-arns arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<ID_IMPORTADA> \
  --context-entries \
    "ContextKeyName=aws:TagKeys,ContextKeyValues=primate:managed_by,ContextKeyType=stringList" \
    "ContextKeyName=aws:RequestTag/primate:managed_by,ContextKeyValues=pcm,ContextKeyType=string" \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
```

Esperado con los dos usuarios: **`explicitDeny`** en las dos acciones
(`NadieTocaLaLlaveDePropiedadEnRecursosExistentes`). Repetir con
`ContextKeyValues=Primate:Managed_By`: también **`explicitDeny`**. Repetir
sobre `instance/<ID_DE_PCM>`, con
`aws:ResourceTag/primate:managed_by=pcm` en el contexto: **también**
`explicitDeny`. Ni lo propio se re-etiqueta con la llave.

```bash
# La misma etiqueta DENTRO de RunInstances: permitida (solo provision)
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::<ACCOUNT_ID>:user/pcm-prod-prov \
  --action-names ec2:CreateTags \
  --resource-arns arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/i-nueva \
  --context-entries \
    "ContextKeyName=ec2:CreateAction,ContextKeyValues=RunInstances,ContextKeyType=string" \
    "ContextKeyName=aws:TagKeys,ContextKeyValues=primate:managed_by,ContextKeyType=stringList" \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
```

Esperado: **`allowed`** con `pcm-prod-prov`; con `pcm-prod-ro`,
**`implicitDeny`** (no crea nada).

## Antes de importar: ¿algo productivo YA tiene la llave?

Solo lectura y con las claves de **`pcm-prod-ro`** (perfil local, ej.
`pcm-prod-ro`). Lo corre Daryl, no Claude. Usa solo `ec2:DescribeRegions`,
`ec2:DescribeTags` y `rds:DescribeDBInstances`, que esa policy ya permite.
Recorre todas las regiones y el `grep -i` también caza variantes de
mayúsculas:

```bash
export AWS_PROFILE=pcm-prod-ro
for r in $(aws ec2 describe-regions --query 'Regions[].RegionName' --output text); do
  aws ec2 describe-tags --region "$r" --output text \
      --query 'Tags[].[ResourceType,ResourceId,Key,Value]' \
    | grep -i 'primate:managed_by' | sed "s/^/$r  /"
  aws rds describe-db-instances --region "$r" --output text \
      --query "DBInstances[].[DBInstanceIdentifier,join(',',TagList[].Key)]" \
    | grep -i 'primate:managed_by' | sed "s/^/$r  rds  /"
done; echo "-- fin del listado --"
```

`describe-tags` cubre todos los tipos de recurso de EC2: instancias,
volúmenes, security groups, Elastic IPs, snapshots, AMIs, etc.
**Esperado: solo la línea `-- fin del listado --`.** Cualquier otra línea
(región, tipo, id, clave, valor) es un recurso productivo que hoy
`pcm-prod-prov` podría operar: se frena antes de importar.

### En la consola

IAM → **Policy simulator** → usuario `pcm-prod-ro` (y después `pcm-prod-prov`) → servicio *Amazon EC2* →
acción `TerminateInstances` → en *Simulation settings*, Resource ARN de la
instancia importada; dejar **vacía** la clave global
`aws:ResourceTag/primate:managed_by` → *Run simulation* → **denied**
(*explicitly denied* por `MaquinasNoCreadasPorPCMIntocables`).

## Lo que NUNCA hay que adjuntar a estos usuarios

`AdministratorAccess`, `AmazonEC2FullAccess`, `AmazonSSMFullAccess`,
`AmazonSSMManagedInstanceCore` (es para las instancias, no para personas) ni
nada con `ssm:SendCommand` amplio: un `Allow` de SendCommand con cualquier
documento sobre `instance/*` habilitaría un shell sobre los importados (los
Deny explícitos cubren EC2/RDS/IAM, pero un documento SSM no se puede
acotar por nombre en un Deny). La segunda línea de defensa es PCM: el
cerrojo de frontera bloquea cualquier `SendCommand` a un importado que no sea
`pcm-readonly-discovery` antes de salir a AWS.
