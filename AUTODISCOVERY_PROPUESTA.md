# Auto-descubrimiento de red por región — Propuesta de diseño

> **DISEÑO. Cero código. FRENO al final.** Que un usuario final que NO sabe de AWS cree una
> instancia con solo **región + versión de Odoo + edición**. PCM descubre el resto de la cuenta,
> lo **valida** (comportamiento real, no solo existencia), y **auto-crea únicamente el security
> group** si falta. Lo estructural (VPC/subnet/rol) NO se auto-crea en v1: si falta, error
> accionable apuntando al onboarding. Principio: **descubrir y validar, no adivinar en silencio.**

---

## 0. Qué ya existe y qué se apoya en ello

- `services/aws_ec2.py`: `resolve_ubuntu_ami(region)` (SSM param + fallback `describe_images`),
  `create_instance(... security_group_ids, subnet_id, instance_profile ...)`, `create_tags`,
  lifecycle. El provisioning (`environment._provision_ec2`) HOY toma SG/subnet/profile de los
  `params` del wizard (guardados cifrados). **Contradicción a reconciliar (§10-1):** esos campos
  son obligatorios en el wizard; con auto-discovery pasan a **opcionales** (se completan solos).
- `services/aws_base.py`: `build_resource_tags(...)` y las constantes de tags
  (`primate:managed_by=pcm`, etc.) — el SG auto-creado se taguea con este helper.
- IAM del user `pcm-operator` (runbook): `ec2:Describe*` (cubre Vpcs/Subnets/SecurityGroups/
  InternetGateways/RouteTables), `ec2:RunInstances/CreateTags`, `iam:PassRole` sobre
  `pcm-ssm-role`. **NO** tiene `iam:GetInstanceProfile` (la prueba real de Fase 8 lo confirma:
  da AccessDenied, pero `PassRole` funciona). Ver §2 (nuevo IAM).

---

## 1. AMI — ya resuelto (confirmado)

`resolve_ubuntu_ami(region)` sigue sirviendo: descubre el AMI de Ubuntu por región vía SSM public
parameter (`/aws/service/canonical/ubuntu/.../ami-id`), con fallback a `describe_images` (owner
Canonical). No requiere nada nuevo. El auto-discovery lo **reusa** como el primer recurso a
resolver. Validación = el AMI resuelve a un id no vacío para la región; si no → error accionable
"no se pudo resolver el AMI de Ubuntu para <región>" (raro; suele ser permiso SSM).

---

## 2. Qué descubrir y validar (por región, en la cuenta del entorno)

Regla transversal (**"estado terminal no basta"**): validar el **comportamiento real** del
recurso, no solo que exista.

| # | Recurso | Cómo se descubre (llamada AWS) | Qué se valida (comportamiento, no solo existencia) | Falta / no cumple |
|---|---|---|---|---|
| 1 | **AMI** | `resolve_ubuntu_ami` (SSM param) | Resuelve a un id no vacío | Error accionable (permiso SSM) |
| 2 | **Rol / instance profile `pcm-ssm-role`** | `iam:GetInstanceProfile` (nuevo) **o** dry-run RunInstances (ver §2.1) | Existe **y** es pasable (PassRole ok) | **Error accionable, NO auto-crear** → "corré el onboarding IAM en esta cuenta" |
| 3 | **VPC** | `describe_vpcs` (default de la región, o tagueada `pcm-managed`) | Existe **y tiene salida a internet**: un Internet Gateway attached (`describe_internet_gateways` filtrando `attachment.vpc-id`) | **Error accionable, NO auto-crear** (v2) |
| 4 | **Subnet** | `describe_subnets` (de esa VPC) | Es **pública de verdad**: `MapPublicIpOnLaunch=True` **y** su route table (`describe_route_tables`) tiene ruta `0.0.0.0/0 → igw-…`. Existir no basta | **Error accionable, NO auto-crear** (v2) |
| 5 | **Security Group** | `describe_security_groups` filtrando por tag `primate:managed_by=pcm` (+ nombre convenido `pcm-managed`) en esa VPC | Reglas correctas (§3): ingress 80+443, egress abierto | **AUTO-CREAR** (§3, único que se crea en v1) |

### 2.1 El rol: por qué no se lee hoy y cómo validarlo
Hoy `pcm-operator` no tiene `iam:GetInstanceProfile`. Dos caminos:
- **(A, recomendado)** Agregar `iam:GetInstanceProfile` (Resource = el ARN del instance profile
  `pcm-ssm-role`, scopeado) al user → lectura limpia "existe sí/no". Es read-only y acotado.
- **(B, sin IAM nuevo)** Validar por **RunInstances DryRun** con el `IamInstanceProfile`: si el
  perfil no existe o PassRole falla, el dry-run devuelve el error específico; si el perfil está
  ok, devuelve `DryRunOperation`. No lee el rol pero prueba el **comportamiento** (que es lo que
  importa). Encaja con "estado terminal no basta".

Propongo **(A)** por claridad del mensaje de error, con **(B)** como validación adicional del
camino real (dry-run) antes de aprovisionar. Decisión del usuario en §10-2.

---

## 3. Security Group — el único que se auto-crea (v1)

### Reglas mínimas requeridas
- **Ingress**: `tcp/80` y `tcp/443` desde `0.0.0.0/0` (acceso web público a Odoo/nginx; 80 también
  lo necesita certbot ACME http-01). **Sin SSH (22)**: SSM no necesita puerto entrante — menos
  superficie.
- **Egress**: todo saliente (`-1`, `0.0.0.0/0`) — el agente SSM y el `install_odoo.sh` (apt/pip/
  git/certbot) necesitan salida. Es el egress default de un SG nuevo.

### Descubrir → validar → auto-crear (idempotente)
1. `describe_security_groups` en la VPC, filtro `tag:primate:managed_by=pcm` + `group-name=pcm-managed`.
2. **Existe y cumple** → reusar (no tocar).
3. **Existe pero le falta una regla** (p. ej. sin 443): **completar la regla faltante** — es
   seguro porque es un SG **de PCM** (tagueado). **Nunca** se tocan reglas de un SG que no sea
   `pcm-managed`.
4. **No existe** → **crear** (`ec2:CreateSecurityGroup` con nombre fijo `pcm-managed`, tagueado
   con `build_resource_tags`) + `AuthorizeSecurityGroupIngress` (80/443). El egress abierto viene
   por default. Idempotencia + concurrencia en §6.

> Un SG descubierto que existe pero **no** es `pcm-managed` (lo eligió un admin a mano) se **usa
> tal cual si el admin lo indicó explícitamente**, pero PCM no lo modifica ni lo valida a fondo
> (no es suyo). El auto-path solo gestiona el `pcm-managed`.

---

## 4. IAM nuevo (verificado contra la doc de AWS, para el runbook)

Descubrir usa `ec2:Describe*` (**ya concedido**) — cubre `DescribeVpcs`, `DescribeSubnets`,
`DescribeInternetGateways`, `DescribeSecurityGroups`, `DescribeRouteTables`. **Nada nuevo para
describir la red.**

Lo nuevo:

```json
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
}
```

- `ec2:CreateTags` (para taguear el SG) **ya está** en `Ec2Provision`.
- `CreateSecurityGroup`/`Authorize*` requieren `Resource: "*"` (o scoped a la VPC + el SG nuevo,
  pero el SG aún no tiene ARN al crearlo; `*` con la cuenta acotada es lo estándar).
- `iam:GetInstanceProfile` scopeado al ARN del profile (opción A de §2.1). Si se elige la opción
  B (dry-run), este statement **no** hace falta.
- **Verificar en Policy Simulator** antes de usar: `ec2:CreateSecurityGroup` → allowed,
  `ec2:AuthorizeSecurityGroupIngress` → allowed, `iam:GetInstanceProfile` sobre el ARN → allowed.

---

## 5. Estructura: servicio + modelo caché

### `services/aws_discovery.py` (adaptador PURO, read-only)
Sin ORM, sin lógica de negocio. Métodos que componen `describe_*`:
`discover_vpc(region)`, `discover_subnet(region, vpc_id)`, `vpc_has_igw(region, vpc_id)`,
`subnet_is_public(region, subnet_id)` (MapPublicIp + ruta a IGW), `discover_managed_sg(region, vpc_id)`,
`validate_sg_rules(sg)`. Devuelven datos normalizados + banderas de validación. La **creación**
del SG vive en `aws_ec2` (mutación): `ensure_security_group(region, vpc_id, name, tags)` +
`authorize_ingress(...)` — idempotente. Así el discovery queda testeable con moto y reutilizable.

### Modelo `primate.cloud.region.setup` (caché por cuenta+región)
Campos: `account_id`, `region`, `ami_id`, `vpc_id`, `subnet_id`, `security_group_id`,
`profile_ok`, `status` (Selection: `ok` / `missing_structural` / `error`), `detail` (texto
accionable), `last_discovered_at`, `discovered_by`. Constraint `UNIQUE(account_id, region)`.
- `action_discover()` — **síncrono** (read-only, corto, por la política): corre el discovery,
  valida, escribe el caché. Botón "Verificar región".
- `job_ensure_security_group()` — **queue_job** (mutación): crea/completa el SG idempotente,
  guarda `security_group_id`. Se encola solo si el discovery dice "SG falta pero VPC/subnet ok".
- **Refresh**: TTL (re-descubrir si `last_discovered_at` > N horas) + botón manual + invalidación
  al fallar un provisioning por recurso.

---

## 6. Idempotencia y concurrencia (dos creaciones simultáneas → un solo SG)

- **Idempotencia AWS nativa**: el SG tiene **nombre fijo** `pcm-managed` (único por VPC). Si dos
  jobs intentan crearlo, el segundo recibe `InvalidGroup.Duplicate` → se **captura**, se
  `describe_security_groups` y se **reusa** el existente. Nunca dos SG.
- **Serialización PCM**: el `job_ensure_security_group` corre en un **canal queue_job dedicado por
  (cuenta, región)** o toma un **advisory lock** (`pg_advisory_xact_lock(hash(account,region))`)
  antes de describir+crear, de modo que el segundo espera y encuentra el SG ya creado. Doble red:
  lock + captura de Duplicate.
- El `region.setup` es el punto de deduplicación: `security_group_id` se escribe una vez; las
  provisiones leen de ahí.

---

## 7. El wizard (usuario final vs admin)

### Usuario final (no ve AWS)
Tres campos: **región · versión de Odoo · edición**. Al elegir región, un paso **"Verificar
región"** corre `action_discover` (síncrono) y muestra un semáforo:
- **Todo verde** → botón **Crear** habilitado. El SG faltante se auto-crea al aprovisionar (o en
  el mismo "verificar", encolando `job_ensure_security_group`).
- **Rojo estructural** (rol/VPC/subnet) → **Crear deshabilitado** + mensaje accionable (§8). El
  usuario final ve algo legible ("esta región todavía no está preparada; avisá al administrador"),
  no el detalle AWS.

### Admin (ve y ajusta)
Mismo flujo + un **panel "Qué descubrió PCM"** (por `region.setup`): VPC/subnet/SG/profile con su
estado, **editable** (puede fijar un SG/subnet específico, override del auto). Ve el detalle AWS y
el link al paso del runbook si algo falta.

**Visibilidad por rol**: el semáforo detallado y el panel de override son solo para
`group_cloud_admin`; el usuario final ve el estado agregado (listo / no listo).

---

## 8. Errores accionables (lo que ve el admin, apuntando al runbook)

Nada de traceback crudo. Por recurso que falta y NO se auto-crea:

- **Rol/profile**: *"El instance profile `pcm-ssm-role` no existe o no es pasable en esta cuenta.
  Corré el paso IAM del onboarding (RUNBOOK §Checklist de alta → rol `pcm-ssm-role`) antes de
  aprovisionar."*
- **VPC sin internet**: *"La VPC default de `<región>` no tiene Internet Gateway (o no hay VPC
  usable). PCM no crea VPCs en v1: creá/attachá un IGW o preparate una VPC en esta región
  (RUNBOOK)."*
- **Subnet no pública**: *"No hay una subnet pública en `<región>` (con auto-assign public IP y
  ruta a Internet Gateway). Preparate una subnet pública (RUNBOOK); PCM no crea subnets en v1."*
- **SG (no es error, se auto-crea)**: informativo *"Se creó/completó el security group
  `pcm-managed` con ingress 80/443."*

---

## 9. Plan por bloques (con testeo)

| # | Bloque | Testeo |
|---|---|---|
| **B1** | `aws_discovery` (describe VPC/subnet/IGW/route/SG + validaciones puras) + `aws_ec2.ensure_security_group` | **moto** soporta describe/create de VPC/Subnet/IGW/RouteTable/SG (verificar en el harness) — tests puros de descubrimiento y validación |
| **B2** | Modelo `region.setup` + `action_discover` (síncrono) + estados/errores accionables | tests con `aws_discovery` mockeado + moto |
| **B3** | `job_ensure_security_group` (idempotente, lock + Duplicate) + completar reglas faltantes | moto: crear, re-crear (reusa), SG sin 443 (completa), concurrencia simulada |
| **B4** | Wizard: usuario final (región/versión/edición) + "verificar región" semáforo + panel admin override; SG/subnet opcionales en el flujo | smoke_ui + tests del wizard |
| **B5** | **Prueba real** en una **región virgen** de la cuenta real (t3.micro): descubre, crea el SG, aprovisiona con **cero config manual**, HTTP 200; terminar + barrer | prueba real controlada (barata) |

**IAM al runbook**: statements `PcmSecurityGroupAutocreate` (+ `PcmReadInstanceProfile` si opción
A) — con su chequeo en Policy Simulator.

---

## 10. Contradicciones / notas (marcadas, no tapadas)

1. **SG/subnet obligatorios en el wizard hoy → pasan a opcionales.** El auto-discovery los
   completa. El wizard `provision.wizard` y el `_prepare_params` deben tolerar SG/subnet vacíos
   (se resuelven desde `region.setup`). La **config guardada cifrada** ya no necesita SG/subnet
   para el path de usuario final (el admin puede seguir fijándolos).
2. **`iam:GetInstanceProfile` no está hoy** → opción A (agregarlo, read-only scopeado) vs opción B
   (dry-run sin IAM nuevo). Recomiendo A + dry-run de validación real.
3. **"VPC default"**: algunas cuentas viejas no tienen VPC default. El descubrimiento cae al
   criterio de tag `pcm-managed`; si no hay ninguna → error accionable (no adivinar cuál usar).
4. **v2 explícito**: auto-crear VPC+subnet+IGW+route (la pieza estructural cara) y auto-crear el
   rol quedan fuera de v1 por decisión tomada. El diseño deja el hueco (los errores accionables
   ya nombran "PCM no crea esto en v1").
5. **Regla "descubrir y validar, no adivinar"**: si hay **varias** subnets públicas o **varias**
   VPCs candidatas, NO elegir en silencio — preferir la default/tagueada; si hay ambigüedad real,
   pedirle al admin que tague la que quiere (`pcm-managed`). Documentar el criterio de desempate.

**FRENO. Espero tu revisión (en especial §2.1 opción A/B del rol, y §10-1 SG/subnet opcionales)
para arrancar B1.**
