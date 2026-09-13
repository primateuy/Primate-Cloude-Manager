# Guía: preparar AWS para la prueba real de aprovisionamiento (Fase 4)

Objetivo: montar un **Odoo 19 Community** real en una EC2, accesible **por IP (HTTP)**,
con **PostgreSQL local** (sin RDS ni DNS al principio) usando el botón **Aprovisionar**
del entorno. Esta guía cubre TODO lo que tenés que crear vos en AWS. El módulo no puede
crear la cuenta por vos (AWS pide tarjeta + verificación).

> 💸 **Costo**: una `t3.small` cuesta ~US$0,02/h. Si la **terminás** al terminar la
> prueba (botón Terminar de la instancia), el gasto es de centavos. El Free Tier
> (`t3.micro`, 1 GB) NO alcanza para Odoo: usá `t3.small` (2 GB) o `t3.medium`.

---

## Paso 1 — Crear la cuenta AWS

1. Entrá a https://aws.amazon.com/ → **Crear una cuenta de AWS**.
2. Email, nombre de cuenta, tarjeta de crédito, verificación por SMS.
3. Elegí el plan **Basic (gratis)**.
4. Iniciá sesión en la **Consola** como usuario root **solo para crear el IAM** (regla del
   módulo: nunca usar root para operar).

## Paso 2 — Usuario IAM con permisos mínimos

> El módulo guarda **Access Key + Secret** de un usuario IAM, nunca root.

1. Consola → **IAM** → **Users** → **Create user** (ej.: `pcm-operator`).
2. **Sin** acceso a consola (solo claves programáticas).
3. **Attach policies → Create inline policy** y pegá este JSON (permite lo de la prueba:
   EC2 + SSM + pasar el rol SSM a la instancia):

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
    }
  ]
}
```

4. Terminá y en **Security credentials → Create access key** (tipo *Application running
   outside AWS*). **Guardá el Access Key ID y el Secret** → van al módulo.

## Paso 3 — Rol + Instance profile para SSM (CRÍTICO)

El aprovisionamiento instala Odoo **vía SSM**, no SSH. La EC2 necesita un rol con el agente SSM.

1. IAM → **Roles** → **Create role** → *AWS service* → **EC2**.
2. Adjuntá la policy administrada **`AmazonSSMManagedInstanceCore`**.
3. Nombrá el rol **`pcm-ssm-role`** (coincide con el `iam:PassRole` de arriba).
   - Al crear el rol, AWS crea un **instance profile** con el mismo nombre.
4. En el wizard de aprovisionamiento, campo **Instance profile** = `pcm-ssm-role`.

## Paso 4 — Security Group (para abrir Odoo en el navegador)

1. Consola → **EC2** → **Security Groups** → **Create**.
2. Inbound rules:
   - **HTTP** (80) desde `0.0.0.0/0`
   - **HTTPS** (443) desde `0.0.0.0/0` (opcional, para más adelante)
   - **SSH** (22) desde tu IP (opcional, respaldo)
3. Anotá el **ID** (`sg-...`) → va al wizard, campo **Grupos de seguridad**.
   (SSM funciona con tráfico **saliente**, que el SG default ya permite.)

## Paso 5 — (Opcional) Key pair SSH de respaldo

EC2 → **Key Pairs** → **Create** → guardá el `.pem`. Su nombre va al campo **Par de claves**.
No es necesario para SSM; solo para entrar por SSH si algo falla.

## Paso 6 — AMI de Ubuntu 24.04 (ahora automático)

**Ya no hace falta cargar el `ami-...` a mano.** El campo AMI del wizard es **opcional**:

- Si lo dejás **vacío**, el módulo resuelve solo el **último Ubuntu 24.04 de la región** de la
  cuenta al crear la instancia (probando primero el parámetro público de SSM y, si no tiene
  permiso, cayendo a `DescribeImages`). Así se evita el error de *región cruzada*.
- O tocá el botón **"Resolver AMI"** en el wizard para verlo/autocompletarlo antes de aprovisionar.
- Si preferís, podés cargar uno manual (se limpia solo si pegás corchetes/comillas por error).

> El parámetro público de SSM (Opción A, 1 sola llamada) necesita el permiso `ssm:GetParameter`
> del bloque **`SsmGetPublicAmi`** del Paso 2. **No es obligatorio**: sin él, el fallback por
> `DescribeImages` (que ya cubre `ec2:Describe*`) resuelve igual.

---

## Resumen de lo que cargás en el módulo

| Dónde | Valor |
|---|---|
| `Cuenta AWS` → Access Key ID / Secret | del Paso 2 |
| `Cuenta AWS` → Región por defecto | `us-east-1` (recomendado) |
| Wizard Aprovisionar → AMI | **dejar vacío** (se resuelve solo) o botón "Resolver AMI" |
| Wizard Aprovisionar → Tipo de instancia | `t3.small` |
| Wizard Aprovisionar → Instance profile | `pcm-ssm-role` (Paso 3) |
| Wizard Aprovisionar → Grupos de seguridad | `sg-...` (Paso 4) |
| Wizard Aprovisionar → Base de datos | **PostgreSQL local** |
| Wizard Aprovisionar → Crear DNS | **No** (acceso por IP) |

Cuando tengas el Access Key + Secret, avisame y hacemos:
1. Cargar la cuenta en el módulo + **Validar conexión** (debe dar *Conectada*).
2. Crear el **Proyecto** y el **Entorno** (Odoo 19 / Community).
3. Botón **Aprovisionar** con los valores de arriba → el flujo monta el server.
4. Abrir Odoo en `http://<IP pública>` y, al terminar, **Terminar** la instancia.

> ⚠️ Antes de disparar el aprovisionamiento real te lo confirmo: crea recursos facturables.
