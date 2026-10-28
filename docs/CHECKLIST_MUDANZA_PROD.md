# Runbook — PCM Producción y montaje de Barraca Julia UAT

Orden obligatorio: no se pasa al paso siguiente sin cerrar el anterior.
Todo lo importado es **solo lectura sin excepción** (UI, modelo, cerrojo de
frontera e IAM). Detalle de IAM y del simulador: [`docs/iam/README.md`](iam/README.md).

**Quién hace cada paso:**
- **YO** = Daryl, a mano (consola de AWS, CloudShell, tu Mac).
- **CC** = Claude Code: prepara, guía y verifica en tu Mac. Nunca toca la cuenta real.
- **PCM** = lo hace PCM cuando apretás el botón.

## ⛔ Regla del día (para Claude, sin excepción)

Claude **no ejecuta ningún comando directo contra servidores o cuentas
productivas**: ni `ssh`, ni `aws` cli, ni `rm`, ni nada equivalente. Todo pasa
por PCM: lectura e importación con la cuenta **«pcm-prod-ro»** (usuario IAM
`pcm-prod-ro`), y el montaje de Barraca Julia con la cuenta **«pcm-prod-prov»**
(usuario IAM `pcm-prod-prov`, «Solo aprovisionamiento»). Si algo requiere
salirse de eso, Claude **frena y le pregunta a Daryl** antes de hacer nada.

Los comandos directos de este runbook (CloudFormation, CloudShell, gemelo de
Cloudpepper por ssh, subida del dump) los corre **Daryl**; Claude solo los
prepara. Las credenciales productivas las carga **Daryl** en PCM: Claude no
las pide ni las lee.

**Video:** el recorrido completo se graba (credenciales y datos de clientes
blureados en el montaje).

---

## Orden del día (28-oct)

| # | Paso | Quién | Detalle |
|---|---|---|---|
| 0 | Push, levantar PCM Producción, A2F y suscripción | YO | [§0](#0--antes-de-empezar) |
| 1 | Stack `pcm-prerequisitos` en us-east-1 | YO | [§1](#1--stack-de-prerrequisitos-yo-consola--cloudshell) |
| 2 | `verificar.sh` en CloudShell → «TODO OK» | YO | [§2](#2--verificarsh-yo-cloudshell) |
| 3 | Regiones extra: `pcm-ssm-readonly.yaml` donde haya servidores a importar | YO | [§3](#3--regiones-extra-yo) |
| 4 | Keys: crear en IAM, cargar en PCM, «Validar conexión», perfil de aws cli | YO · PCM | [§4](#4--access-keys-yo--pcm) |
| 5 | Dump: `subir_dump.sh` | YO | [§5](#5--dump-de-barraca-julia-yo) |
| 6 | Montaje de bj-uat: aprovisionar (termina en sslip.io con HTTPS) → restore → checks | PCM · YO | [§6](#6--montaje-de-bj-uat-pcm-con-pcm-prod-prov) |
| 7 | Importación de solo lectura, **fuera de horario** (AWS por PCM + Cloudpepper por ssh) | PCM · YO | [§7](#7--importación-de-solo-lectura-fuera-de-horario) |
| 8 | Cierre: bitácora, grupo destructivo, aviso a INCO, limpieza | YO · CC | [§8](#8--cierre-y-aviso-a-inco) |

CC acompaña cada paso: guía, lee lo que le pegues (nunca credenciales) y
verifica en tu Mac. El paso 5 (subida, 8–30 min) puede correr mientras PCM
aprovisiona en el paso 6.1, porque el restore (6.3) recién lo usa al final.

## Preparado el 27-oct (CC ✅)

- [x] **`pcm_produccion` actualizada** a 19.0.1.6.0 (`-u primate_cloud_manager`
      hecho; respaldo previo de la base). Trae los arreglos del ensayo: dos
      cuentas con el mismo AWS ID, importación en cola, IP fija, dominio
      definitivo, secretos fuera de queue_job, Access Key ID enmascarada.
- [x] **Cuentas AWS sin credenciales**: **«pcm-prod-ro»** (lectura) y
      **«pcm-prod-prov»** («Solo aprovisionamiento: Sí»), las dos us-east-1,
      método Access Key, estado Borrador. Cada una con su **«Usuario IAM
      esperado»** (`pcm-prod-ro` / `pcm-prod-prov`): «Validar conexión» falla si
      las keys responden con otro usuario.
- [x] **Parámetros sin sufijo**: `pcm.ssm_instance_profile = pcm-ssm-role`,
      `pcm.readonly_discovery_document = pcm-readonly-discovery`,
      `pcm.ssl_use_le_staging = False` (Let's Encrypt **producción**). El
      bucket `pcm-dumps-<cuenta>` **no** es un parámetro: su ruta la imprime
      `subir_dump.sh` y se pega en el restore.
- [x] **Sin datos de demo**: ningún módulo con demo; solo el proyecto Barraca
      Julia, el entorno `bj-uat` y su instancia, en Borrador.
- [x] **Formulario de bj-uat precargado** (cuenta pcm-prod-prov): us-east-1 ·
      t3.xlarge · disco 100 GB (gp3) · Ubuntu 24.04 · PostgreSQL local, base
      `bj_uat` · IP fija marcada · **«Dominio sslip.io automático» marcado** ·
      **«Emitir HTTPS al terminar» marcado** · sin Route 53 · instancia
      `bj-uat`. El montaje termina solo en `https://bj-uat.<ip>.sslip.io`. Entorno tipo **Staging**, Odoo 19 Community, sin repos (sin
      LocalizacionUy), con el aviso de UAT en el chatter.
- [x] **Dump**: sha256 recalculado = `SHA256SUMS.txt`; checklist de
      neutralización **re-chequeado leyendo el `dump.sql` dentro del zip**:
      14/14 en verde (ver «Tamaños reales»).
- [x] **`subir_dump.sh`** probado con un aws cli simulado: sube a
      `incoming/barraca-julia/`, frena con otro perfil, con sha256 local
      distinto y con sha256 distinto en S3. Permisos de `pcm-prod-provision`
      revisados (PutObject en `incoming/`, GetObject para la verificación).
- [x] **`~/.pcm_produccion/cloudpepper_servidores.txt`** creado (600) con el
      formato; falta completar los servidores (§7).
- [x] Ensayo completo de los permisos en la sandbox con usuarios `-cfn`
      (27-oct, video `docs/demo/ensayo_permisos_sandbox_2026-10-27.mp4`):
      montaje, sslip.io + HTTPS, restore, importación + gemelo, terminar.
      Sin AccessDenied.

---

## Decisiones tomadas

- PCM Producción en una base **nueva** (`pcm_produccion`), separada de la sandbox.
- **Barraca Julia es un ambiente de PRUEBA / UAT, no de producción.** El
  cuadre contable **no está validado** (`05_cuadre` del 11-sep: 33
  diferencias en caja y bancos 2022–2024). Nombre **`bj-uat`** (entorno e
  instancia) y tipo **Staging** (≠ producción) en PCM, con el aviso en su
  chatter. El cliente sigue operando en su sistema actual.
- Base: **`bj_v19_neutralizada_pcm_2026-10-27.zip`**, generada neutralizada
  para PCM desde `bj_v19_neutralizada_2026-09-14.zip` (OK de Daryl, 27-oct).
- **Sin `create_dns`**: HTTPS vía **sslip.io** con IP fija. El dominio
  definitivo se suma después (ver «Pendiente de bj-uat»).
- **Sin LocalizacionUy**: la base no tiene ningún módulo instalado y para v19
  solo existe la rama `19.0_staging`. Queda como pendiente en la KB.
- Región **us-east-1** (Virginia): región de **pruebas**, bajo demanda.
  INCO tiene **EC2 Instance Savings Plans (de familia) en Ohio**: las instancias
  definitivas que migren desde Cloudpepper van en **Ohio (us-east-2) y en esa
  familia** (a confirmar por INCO). bj-uat NO entra en el savings plan.
- Servidor según INCO: **t3.xlarge, EBS gp3 100 GB, Ubuntu 24.04** (27-oct:
  INCO bajó el disco de 200 a 100 GB; entra, ver «Tamaños reales»). Si hiciera
  falta más: [`docs/AMPLIAR_DISCO.md`](AMPLIAR_DISCO.md) (PCM no lo hace todavía).
- La importación corre **fuera de horario**: el discovery lee disco, base y
  `pip freeze` de cada servidor productivo (solo lectura, pero con carga).

## Tamaños reales

| | |
|---|---|
| Artefacto a restaurar | `~/dumps/barracajulia/pcm_produccion/bj_v19_neutralizada_pcm_2026-10-27.zip` · **11.229.926.329 bytes (10,46 GiB)** · sha256 `60251a0c77745c3055675d67fbe3d0291a663ad0cd736d2662b0fd3055a5a429` |
| Checklist de neutralización | verde al generarlo (`.checklist.json`: 115 módulos) y **re-chequeado el 27-oct** desde el `dump.sql` del zip: SMTP activos 0, SMTP con credenciales 0, fetchmail activos 0, crons activos 0, correo en cola 0, websites con dominio 0, `web.base.url` = placeholder, `database.is_neutralized` = true, Sibra sin credenciales, sin Google Places key, sin access_token de portal, sin signup_token, 1 usuario con contraseña (administrator), 0 API keys |
| Descomprimido | 20,81 GiB = `dump.sql` 11,94 GiB + filestore 8,87 GiB (261.891 archivos) |
| Base cargada | **19,19 GiB** (medida el 27-oct; la `bj_v19` local mide 43 GB por bloat) |
| Pico del restore (actual) | ~41 GiB: lo descomprimido + la base mientras carga (el zip se borra al extraerse y el `dump.sql` al cargarse) |
| + sistema, Odoo y logs | ~7 GiB → **~48 GiB de ~96 utilizables en 100 GB** |
| Chequeo de PCM | pide 23 GiB para descomprimir y ~45 GiB para el pico (+10 %): **pasa con 100 GB**; con 30 GB corta antes de tocar nada |

## Piezas preparadas (en el repo)

| Pieza | Para qué |
|---|---|
| `infra/cloudformation/pcm-prerequisitos.yaml` | Todo lo de IAM/SSM/S3 de la cuenta en un stack, sin reemplazar nada a mano |
| `infra/cloudformation/pcm-ssm-readonly.yaml` | Solo el documento SSM, para otras regiones con servidores a importar |
| `infra/cloudformation/generar.py` | Genera las dos plantillas desde `docs/iam/*.json` y `data/ssm/` (`--check` avisa si quedaron viejas) |
| `infra/cloudshell/verificar.sh` | Verificación de solo lectura para pegar en CloudShell |
| `infra/subir_dump.sh` | Subida del dump con el perfil `pcm-prod-provision`, con progreso y verificación de tamaño y sha256 |
| `infra/gemelo_cloudpepper.sh` | Gemelo de todos los servidores Cloudpepper por ssh (solo lectura), un `.json` por servidor |
| `infra/cloudpepper_servidores.example` | Formato de la lista de servidores Cloudpepper |

---

## 0 · Antes de empezar

1. [ ] YO · Push: `git push origin 19.0`.
2. [ ] YO · Levantar PCM Producción: `~/Odoo/clients/19.0/pcm_produccion/levantar.sh`
   → http://127.0.0.1:19190, usuario `admin`. (El `-u` ya lo hizo CC el 27-oct;
   si después de hoy entra otro commit de PCM, se repite
   `levantar.sh -u primate_cloud_manager --stop-after-init` antes de levantar.)
3. [ ] YO · Preferencias → **Seguridad → «Habilitar la A2F»** (si no está).
4. [ ] YO · Enterprise: registrar el código de suscripción (si no, vence a los 30 días).

> No usar `pcm_demo` (sandbox) para nada productivo: tiene una cuenta de
> prueba vieja llamada «Primate Producción» con un ID ficticio (123456789012).

> ⚠️ **`limit_time_real` en el `.conf` de cualquier PCM que corra jobs
> largos** (2026-10-28). queue_job corre cada job DENTRO de un pedido HTTP:
> con el default de Odoo (120 s) el servidor de PCM se reinicia a mitad de un
> job largo y queue_job lo relanza («dead job»), mientras el script de SSM
> sigue corriendo en el servidor. En el montaje de bj-uat eso dejó cuatro
> restores simultáneos. `pcm_produccion.conf` lleva **`limit_time_real =
> 14400`** (0 no sirve: con `workers > 0` es el watchdog y mata cada worker
> al instante). La sandbox (`PrimateCloudeManager.conf`) ya tenía 120000.
> Además, desde ese día los jobs que borran o pisan datos **no se relanzan
> solos** y el restore toma un **candado por base** en el servidor (ver §6.3).

## 1 · Stack de prerrequisitos (YO, consola + CloudShell)

0. [x] YO · INCO aprobó `pcm_permisos_2026-10-27.zip`.
1. [ ] YO · **Antes de desplegar**, en CloudShell (us-east-1). Subí
   `pcm-prerequisitos.yaml` desde «Actions → Upload file» y corré:

   ```bash
   # Nada de esto puede existir ya (si existe, el stack falla al crearlo):
   aws iam get-role --role-name pcm-ssm-role                 2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-ro                  2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-prov                2>&1 | tail -1
   CUENTA=$(aws sts get-caller-identity --query Account --output text)
   aws s3api head-bucket --bucket pcm-dumps-$CUENTA          2>&1 | tail -1
   ```

   Esperado: las cuatro líneas dicen `NoSuchEntity` / `Not Found`. Si algo ya
   existe, se frena y se decide antes de seguir (no se pisa).
   La plantilla pesa ~81 KB (más de 51.200 bytes): **no** sirve
   `validate-template --template-body` por CLI. La consola la sube a su bucket
   `cf-templates-…` (lo crea CloudFormation, no PCM; tope 1 MB) y la valida al
   cargarla. Revisada con `cfn-lint`, sin errores.
2. [ ] YO · **CloudFormation → Create stack → With new resources → Upload a
   template file** → `pcm-prerequisitos.yaml` → nombre **`pcm-prerequisitos`**
   → parámetros **todos vacíos** (`Sufijo`, `ZonaDns` y `NombresDns`: sin
   Route 53, bj-uat va por sslip.io) → tildar **«I acknowledge that AWS
   CloudFormation might create IAM resources with custom names»** → Create →
   esperar **CREATE_COMPLETE**. Crea:
   - documento SSM `pcm-readonly-discovery`;
   - rol + instance profile **`pcm-ssm-role`** (agente SSM + dumps en `pcm-*`);
   - bucket **`pcm-dumps-<cuenta>`** en us-east-1: privado, Block Public Access
     completo, SSE-S3, sin versionado, `incoming/` expira a los 7 días, solo TLS.
     **Todo bucket de PCM se llama `pcm-<uso>-<cuenta>`** (p. ej. el de backups,
     `pcm-backups-<cuenta>`): los permisos sólo alcanzan a ese patrón y a esta
     cuenta;
   - managed policies **`pcm-prod-readonly`** y **`pcm-prod-provision`** (con
     el Deny de CreateTags/DeleteTags sobre `primate:managed_by` y PutObject
     solo en `incoming/` de ese bucket);
   - usuarios **`pcm-prod-ro`** y **`pcm-prod-prov`**, cada uno con SOLO su
     policy, sin consola y sin access keys.

   La plantilla se niega a desplegarse fuera de us-east-1.

## 2 · verificar.sh (YO, CloudShell)

1. [ ] YO · CloudShell: subí `infra/cloudshell/verificar.sh` y corré
   `bash verificar.sh` (sin `SUFIJO`). Solo lectura. Hace:
   1. regiones con EC2 (**anotalas: son las del paso 3**);
   2. recursos que **ya** tengan la etiqueta `primate:managed_by` → tiene que
      dar **ninguno** (si aparece algo, FRENAR);
   3. `pcm-prod-ro` no modifica nada (y sí lee y corre solo el documento de lectura);
   4. `pcm-prod-prov` no toca instancias sin la etiqueta;
   5. `pcm-prod-prov` no agrega ni borra la etiqueta en recursos existentes;
   6. `pcm-prod-prov` sí crea instancias con la etiqueta (y no sin ella),
      opera las suyas y sube dumps solo a `incoming/` de su bucket.

   Esperado: **«TODO OK»** al final. Con «HAY PROBLEMAS» se frena (en la
   cuenta real **no** se espera la falla de la prueba 2 que daba la sandbox).
   Pegale la salida a CC.

## 3 · Regiones extra (YO)

1. [ ] YO · En **cada región con servidores a importar** que no sea us-east-1
   (la lista del paso 2.1; p. ej. Ohio, us-east-2): **cambiar la región en la
   consola** → CloudFormation → Create stack → `pcm-ssm-readonly.yaml` → nombre
   **`pcm-ssm-readonly`** → `Sufijo` vacío → Create → **CREATE_COMPLETE**.
   Solo crea el documento SSM de lectura `pcm-readonly-discovery` en esa región.
2. [ ] YO · Sin ese stack, la importación de esa región sale «sin documento» y
   sus servidores entran sin gemelo (no se rompe nada, pero queda incompleto).

## 4 · Access keys (YO + PCM)

1. [ ] YO · **IAM → Users → `pcm-prod-ro` → Security credentials → Create
   access key → «Application running outside AWS»**. Copiá Access Key ID y
   Secret al momento (la secret no se vuelve a mostrar). Lo mismo para
   **`pcm-prod-prov`**.
2. [ ] YO · **Dónde va cada key en PCM** (Claude no las ve): en
   http://127.0.0.1:19190 → app **Primate Cloud** → menú **Configuración →
   Cuentas AWS** → clic en la cuenta → botón **«Editar»** → pegar en
   **«Access Key ID»** y **«Secret Access Key»** → **«Guardar»** → botón
   **«Validar conexión»**. Tiene que quedar **«Conectada»** con el **AWS
   Account ID** completo (12 dígitos, el de Primate) y **«Respondió (ARN)»** =
   `arn:aws:iam::<cuenta>:user/<el usuario de la tabla>`.

   | Usuario IAM | Cuenta en PCM | Además |
   |---|---|---|
   | `pcm-prod-ro` | **«pcm-prod-ro»** | — |
   | `pcm-prod-prov` | **«pcm-prod-prov»** | la ficha dice **«Solo aprovisionamiento: Sí — no importa ni ve lo ajeno»**; las **mismas** keys van al perfil de aws cli (4.3) |

   Si se cruzan (las de lectura en la de aprovisionamiento o al revés),
   «Validar conexión» **falla**: la cuenta queda en **Error** con «Las keys
   responden como «pcm-prod-ro» (…), pero esta cuenta espera «pcm-prod-prov».
   ¿Están cruzadas?». Se pegan las correctas y se vuelve a validar. El usuario
   esperado es el campo **«Usuario IAM esperado»** de cada cuenta (ya cargado).
3. [ ] YO · **Perfil de aws cli** para subir el dump (keys de `pcm-prod-prov`),
   en tu Mac:

   ```bash
   aws configure --profile pcm-prod-provision
   #   AWS Access Key ID [None]:     <Access Key ID de pcm-prod-prov>
   #   AWS Secret Access Key [None]: <Secret de pcm-prod-prov>
   #   Default region name [None]:   us-east-1
   #   Default output format [None]: json

   # Comprobación (tiene que terminar en user/pcm-prod-prov):
   aws sts get-caller-identity --profile pcm-prod-provision --query Arn --output text
   ```

## 5 · Dump de Barraca Julia (YO)

1. [ ] YO · En la raíz del repo:

   ```bash
   cd ~/Odoo/clients/19.0/Primate-Cloude-Manager
   infra/subir_dump.sh ~/dumps/barracajulia/pcm_produccion/bj_v19_neutralizada_pcm_2026-10-27.zip
   ```

   Imprime «Respondió: <ARN>» y frena si no es el usuario `pcm-prod-prov`
   (variable `USUARIO_ESPERADO`); chequea que el sha256 local coincida con
   `SHA256SUMS.txt` (`60251a0c…a5a429`), sube con progreso a
   `s3://pcm-dumps-<cuenta>/incoming/barraca-julia/bj_v19_neutralizada_pcm_2026-10-27.zip`
   y al final verifica tamaño y sha256 en S3. Termina en **«LISTO»** e imprime
   la ruta para PCM: **copiala** (va en 6.3). 10,5 GiB: a 50 Mbit/s de subida
   ~30 min; a 200 Mbit/s ~8.
2. [ ] YO · Si termina en «HAY PROBLEMAS» o «Freno», no se restaura: pegale la
   salida a CC.

## 6 · Montaje de bj-uat (PCM con «pcm-prod-prov»)

1. [ ] PCM · Entornos → **`bj-uat`** (Staging, en Borrador) →
   **«Aprovisionar»**. El formulario ya viene **precargado** (ver «Preparado
   el 27-oct»); revisá que diga cuenta **pcm-prod-prov**, us-east-1,
   t3.xlarge, 100, Ubuntu 24.04, base `bj_uat`, **IP fija**, **«Dominio
   sslip.io automático»** y **«Emitir HTTPS al terminar»** marcados, Route 53
   apagado. Lo único que falta:
   - **«Verificar región»** → verde (security group, subred y perfil
     `pcm-ssm-role`; el SG `pcm-managed` lo crea PCM si falta);
   - **contraseña admin** (master del `odoo.conf`; guardala en el llavero);
   - **Aprovisionar**. El volumen sale gp3 (AMI de Canonical `ebs-gp3`).
2. [ ] PCM · **Todo solo, sin botones**: PCM crea la EC2, asigna la IP fija,
   arma el dominio **`bj-uat.<ip-con-guiones>.sslip.io`** con esa IP (sslip.io
   resuelve solo, sin registro DNS), instala Odoo con nginx para ese nombre
   y, al terminar, emite el certificado (Let's Encrypt **producción**) y lo
   verifica con un GET https real. Qué ver, en orden, en el chatter del
   entorno («Gestionar»; el más nuevo arriba):
   1. «IP fija (Elastic IP) <ip> asociada al servidor.»;
   2. «Dominio provisorio: bj-uat.<ip-con-guiones>.sslip.io (sslip.io con la
      IP fija, sin registro DNS).»;
   3. «Entorno aprovisionado y activo en bj-uat.<ip-con-guiones>.sslip.io.»;
   4. **«HTTPS activo: https://bj-uat.<ip-con-guiones>.sslip.io (Let's
      Encrypt).»** — y la instancia con el badge **«HTTPS activo»**.

   Si en vez del 4 dice «Sin HTTPS todavía en …», ver «Plan B si SSL falla».
   Probado en real el 27-oct en la sandbox (t3.small): montaje completo en
   4 min 41 s, certificado de Let's Encrypt de producción (emisor YE1) y
   http → 301 → https.

   Abrí `https://bj-uat.<ip>.sslip.io/web/login`: **candado válido**, emisor
   Let's Encrypt (no «(STAGING)»), y `http://` redirige a `https://`. Con la IP
   fija, un stop/start no rompe sslip.io ni el certificado.
   **«Activar HTTPS ahora» solo se usa si el chatter dice «Sin HTTPS
   todavía»** (badge «Sin HTTPS» con el motivo): ver «Plan B si SSL falla».
3. [ ] PCM · **«Restaurar desde un dump…»** (panel lateral): origen «Key S3
   manual» = la ruta que imprimió `subir_dump.sh` · formato: detectar ·
   **Eliminar el dump de S3 al finalizar: sí** · base destino `bj_uat` ·
   **desmarcar «Respaldar el destino antes»** (viene marcado; la base es la
   vacía del install) → Restaurar → Ok. PCM neutraliza solo porque la
   instancia es Staging, **antes** de arrancar Odoo, y corre el checklist de
   SELECTs **en el servidor**; si algo da rojo o falla la carga, Odoo queda
   **detenido** y el restore figura fallido con la cola del log.
   Proyección: **30–45 min** (ensayo con la reducida: 2 min 30 s).

   **Si el restore se corta** (PCM se reinicia, la Mac se duerme, el job
   muere): el job queda **fallido** con «NO se relanza automáticamente» en la
   bitácora, y el script puede **seguir corriendo en el servidor**. Antes de
   «Reintentar» (que relanza una vez, a mano): mirar el servidor por PCM
   (procesos de restore, `/var/tmp/pcm/`, disco). Si un restore de la misma
   base sigue vivo, el nuevo aborta solo («ya hay un restore de la base …
   corriendo», candado `/var/lock/pcm-restore-<base>.lock`) sin tocar nada.
4. [ ] YO · Checks post-montaje:
   - [ ] Chatter del entorno: «Checklist de SELECTs en verde» con
         `modules_installed=115`, `smtp_active=0`, `smtp_with_credentials=0`,
         `crons_active=0`, `mail_pending=0`, `website_with_domain=0`,
         `web_base_url=https://bj-uat.<ip>.sslip.io`.
   - [ ] Adjuntos: `PCM_FILESTORE_FILES` ≈ 261.891 y `attachments_stored`
         ≈ 264.000; abrir un PDF de una factura en el Odoo.
   - [ ] Login web en `https://bj-uat.<ip>.sslip.io` con `administrator`
         (contraseña del llavero `bj-v19-zip-admin`).
   - [ ] HTTPS activo (candado + badge en PCM) y http redirige a https.
   - [ ] En el Odoo (`?debug=1`): **Aplicaciones** → quitar «Aplicaciones» y
         elegir «Instalado» → **115**. **Acciones planificadas** → quitar
         «Todos» (incluye las archivadas) → **ninguna**. **Servidores de correo
         saliente** → ninguno activo; con «Archivado» aparece el de origen
         **sin usuario ni contraseña**. **Parámetros del sistema** →
         `web.base.url` = la URL sslip.io.

### ✅ Dominio definitivo de bj-uat (hecho 2026-10-28)
**`https://barracajuliav19.erp.primateuy.com`**, por **«Crear DNS ahora»**, en 70 s:
registro A `barracajuliav19.erp.primateuy.com → 32.200.58.86` (TTL 300) creado por PCM en la
zona `primateuy.com` (`Z06154501H54LEU4PFGH2`), INSYNC, propagación contra los NS de la zona,
vhost en paralelo, certificado de Let's Encrypt de producción verificado por https, y recién
ahí se retiró sslip.io (vhost y certificado) y se actualizó `web.base.url`. En la zona solo se
agregó ese registro (18 → 19, ninguno tocado). `/web/database` sigue en 404 en el dominio nuevo.

Para los próximos montajes: el stack tiene `ZonaDns = Z06154501H54LEU4PFGH2` y `NombresDns =
*.erp.primateuy.com`, y `pcm_produccion` tiene esos defaults (`pcm.route53_default_zone_id`,
`pcm.route53_default_domain_suffix`): «Crear DNS ahora» propone `<slug>.erp.primateuy.com` y
rechaza, antes de tocar nada, un nombre fuera de ese subdominio. Ver `docs/AGREGAR_DOMINIO.md`.

### Plan B si SSL falla
(Esto vale para el HTTPS del **montaje**. Al pasar después a un **dominio
definitivo** es distinto: si el certificado nuevo falla, PCM desarma lo nuevo y
la instancia **sigue en sslip.io con su HTTPS**, sin pasar por HTTP; la ficha
avisa «No se pudo pasar a <dominio>» con Reintentar / Descartar. Ver
`docs/AGREGAR_DOMINIO.md`.)

En el montaje, la instancia queda **activa en HTTP** (nunca rota): badge «Sin HTTPS» con el
motivo. Corregir la causa (IP cambiada, puerto 443) y **«Activar HTTPS
ahora»**. El reintento automático corre cada 10 min (solo gasta intento de
certbot con la propagación confirmada; tope 6 intentos y 24 h esperando).

### Rollback limpio de lo nuevo (sin tocar nada importado)
- Falló el install con la EC2 creada → «Reintentar instalación».
- Falló el restore → el chatter dice en qué paso; el dump sigue en S3 (solo
  se borra si salió bien): se repite el restore.
- Hay que deshacer todo → YO me asigno el grupo destructivo y termino el
  servidor (triple confirmación). IAM impide igual tocar lo importado.
  PCM libera la IP fija al terminar (si no puede, lo dice en el chatter:
  liberarla a mano en EC2 → Elastic IPs). Al terminar, me lo saco.

### Tiempos medidos en los ensayos (sandbox, base reducida)

| Paso | Medido |
|---|---|
| EC2 creada + bootstrap | ~15 s |
| Instalación de Odoo | 2 min 36 s |
| Dominio → sslip.io + HTTPS | 30 s |
| Restore (bajar, descomprimir, cargar, filestore, neutralizar, checklist) | 2 min 30 s |

## 7 · Importación de solo lectura (fuera de horario)

### AWS (PCM con «pcm-prod-ro»)
1. [ ] PCM · Cuenta **«pcm-prod-ro»** → **«Importar servidores»**, **todas las
   regiones con recursos**. Todo entra `adopted_readonly`, con el entorno
   marcado «Importado de AWS». **bj-uat no se importa**: el reporte lo lista en
   «Ya gestionados por PCM (no se importan)» (es lo esperado).
2. [ ] YO · Reporte → **«Sin acceso SSM»**: son los servidores AWS que ya
   existían **sin agente SSM o sin un rol** (instance profile) que le permita
   hablar con SSM. Entran igual al inventario, en solo lectura, pero **sin
   gemelo**. **PCM no les agrega ningún rol** ni instala nada: eso sería
   modificarlos. Su gemelo se saca **por ssh con `infra/gemelo_cloudpepper.sh`**,
   igual que Cloudpepper: agregalos a `~/.pcm_produccion/cloudpepper_servidores.txt`
   y, al subir el `.json` en «Importar servidor de Cloudpepper», elegí **ese
   servidor en «Entorno existente»** (así el gemelo queda sobre el servidor AWS
   importado, sin crear otro). Una región en
   «Regiones sin documento de lectura» = falta el stack del paso 3 ahí.
3. [ ] PCM · El gemelo se captura en la importación; «Capturar gemelo (solo
   lectura)» para re-capturar. Si un servidor con muchos Odoo da «la salida
   inline de SSM se corta en 24.000 caracteres», va por ssh como Cloudpepper.

### Cloudpepper (PCM no se conecta: ni API ni SSH)
1. [ ] YO · Completar **`~/.pcm_produccion/cloudpepper_servidores.txt`** (ya
   existe, 600, con el formato): una línea por servidor,
   `<alias> <destino ssh> <sudo|root>`.
   - **alias**: nombre corto sin espacios (p. ej. `cp-clientes-1`); es el
     nombre del `.json` y el «nombre del servidor» al importarlo en PCM.
   - **destino ssh**: lo mismo que usás en `ssh <destino>`: `usuario@IP` o un
     `Host` de `~/.ssh/config` (ahí van puerto, clave y usuario). Tiene que
     entrar **sin preguntar nada** (clave cargada, host conocido o nuevo).
   - **modo**: `root` si entrás como root; `sudo` si el usuario tiene **sudo
     sin contraseña** (si la pide, ese servidor falla y se sigue).
   - Sumá los servidores AWS que salieron «Sin acceso SSM» en 7.2.
   - Probá cada destino antes con `ssh <destino> true` (no debe pedir nada).
2. [ ] YO · En la raíz del repo: `infra/gemelo_cloudpepper.sh`. Recorre todos
   por ssh en solo lectura (el script viaja por la entrada y el gemelo vuelve
   por la salida: no se copia ni se escribe nada allá) y deja
   **`~/pcm_gemelos/<fecha>/<alias>.json`**. Resumen OK/FALLA por servidor.
3. [ ] PCM · Inventario → **«Importar servidor de Cloudpepper»** → nombre =
   el alias → subir el `.json`. Queda marcado «Importado de Cloudpepper».
   Para un servidor AWS «Sin acceso SSM»: en vez del nombre, elegirlo en
   **«Entorno existente»** y subir su `.json`.

### Revisión del gemelo (por instancia, pestaña «Gemelo») — YO
- [ ] Cada Odoo del servidor aparece como instancia propia y con su base.
- [ ] Repos en el orden del `addons_path`, con remote, rama y SHA.
- [ ] **ALERTAS de cambios locales** (archivos sin commitear / commits sin
      pushear): la mudanza por git LOS PERDERÍA. Resolver con el dueño del
      repo ANTES de mudar (en origen, nunca desde PCM).
- [ ] Odoo core/enterprise con commit, Python y `pip freeze`.
- [ ] Base (PostgreSQL y tamaño), filestore, nginx (dominios y certificados), crons.
- [ ] Clasificar cada instancia en su proyecto (cliente).

### Cero acciones sobre lo importado — YO
- [ ] Banner **IMPORTADO — SOLO LECTURA** y **ningún** botón que modifique
      (solo «Capturar gemelo») en entornos, servidores e instancias importados.
- [ ] Bitácora: cualquier intento queda como «Bloqueo de solo lectura».

## 8 · Cierre y aviso a INCO

- [ ] YO · Bitácora de PCM: ningún «Bloqueo de solo lectura» inesperado.
- [ ] YO · Grupo «PCM: operaciones destructivas» **vacío**.
- [ ] YO · Video del recorrido completo (credenciales y datos de clientes blureados).
- [ ] CC · KB actualizada (estado + decisiones).
- [ ] YO · **Aviso a INCO**: servidor `bj-uat` (UAT, no productivo) en
      us-east-1 (t3.xlarge, gp3 100 GB, `i-0617ee15ddadb0a71`, IP fija `32.200.58.86`, `https://barracajuliav19.erp.primateuy.com`) para que
      configuren **backups** (snapshots del EBS), **DR** y **alarmas** (CPU,
      disco, estado del sistema). PCM no tiene política de backup en este
      entorno.
- [ ] YO · Limpieza de lo de prueba: zona `pcm-ensayo-primate.com` de la
      sandbox (primero sus registros `erp` y `manual`), access keys de
      `pcm-prod-ro-cfn` y `pcm-prod-prov-cfn`, access key de `cc-lab-domain`
      (y su perfil en `~/.aws/credentials` y `~/.aws/config`), CSVs de keys en
      `~/Downloads`.
