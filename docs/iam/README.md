# IAM de la cuenta productiva para PCM

Dos policies, en **dos usuarios IAM distintos** y **dos cuentas en PCM**:

| Usuario IAM | Policy | Cuenta en PCM | Cuándo |
|---|---|---|---|
| `pcm-prod-ro` | `pcm-prod-readonly.json` | «Prod — solo lectura» (normal) | Desde el enlace: inventario, importación y gemelo. |
| `pcm-prod-prov` | `pcm-prod-provision.json` | «Prod — aprovisionamiento», con **Solo aprovisionamiento** marcado | Recién para montar Barraca Julia. |

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
volumen, security group y Elastic IP). Un servidor importado **no** la tiene,
y la policy de readonly prohíbe agregarla (`ec2:CreateTags` sobre recursos
sin la etiqueta solo se permite en la creación). Por eso lo importado queda
fuera del alcance de cualquier escritura aunque se adjunte `provision`.

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
