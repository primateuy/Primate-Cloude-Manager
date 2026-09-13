# Runbook — Alta de una cuenta AWS de cliente en Primate Cloud Manager

> Documento único de puesta en producción. Es el checklist que sigue un admin para dar de
> alta la cuenta AWS de un cliente nuevo en PCM, con **todo** lo que la prueba real de la
> Fase 8 dejó confirmado (IAM completo user+rol, buckets S3, verificación). Git-ignored.
>
> Reemplaza como fuente de verdad de IAM a las secciones sueltas de `PRUEBA_AWS_REAL.md`
> (que queda como narrativa de la primera prueba de EC2). Todo el JSON de acá fue
> **verificado contra AWS real** (cuenta de pruebas `603011031378`, 2026-07-02).

Convención: reemplazá `<ACCOUNT_ID>` por el ID de 12 dígitos de la cuenta AWS del cliente,
y `<REGION>` por la región de trabajo (la prueba usó `us-east-2`).

---

## Principios (por qué el setup es así)

- **Nunca root.** PCM guarda Access Key + Secret de un usuario IAM (`pcm-operator`), cifradas.
  Root solo se usa una vez en consola para crear el IAM.
- **Permisos mínimos.** Dos identidades con roles distintos:
  - **User `pcm-operator`** — lo que hace PCM *desde Odoo* (API AWS): describe/crea EC2,
    manda comandos SSM, lee/lista/gestiona objetos y lifecycle en S3.
  - **Rol `pcm-ssm-role`** (instance profile de las EC2) — lo que hace *la instancia*: recibe
    comandos SSM y **sube/baja los dumps a S3** (el backup corre en la EC2, no en Odoo).
- **PCM NO crea buckets.** Decisión fija (prueba real Fase 8). El bucket de una política de
  respaldo debe existir de antemano, **creado por un admin**. Si falta, PCM falla con un
  error accionable (`ensure_bucket` → `head_bucket`), no lo crea. Esto evita otorgar
  `s3:CreateBucket` a ninguna identidad de PCM.
- **Etiquetado obligatorio.** Todo recurso que crea PCM lleva `primate:client`,
  `primate:environment`, `primate:managed_by=pcm` (sostiene la atribución de costos, Fase 9).

---

## Checklist de alta

### 1. Preparar la cuenta
- [ ] Tener el **Account ID** (12 dígitos) de la cuenta AWS del cliente.
- [ ] Elegir la **región** de trabajo (`<REGION>`). Todo (SG, subred, AMI, buckets) es por
      región; no mezclar.
- [ ] Iniciar sesión como **root solo para crear el IAM** (después no se usa más).

### 2. Rol `pcm-ssm-role` (instance profile) — crear PRIMERO
El user va a referenciarlo con `iam:PassRole`, así que conviene crearlo antes.

- [ ] IAM → **Roles** → **Create role** → *AWS service* → **EC2**.
- [ ] Adjuntar la policy administrada **`AmazonSSMManagedInstanceCore`** (habilita el agente SSM).
- [ ] Nombrar el rol **exactamente `pcm-ssm-role`** (AWS crea un instance profile homónimo).
- [ ] Agregar esta **inline policy** (S3 de la Fase 8: subir/bajar dumps y filestore):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PcmS3BackupObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::pcm-*/*"
    }
  ]
}
```

> `PutObject` = subir dump + filestore (streaming desde la EC2). `GetObject` = el marcador
> `head-object` que mide el tamaño del backup y bajar el dump durante un restore.

### 3. Usuario `pcm-operator`
- [ ] IAM → **Users** → **Create user** → `pcm-operator` (sin acceso a consola).
- [ ] **Attach policies → Create inline policy** y pegar este JSON (EC2 + SSM + PassRole + S3
      de Fase 8, todo lo que PCM ejecuta desde Odoo):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Ec2Provision",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*", "ec2:RunInstances", "ec2:CreateTags",
        "ec2:StartInstances", "ec2:StopInstances",
        "ec2:RebootInstances", "ec2:TerminateInstances"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SsmRun",
      "Effect": "Allow",
      "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": "*"
    },
    {
      "Sid": "SsmGetPublicAmi",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:*::parameter/aws/service/canonical/*"
    },
    {
      "Sid": "Sts",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    },
    {
      "Sid": "PassSsmRole",
      "Effect": "Allow",
      "Action": ["iam:PassRole"],
      "Resource": "arn:aws:iam::*:role/pcm-ssm-role"
    },
    {
      "Sid": "PcmSecurityGroupAutocreate",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PcmReadInstanceProfile",
      "Effect": "Allow",
      "Action": ["iam:GetInstanceProfile"],
      "Resource": "arn:aws:iam::*:instance-profile/pcm-ssm-role"
    },
    {
      "Sid": "PcmS3BackupsBucket",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration"
      ],
      "Resource": "arn:aws:s3:::pcm-*"
    },
    {
      "Sid": "PcmS3BackupsObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::pcm-*/*"
    },
    {
      "Sid": "PcmRoute53Zones",
      "Effect": "Allow",
      "Action": ["route53:ListHostedZones"],
      "Resource": "*"
    },
    {
      "Sid": "PcmRoute53Records",
      "Effect": "Allow",
      "Action": [
        "route53:ChangeResourceRecordSets",
        "route53:ListResourceRecordSets"
      ],
      "Resource": "arn:aws:route53:::hostedzone/*"
    },
    {
      "Sid": "PcmRoute53Change",
      "Effect": "Allow",
      "Action": ["route53:GetChange"],
      "Resource": "arn:aws:route53:::change/*"
    },
    {
      "Sid": "PcmCostExplorer",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PcmCloudWatchMetrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    }
  ]
}
```

> **Cost Explorer y CloudWatch (Fase 9)** — verificado contra el *Service Authorization
> Reference*. Ninguno soporta resource-level: **obligan `Resource: "*"`** (Cost Explorer no
> tiene tipos de recurso; las métricas de CloudWatch tampoco para la lectura clásica). Los
> logs de la Fase 9 van por **SSM** (ya permitido), así que `logs:*` NO hace falta en v1. El
> rol `pcm-ssm-role` no necesita nada nuevo (las consultas corren desde Odoo).

> **Por qué el S3 va partido en dos statements:** `ListBucket` y las de *lifecycle* operan
> sobre el **bucket** (`arn:aws:s3:::pcm-*`); `GetObject`/`DeleteObject` operan sobre los
> **objetos** (`arn:aws:s3:::pcm-*/*`). `GetLifecycleConfiguration` es necesario porque PCM
> **lee** la config de lifecycle antes de escribirla (mergea su regla sin pisar reglas
> ajenas). `DeleteObject` es para la limpieza (`delete_object`).
>
> **Route 53 (Fase 8.5, CRUD de DNS)** — verificado contra el *Service Authorization
> Reference* de AWS. Route 53 es **global**: los ARN no llevan región ni cuenta (`route53:::`)
> y el ID de zona va **sin** el prefijo `/hostedzone/`. Resource-level:
> `ChangeResourceRecordSets`/`ListResourceRecordSets` se restringen por zona
> (`hostedzone/*`, o zonas concretas `hostedzone/Z0123...` para menor superficie);
> `ListHostedZones` **obliga** `Resource: "*"`; `GetChange` (estado de propagación
> INSYNC/PENDING) va por `change/*`. El rol `pcm-ssm-role` NO necesita nada de Route 53
> (el CRUD corre desde Odoo, no desde la EC2).

- [ ] **Security credentials → Create access key** (*Application running outside AWS*).
      Guardar **Access Key ID + Secret** → van al módulo (paso 6).

### 4. Buckets S3 (los crea el admin — PCM no)
Convención de nombres (los nombres de bucket son **globales**; el sufijo de cuenta evita
colisiones y encaja en el scope `pcm-*` de las políticas):

| Uso | Nombre | Región |
|---|---|---|
| Respaldos gestionados | `pcm-backups-<ACCOUNT_ID>` | `<REGION>` |
| Transferencia de staging (opcional) | `pcm-staging-<ACCOUNT_ID>` | `<REGION>` |

- [ ] Consola S3 → **Create bucket** → `pcm-backups-<ACCOUNT_ID>` en `<REGION>` (defaults).
- [ ] (Opcional) `pcm-staging-<ACCOUNT_ID>` si se usará un bucket separado para staging.
- [ ] **No** crear una regla de lifecycle a mano: PCM la administra por prefijo según la
      retención de cada política de respaldo.

> ⚠️ El nombre **debe empezar con `pcm-`** para caer en el scope de ambas políticas. Un
> nombre fuera de `pcm-*` (p. ej. `mis-backups`) da `AccessDenied` al subir aunque el bucket
> exista — es lo que pasó en la prueba real con `pcm-fase8-test-*` antes de renombrar.

### 5. Verificar los permisos con el **Policy Simulator** (antes de usar PCM)
Regla de oro (aprendida en la prueba real, donde iteramos sobre errores crudos): **confirmar
en el simulador, no en PCM.**

- [ ] IAM → **Policy Simulator** → seleccionar **`pcm-operator`** → simular:
      `s3:ListBucket` sobre `arn:aws:s3:::pcm-backups-<ACCOUNT_ID>` → **allowed**;
      `s3:GetObject` / `s3:DeleteObject` sobre `arn:aws:s3:::pcm-backups-<ACCOUNT_ID>/x` → **allowed**;
      `s3:PutLifecycleConfiguration` sobre el bucket → **allowed**;
      `ec2:RunInstances` y `ssm:SendCommand` → **allowed**; `iam:PassRole` sobre
      `pcm-ssm-role` → **allowed**;
      `route53:ChangeResourceRecordSets` sobre `arn:aws:route53:::hostedzone/*` → **allowed**;
      `route53:ListHostedZones` → **allowed** (Resource `*`);
      `ce:GetCostAndUsage` → **allowed** (Resource `*`);
      `cloudwatch:GetMetricData` → **allowed** (Resource `*`);
      `ec2:CreateSecurityGroup` y `ec2:AuthorizeSecurityGroupIngress` → **allowed** (Resource `*`);
      `iam:GetInstanceProfile` sobre `arn:aws:iam::*:instance-profile/pcm-ssm-role` → **allowed**.
- [ ] Simular **`pcm-ssm-role`**: `s3:PutObject` y `s3:GetObject` sobre
      `arn:aws:s3:::pcm-backups-<ACCOUNT_ID>/x` → **allowed**. (No necesita Route 53 ni CE/CW.)

> Si algo da *denied*, revisar: ¿la versión editada de la policy quedó como **default**?
> ¿el statement está en la **identidad correcta** (user vs rol)? ¿el `Resource` incluye el
> ARN del **bucket** para `ListBucket` (no solo `/*`)?

### 6. Red (SG + subred) — por región
- [ ] Un **Security Group** en `<REGION>` que permita entrada 80/443 (y 22 si se quiere SSH
      de emergencia). SSM funciona con tráfico **saliente**, que el SG default ya cubre.
- [ ] Una **subred pública** de esa VPC con **auto-asignación de IP pública** (para IP
      pública + registro SSM). Anotar el `subnet-id` y el `sg-id` (van al wizard).

### 7. Dar de alta la cuenta en PCM
- [ ] En Odoo: **Cloud Manager → Configuración → Cuentas AWS → Nueva**.
- [ ] Método `access_key`; pegar Access Key ID + Secret (se guardan cifrados, visibles solo
      para `group_cloud_admin`). Región por defecto = `<REGION>`.
- [ ] Botón **Validar conexión** → debe dar OK (usa `sts:GetCallerIdentity`).
- [ ] Crear un **Proyecto** (con su **Cliente/partner**) y un **Entorno**; en el wizard de
      aprovisionar cargar el `instance profile = pcm-ssm-role`, el `sg-id` y el `subnet-id`
      del paso 6.

### 8. Activar los *cost allocation tags* (Billing) — ⚠️ HACERLO YA, no después
> **Paso del alta, no posterior.** Es lo que habilita la atribución de costos de la Fase 9.
> PCM **no** activa estos tags (los activa un admin en Billing, mismo criterio que "PCM no crea
> buckets/zonas").

- [ ] Consola AWS → **Billing → Cost allocation tags** → activar **`primate:environment_id`**
      y **`primate:client_id`** (User-Defined Cost Allocation Tags). Aparecen en la lista
      recién después de que exista al menos un recurso etiquetado (por eso este paso va tras
      crear el primer entorno; si no aparecen aún, aprovisioná uno y volvé).
- [ ] **La atribución de la Fase 9 usa `primate:environment_id` y `primate:client_id`** (los
      identificadores ESTABLES), **NO** `primate:environment` / `primate:client` por nombre
      (esos quedan solo para lectura humana en la consola: mutables y no únicos).
- [ ] ⚠️ **La activación NO es retroactiva.** Activar un cost allocation tag **no** discrimina
      los costos de meses PASADOS. Aunque después re-etiquetes los recursos existentes (botón
      "Re-etiquetar recursos" de la cuenta), los costos previos a la activación quedan en
      **"Sin atribuir"** para ese período; el desglose fino arranca **desde la activación hacia
      adelante**. Por eso: **activá los tags cuanto antes en cada cuenta nueva** — cada mes que
      pasa sin activarlos es un mes que **nunca** se va a poder desglosar por entorno/cliente.
- [ ] (Para cuentas con recursos preexistentes) Cuenta AWS del cliente en Odoo → **Previsualizar
      re-etiquetado** y luego **Re-etiquetar recursos**: aplica los tags estables a lo ya creado
      (idempotente). No afecta la retroactividad de costos, solo asegura que los recursos vivos
      lleven la clave para los meses futuros.

### 9. Política de respaldo (si el cliente tendrá backups gestionados)
- [ ] **Configuración → Políticas de respaldo**: usar una de las seed (Básica/Estándar/
      Crítica) o crear una; marcar **Gestionada por PCM** y poner **Bucket S3** =
      `pcm-backups-<ACCOUNT_ID>`, prefijo `pcm-backups`, hora de ejecución (UTC).
- [ ] Asignar la política al entorno. El validador diario y el ejecutor de backups quedan
      activos por cron; "Respaldar ahora" y "Verificar ahora" están en el hub del entorno.

---

## Resumen de nombres fijos

| Recurso | Nombre |
|---|---|
| Usuario IAM | `pcm-operator` |
| Rol / instance profile | `pcm-ssm-role` |
| Managed policy del rol | `AmazonSSMManagedInstanceCore` |
| Inline policy del rol (S3) | `PcmS3BackupObjects` |
| Statements S3 del user | `PcmS3BackupsBucket`, `PcmS3BackupsObjects` |
| Statements Route 53 del user | `PcmRoute53Zones`, `PcmRoute53Records`, `PcmRoute53Change` |
| Statements Costos/Métricas del user | `PcmCostExplorer`, `PcmCloudWatchMetrics` |
| Statements Auto-discovery del user | `PcmSecurityGroupAutocreate`, `PcmReadInstanceProfile` |
| Security group auto-gestionado | `pcm-managed` (uno por VPC, ingress 80/443) |
| Bucket de respaldos | `pcm-backups-<ACCOUNT_ID>` |
| Bucket de staging (opcional) | `pcm-staging-<ACCOUNT_ID>` |
| Tags de lectura humana | `primate:client`, `primate:environment`, `primate:managed_by=pcm` |
| Tags de atribución (ESTABLES, activar en Billing) | `primate:environment_id`, `primate:client_id` |

> **DNS (Fase 8.5):** PCM gestiona **registros** dentro de hosted zones **existentes** (las
> crea un admin, mismo criterio que los buckets: PCM no crea/borra zonas). El módulo importa
> los registros al sincronizar la cuenta y permite crear/editar/borrar registros A/CNAME/TXT/MX
> desde el hub. Los registros **alias** de Route 53 se ven pero no se editan en v1.

## Verificación end-to-end (opcional, replica la prueba real)
Sobre un entorno demo del cliente, en orden: **Respaldar ahora** (verificar dump+filestore en
S3 y validador → "Cumple") → **Crear staging desde la instancia** (HTTP 200, filestore íntegro)
→ **Restaurar** un backup sobre el staging (pre-backup en S3 antes del drop) → **terminar** las
EC2 de prueba y **vaciar** el bucket. Detalle y evidencia de referencia en `PRUEBA_FASE8_REAL.md`.

## Companion `pcm_impersonate` — segundo software desplegable (Login as / Bloque B5)

El "Login as" del panel instala un **addon companion** (`pcm_impersonate`) DENTRO del Odoo de cada
instancia gestionada. Es una **segunda pieza de software con su propia superficie de seguridad**
(un endpoint que crea sesiones), así que su ciclo de vida es parte del ciclo de vida de PCM:

- **Despliegue/actualización**: PCM lo empaqueta desde `companion/pcm_impersonate/` y lo envía por
  SSM (extraer en custom-addons → `-i`/`-u` por BD → escribir la clave pública → nginx allow-list).
  **Cuando cambie el código del companion (fix/mejora), hay que RE-desplegarlo a las instancias que
  lo tengan instalado** — no se actualiza solo. Tratarlo como un release: versión en su
  `__manifest__.py`, y re-deploy tras cada cambio.
- **Deshabilitado por default** (`pcm.impersonate.enabled=False`) + **allow-list de IP de egreso de
  PCM en nginx** + **kill-switch** (deshabilitar corta también las sesiones vivas). El cliente lo ve
  instalado y lo puede desactivar desde su Odoo (menú "Soporte PCM").
- **La clave privada Ed25519 de PCM es ahora EL secreto crítico**: quien la tenga puede forjar
  tokens y abrir sesión como cualquier usuario en cualquier instancia que confíe en su pública.
  Vive cifrada en PCM (`pcm.impersonate.privkey.<account_id>`), nunca sale. **Si se sospecha
  compromiso de la privada: ROTAR el par (regenerar) y RE-DESPLEGAR la clave PÚBLICA nueva a TODAS
  las instancias** — hasta que la pública se rote en cada instancia, los tokens firmados con la
  privada comprometida siguen siendo válidos ahí.
