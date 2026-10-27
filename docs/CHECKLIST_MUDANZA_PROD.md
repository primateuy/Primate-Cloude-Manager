# Runbook — PCM Producción y montaje de Barraca Julia UAT

Orden obligatorio: no se pasa a la fase siguiente sin cerrar la anterior.
Todo lo importado es **solo lectura sin excepción** (UI, modelo, cerrojo de
frontera e IAM). Detalle de IAM y del simulador: [`docs/iam/README.md`](iam/README.md).

**Quién hace cada paso:**
- **YO** = Daryl, a mano (consola de AWS, CloudShell, tu Mac).
- **CC** = Claude Code: prepara, guía y verifica en tu Mac. Nunca toca la cuenta real.
- **PCM** = lo hace PCM cuando apretás el botón.

## ⛔ Regla del día (para Claude, sin excepción)

Claude **no ejecuta ningún comando directo contra servidores o cuentas
productivas**: ni `ssh`, ni `aws` cli, ni `rm`, ni nada equivalente. Todo pasa
por PCM: lectura e importación con la cuenta **«Primate»** (usuario
`pcm-prod-ro`), y el montaje de Barraca Julia con **«Primate -
aprovisionamiento»** (usuario `pcm-prod-prov`). Si algo requiere salirse de
eso, Claude **frena y le pregunta a Daryl** antes de hacer nada.

Los comandos directos de este runbook (CloudFormation, CloudShell, gemelo de
Cloudpepper por ssh, subida del dump) los corre **Daryl**; Claude solo los
prepara. Las credenciales productivas las carga **Daryl** en PCM: Claude no
las pide ni las lee.

**Video:** el recorrido completo se graba (credenciales y datos de clientes
blureados en el montaje).

---

## Decisiones tomadas

- PCM Producción en una base **nueva** (`pcm_produccion`), separada de la sandbox.
- **Barraca Julia es un ambiente de PRUEBA / UAT, no de producción.** El
  cuadre contable **no está validado** (`05_cuadre` del 11-sep: 33
  diferencias en caja y bancos 2022–2024). Nombre **`bj-uat`** (entorno e
  instancia) y tipo **Staging** (≠ producción) en PCM, con el aviso en su
  chatter. El cliente sigue operando en su sistema actual.
- Base: **`bj_v19_neutralizada_2026-09-14.zip`** (OK de Daryl, 27-oct),
  regenerada neutralizada para PCM.
- **Sin `create_dns`**: HTTPS vía **sslip.io**.
- **Sin LocalizacionUy**: la base no tiene ningún módulo instalado y para v19
  solo existe la rama `19.0_staging`. Queda como pendiente en la KB.
- Región **us-east-1** (Virginia): región de **pruebas**, bajo demanda.
  INCO tiene **EC2 Instance Savings Plans (de familia) en Ohio**: las instancias
  definitivas que migren desde Cloudpepper van en **Ohio (us-east-2) y en esa
  familia** (a confirmar por INCO). bj-uat NO entra en el savings plan.
- Servidor según INCO: **t3.xlarge, EBS gp3 100 GB, Ubuntu 24.04** (27-oct:
  INCO bajó el disco de 200 a 100 GB; entra, ver «Tamaños reales»). Si hiciera
  falta más: [`docs/AMPLIAR_DISCO.md`](AMPLIAR_DISCO.md) (PCM no lo hace todavía).

## Tamaños reales

| | |
|---|---|
| Artefacto a restaurar | **11.229.926.329 bytes (10,46 GiB)**, sha256 `60251a0c77745c3055675d67fbe3d0291a663ad0cd736d2662b0fd3055a5a429` |
| Descomprimido | 20,81 GiB = `dump.sql` 11,94 GiB + filestore 8,87 GiB (261.891 archivos) |
| Base cargada | **19,19 GiB** (medida el 27-oct; la `bj_v19` local mide 43 GB por bloat) |
| Pico del restore (actual) | ~41 GiB: lo descomprimido + la base mientras carga (el zip se borra al extraerse y el `dump.sql` al cargarse) |
| + sistema, Odoo y logs | ~7 GiB → **~48 GiB de ~96 utilizables en 100 GB** |
| Chequeo de PCM | pide 23 GiB para descomprimir y ~45 GiB para el pico (+10 %): **pasa con 100 GB**; con 30 GB corta antes de tocar nada |
| Ensayo en sandbox (23-oct) | **m7i-flex.large** (el plan Free rechaza t3.xlarge), gp3 200 GB, base REDUCIDA |

## Piezas preparadas (en el repo)

| Pieza | Para qué |
|---|---|
| `infra/cloudformation/pcm-prerequisitos.yaml` | Todo lo de IAM/SSM/S3 de la cuenta en un stack, sin reemplazar nada a mano |
| `infra/cloudformation/pcm-ssm-readonly.yaml` | Solo el documento SSM, para otras regiones con servidores a importar |
| `infra/cloudformation/generar.py` | Genera las dos plantillas desde `docs/iam/*.json` y `data/ssm/` (`--check` avisa si quedaron viejas) |
| `infra/cloudshell/verificar.sh` | Verificación de solo lectura para pegar en CloudShell |
| `infra/gemelo_cloudpepper.sh` | Gemelo de todos los servidores Cloudpepper por ssh (solo lectura), un `.json` por servidor |
| `infra/cloudpepper_servidores.example` | Formato de la lista de servidores Cloudpepper |
| `infra/subir_dump.sh` | Subida del dump con el perfil `pcm-prod-provision`, con progreso y verificación de tamaño y sha256 |

---

## F0 · Verificaciones y push — ✅ hecho (23-oct)

- [x] CC · Llave `primate:managed_by` solo al crear (IAM + frontera + re-etiquetado).
- [x] CC · Escaneo de secretos de los commits a subir: limpio.
- [ ] YO · Push: `git push origin 19.0`.

## F1 · PCM Producción

| # | Quién | Paso |
|---|---|---|
| 1 | CC ✅ | Base **`pcm_produccion`** (Odoo 19 Enterprise, sin demo) en `~/Odoo/clients/19.0/pcm_produccion/`: config, filestore (`data/`) y log propios, puerto **19190** (solo `127.0.0.1`), `list_db = False`, master password aleatoria (solo el hash). PCM + `auth_totp`, es_UY. |
| 2 | CC ✅ | Clave de cifrado de credenciales **fuera de la base**: `~/.pcm_produccion/fernet.key` (600), la exporta `levantar.sh`. |
| 3 | CC ✅ | Parámetros: `pcm.ssl_use_le_staging = False`; tope SSL y tope SSM en sus defaults (24 h y 4 h). Grupo «PCM: operaciones destructivas» **vacío**. |
| 4 | CC ✅ | Cuentas AWS **«Primate»** (lectura) y **«Primate - aprovisionamiento»** (**Solo aprovisionamiento** marcado), us-east-1, Access Key IAM, **sin credenciales**. |
| 5 | CC ✅ | Proyecto **Barraca Julia** (cuenta «Primate - aprovisionamiento») y entorno **`bj-uat`** en Borrador, tipo **Staging**, con el aviso de UAT en el chatter. |
| 6 | YO | Levantar: `~/Odoo/clients/19.0/pcm_produccion/levantar.sh` → http://127.0.0.1:19190, usuario `admin`. |
| 7 | YO | Preferencias → **Seguridad → «Habilitar la A2F»**. |
| 8 | YO | Enterprise: registrar el código de suscripción (si no, vence a los 30 días). |
| — | pendiente | Mudar `pcm_produccion` a un servidor con HTTPS (anotado en la KB). |

> No usar `pcm_demo` (sandbox) para nada productivo: tiene una cuenta de
> prueba vieja llamada «Primate Producción» con un ID ficticio (123456789012).

## F1b · Ensayo de los permisos en la SANDBOX (antes de la cuenta real)

La sandbox **ya tiene** `pcm-ssm-role` (lo usa `pcm_demo`): desplegar la plantilla
tal cual fallaría ahí. Se despliega con el parámetro **`Sufijo = -cfn`**: todo
queda con nombres paralelos (`pcm-ssm-role-cfn`, `pcm-prod-ro-cfn`,
`pcm-prod-prov-cfn`, `pcm-readonly-discovery-cfn`, `pcm-dumps-<cuenta>-cfn`) y no
se toca nada de lo que usa `pcm_demo`.

1. [ ] YO · Consola de la sandbox → CloudFormation → stack `pcm-prerequisitos-cfn`
   con `pcm-prerequisitos.yaml` y **Sufijo `-cfn`**.
2. [ ] YO · CloudShell: `SUFIJO=-cfn bash verificar.sh`. **Esperado en la
   sandbox: todo OK MENOS la prueba 2** («hay recursos con la llave»): las
   máquinas, volúmenes, security groups y la Elastic IP que montó `pcm_demo`
   ya tienen `primate:managed_by=pcm`, así que el resumen termina en «HAY
   PROBLEMAS (1)». Cualquier otra FALLA sí es un problema.
3. [ ] YO · Keys de `pcm-prod-ro-cfn` y `pcm-prod-prov-cfn` cargadas en una **base
   de prueba** (no en `pcm_produccion`), con los parámetros
   `pcm.ssm_instance_profile = pcm-ssm-role-cfn` y
   `pcm.readonly_discovery_document = pcm-readonly-discovery-cfn`.
4. [ ] CC · Con ESOS usuarios: importación de solo lectura con gemelo y un
   montaje chico (dump reducido, disco, restore) hasta HTTPS. Cada AccessDenied
   se anota, se corrige la policy y se repite hasta que pase limpio. Video.
   Terminar los recursos al final.

## F2 · IAM en la cuenta real (YO, consola + CloudShell)

0. [ ] YO · Mandar a INCO `entregables/inco/pcm_permisos_<fecha>.zip` (lo arma
   `infra/armar_entregable_inco.sh`, que verifica que no lleve cuentas, IPs,
   keys, correos ni nombres de clientes).

1. [ ] YO · **Antes de desplegar**, en CloudShell (us-east-1). Subí
   `pcm-prerequisitos.yaml` desde «Actions → Upload file» y corré:

   ```bash
   # Nada de esto puede existir ya (si existe, el stack falla al crearlo):
   aws iam get-role --role-name pcm-ssm-role                 2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-ro                  2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-prov                2>&1 | tail -1
   aws s3api head-bucket --bucket pcm-dumps-$(aws sts get-caller-identity --query Account --output text) 2>&1 | tail -1
   ```

   Esperado: las cuatro líneas dicen `NoSuchEntity` / `Not Found`. Si algo ya
   existe, se frena y se decide antes de seguir (no se pisa).
   La plantilla pesa más de 51.200 bytes: **no** sirve `validate-template
   --template-body` por CLI. La consola la sube a su bucket `cf-templates-…`
   (lo crea CloudFormation, no PCM; tope 1 MB) y la valida al cargarla.
   Revisada con `cfn-lint`, sin errores.
2. [ ] YO · **CloudFormation → Create stack → With new resources → Upload a
   template file** → `pcm-prerequisitos.yaml` → nombre **`pcm-prerequisitos`**
   → sin parámetros → tildar **«I acknowledge that AWS CloudFormation might
   create IAM resources with custom names»** → Create. Crea:
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
3. [ ] YO · **Otras regiones con servidores a importar** (las lista el paso 4,
   sección 1): en cada una, stack con `pcm-ssm-readonly.yaml` (solo el
   documento SSM).
4. [ ] YO · CloudShell: subí `infra/cloudshell/verificar.sh` y corré
   `bash verificar.sh`. Solo lectura. Hace:
   1. regiones con EC2;
   2. recursos que **ya** tengan la etiqueta `primate:managed_by` → tiene que
      dar **ninguno** (si aparece algo, FRENAR);
   3. `pcm-prod-ro` no modifica nada (y sí lee y corre solo el documento de lectura);
   4. `pcm-prod-prov` no toca instancias sin la etiqueta;
   5. `pcm-prod-prov` no agrega ni borra la etiqueta en recursos existentes;
   6. `pcm-prod-prov` sí crea instancias con la etiqueta (y no sin ella),
      opera las suyas y sube dumps solo a `incoming/` de su bucket.

   Esperado: **«TODO OK»** al final. Con «HAY PROBLEMAS» se frena.
5. [ ] YO · **Access keys** (consola → IAM → Users → `<usuario>` → Security
   credentials → Create access key → «Application running outside AWS»):
   una para `pcm-prod-ro` y una para `pcm-prod-prov`. Se copian al momento
   (la secret no se vuelve a mostrar).
6. [ ] YO · **Credenciales en PCM** (Claude no las ve). En
   http://127.0.0.1:19190 → app **Primate Cloud** → **Configuración → Cuentas
   AWS** → clic en la cuenta → botón **«Editar»** → pegar en **«Access Key ID»**
   y **«Secret Access Key»** → **«Guardar»** → botón **«Validar conexión»**
   (arriba, violeta). Tiene que quedar **«Conectada»** y completar el Account ID.
   - «Primate» ← claves de **`pcm-prod-ro`**.
   - «Primate - aprovisionamiento» ← claves de **`pcm-prod-prov`**. En la
     ficha tiene que decir **«Solo aprovisionamiento: Sí»**.
7. [ ] YO · **Perfil de aws cli** para subir el dump (las mismas claves de
   `pcm-prod-prov`):

   ```bash
   aws configure --profile pcm-prod-provision
   #   AWS Access Key ID:     <la de pcm-prod-prov>
   #   AWS Secret Access Key: <la de pcm-prod-prov>
   #   Default region name:   us-east-1
   #   Default output format: json
   ```

## F3 · Importación de solo lectura (PCM con «Primate»; YO para Cloudpepper)

### AWS
1. [ ] PCM · Cuenta «Primate» → **«Importar servidores»**, **todas las
   regiones con recursos**. Todo entra `adopted_readonly`, con el entorno
   marcado «Importado de AWS».
2. [ ] YO · Reporte → **«Sin acceso SSM»**: a esos servidores **no se les
   agrega** nada (sería modificarlos); su gemelo va por ssh como los de
   Cloudpepper (agregalos a la lista del paso siguiente).
3. [ ] PCM · El gemelo se captura en la importación; «Capturar gemelo (solo
   lectura)» para re-capturar. Si un servidor con muchos Odoo da «la salida
   inline de SSM se corta en 24.000 caracteres», va por ssh como Cloudpepper.

### Cloudpepper (PCM no se conecta: ni API ni SSH)
1. [ ] YO · Lista de servidores: copiá `infra/cloudpepper_servidores.example`
   a **`~/.pcm_produccion/cloudpepper_servidores.txt`** y completá una línea
   por servidor: `<alias> <destino ssh> <sudo|root>`. El destino es lo mismo
   que usás en `ssh <destino>` (usuario@host o un Host de `~/.ssh/config`, que
   es donde van puerto, clave y usuario). `sudo` = sudo **sin contraseña**.
2. [ ] YO · En la raíz del repo: `infra/gemelo_cloudpepper.sh`. Recorre todos
   por ssh en solo lectura (el script viaja por la entrada y el gemelo vuelve
   por la salida: no se copia ni se escribe nada allá) y deja
   **`~/pcm_gemelos/<fecha>/<alias>.json`**. Resumen OK/FALLA por servidor.
3. [ ] PCM · Inventario → **«Importar servidor de Cloudpepper»** → nombre =
   el alias → subir el `.json`. Queda marcado «Importado de Cloudpepper».

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
- [ ] El grupo «PCM: operaciones destructivas» sigue **vacío**.

## F4 · Base de Barraca Julia UAT

1. [x] YO · OK con `bj_v19_neutralizada_2026-09-14.zip` (27-oct).
2. [x] CC · Artefacto neutralizado generado en la Mac con
   `~/dumps/barracajulia/_herramientas/pcm_artefacto_neutralizado.py`:
   carga el zip en una base temporal, saca credenciales, aplica la **misma**
   neutralización que PCM y corre el checklist de SELECTs; sin todo en verde
   no arma el zip. Resultado (ruta, tamaño, sha256 y checklist): KB de PCM,
   «Estado actual» del 27-oct.
3. [ ] YO · Subir (en la raíz del repo, perfil del paso F2.7):

   ```bash
   infra/subir_dump.sh ~/dumps/barracajulia/pcm_produccion/bj_v19_neutralizada_pcm_2026-10-27.zip
   ```

   Chequea que el perfil sea `pcm-prod-prov`, que el sha256 local coincida con
   `SHA256SUMS.txt`, sube con progreso a
   `s3://pcm-dumps-<cuenta>/incoming/barraca-julia/…` y al final verifica
   tamaño y sha256 en S3. Imprime la ruta para PCM. 10,5 GiB: a 50 Mbit/s de
   subida ~30 min; a 200 Mbit/s ~8.

## F5 · Montaje de bj-uat en us-east-1 (PCM con «Primate - aprovisionamiento»)

1. [ ] PCM · Entornos → **`bj-uat`** (ya creado, Staging, en Borrador) →
   **«Aprovisionar»**: región us-east-1 · **«Verificar región»** (security
   group, subred y perfil `pcm-ssm-role`) · dominio provisorio
   `bj-uat.pcm.invalid` · contraseña admin (master del odoo.conf) · nombre de
   la instancia **`bj-uat`** · **t3.xlarge** · disco **100** · Ubuntu 24.04 ·
   PostgreSQL local, base **`bj_uat`** · **sin** Route 53 · **sin** «Emitir
   HTTPS» (todavía no hay IP). El volumen sale gp3 (AMI de Canonical
   `ebs-gp3`).
2. [ ] PCM · Con la IP: **«Gestionar»** → **URL principal** =
   `bj-uat.<ip-con-guiones>.sslip.io` → Guardar. PCM reescribe el vhost y
   emite HTTPS (Let's Encrypt producción). Esperar **«HTTPS activo»** (en el
   ensayo: 30 s). ⚠ Sin Elastic IP: **no detener el servidor** (al arrancar
   cambia la IP y sslip.io deja de apuntar).
3. [ ] PCM · **«Restaurar desde un dump…»** (panel lateral): origen «Key S3
   manual» = la ruta que imprimió `subir_dump.sh` · formato: detectar ·
   **Eliminar el dump de S3 al finalizar: sí** · base destino `bj_uat` ·
   **desmarcar «Respaldar el destino antes»** (viene marcado; la base es la
   vacía del install) → Restaurar → Ok. PCM neutraliza solo porque la
   instancia es Staging, **antes** de arrancar Odoo, y corre el checklist de
   SELECTs **en el servidor**; si algo da rojo o falla la carga, Odoo queda
   **detenido** y el restore figura fallido con la cola del log.
   Proyección: **30–45 min** (ensayo con la reducida: 2 min 30 s).
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

### Lo que enseñó el ensayo (23-oct, sandbox, ya corregido)
- El plan Free rechaza t3.xlarge: PCM informa el error de AWS y no queda nada
  creado; «Aprovisionar» se puede repetir. El ensayo usó m7i-flex.large.
- Restore fallido por permisos (zip en modo 000) → `Permission denied`
  después del drop; Odoo quedó **detenido** a propósito. Arreglado en el
  generador (644) y en PCM (normaliza permisos al descomprimir).
- El error del restore muestra la cola del log (antes: «exit status 1»).

### Tiempos medidos en el ensayo (base reducida, m7i-flex.large)

| Paso | Medido |
|---|---|
| EC2 creada + bootstrap | ~15 s |
| Instalación de Odoo | 2 min 36 s |
| Dominio → sslip.io + HTTPS | 30 s |
| Restore (bajar, descomprimir, cargar, filestore, neutralizar, checklist) | 2 min 30 s |

### Plan B si SSL falla
La instancia queda **activa en HTTP** (nunca rota): badge «Sin HTTPS» con el
motivo. Corregir la causa (IP cambiada, puerto 443) y **«Activar HTTPS
ahora»**. El reintento automático corre cada 10 min (solo gasta intento de
certbot con la propagación confirmada; tope 6 intentos y 24 h esperando).

### Rollback limpio de lo nuevo (sin tocar nada importado)
- Falló el install con la EC2 creada → «Reintentar instalación».
- Falló el restore → el chatter dice en qué paso; el dump sigue en S3 (solo
  se borra si salió bien): se repite el restore.
- Hay que deshacer todo → YO me asigno el grupo destructivo y termino el
  servidor (triple confirmación). IAM impide igual tocar lo importado.
  Al terminar, me lo saco.

## F6 · Cierre y aviso a INCO

- [ ] YO · Bitácora de PCM: ningún «Bloqueo de solo lectura» inesperado.
- [ ] YO · Grupo destructivo vacío.
- [ ] YO · Video del recorrido completo (credenciales y datos de clientes blureados).
- [ ] CC · KB actualizada (estado + decisiones).
- [ ] YO · **Aviso a INCO**: servidor `bj-uat` (UAT, no productivo) en
      us-east-1 (t3.xlarge, gp3 100 GB, id de instancia e IP) para que
      configuren **backups** (snapshots del EBS), **DR** y **alarmas** (CPU,
      disco, estado del sistema). PCM no tiene política de backup en este
      entorno.
- [ ] YO · Revocar la access key de `cc-lab-domain`.
