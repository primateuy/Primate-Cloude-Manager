# Runbook — PCM Producción y montaje de Barraca Julia

Orden obligatorio: no se pasa a la fase siguiente sin cerrar la anterior.
Todo lo importado es **solo lectura sin excepción** (UI, modelo, cerrojo de
frontera e IAM). Detalle de IAM y del simulador: [`docs/iam/README.md`](iam/README.md).

**Quién:** **Daryl** = vos, a mano · **CC** = Claude Code (prepara, guía,
verifica en tu Mac) · **PCM** = lo hace PCM cuando apretás el botón.

## ⛔ Regla del día (para Claude, sin excepción)

Claude **no ejecuta ningún comando directo contra servidores o cuentas
productivas**: ni `ssh`, ni `aws` cli, ni `rm`, ni nada equivalente. Todo pasa
por PCM: lectura e importación con la cuenta de **`pcm-prod-readonly`**, y la
provisión de Barraca Julia con la de **`pcm-prod-provision`**. Si algo
requiere salirse de eso, Claude **frena y le pregunta a Daryl** antes de
hacer nada.

Los pocos comandos directos de este runbook (Policy Simulator, listado de la
llave de propiedad, gemelo de Cloudpepper por ssh, subida del dump al bucket)
los corre **Daryl** desde su máquina; Claude solo los prepara. Las
credenciales productivas las carga **Daryl** en PCM: Claude no las pide ni
las lee.

**Video:** el recorrido completo se graba (credenciales y datos de clientes
blureados en el montaje).

---

## Decisiones tomadas

- PCM Producción en una base **nueva** (`pcm_produccion`), separada de la sandbox.
- Barraca Julia **sin `create_dns`**, con HTTPS vía **sslip.io**.
- Se monta la **última base completa** de Barraca Julia, **neutralizada**: el
  cliente sigue operando en su sistema actual.
- Región **us-east-1** (Virginia): región de pruebas para el uso real de PCM.
  Las producciones definitivas se coordinan con INCO (savings plans de Ohio).
- Servidor según INCO: **t3.xlarge, EBS gp3 200 GB, Ubuntu 24.04**.

---

## F0 · Verificaciones y push — ✅ hecho (23-oct)

- [x] Llave `primate:managed_by` solo al crear (IAM + frontera + re-etiquetado).
- [x] Escaneo de secretos de los commits a subir: limpio.
- [x] Push (Daryl): `git push origin 19.0`.

## F1 · PCM Producción

| # | Quién | Paso |
|---|---|---|
| 1 | CC ✅ | Base **`pcm_produccion`** (Odoo 19 Enterprise, sin demo) en `~/Odoo/clients/19.0/pcm_produccion/`: config, filestore (`data/`) y log propios, puerto **19190** (solo `127.0.0.1`), `list_db = False`, master password aleatoria (solo el hash). PCM + `auth_totp` instalados, idioma es_UY. |
| 2 | CC ✅ | Clave de cifrado de credenciales **fuera de la base**: `~/.pcm_produccion/fernet.key` (600), la exporta `levantar.sh`. Si se pierde, hay que volver a cargar las claves AWS (no hay otra pérdida). |
| 3 | CC ✅ | Parámetros: `pcm.ssl_use_le_staging = False` (LE de producción); `pcm.ssl_pending_max_hours` **sin fijar** (default real del código: 24 h); `pcm.ssm_long_timeout_seconds` sin fijar (default 4 h). Grupo «PCM: operaciones destructivas» **vacío**. |
| 4 | Daryl | Levantar: `~/Odoo/clients/19.0/pcm_produccion/levantar.sh` → http://127.0.0.1:19190 · usuario `admin` (contraseña dada por pantalla el 23-oct). |
| 5 | Daryl | Preferencias → **Seguridad → «Habilitar la A2F»** (2FA) y, si querés, cambiar la contraseña. |
| 6 | Daryl | Enterprise: registrar el código de suscripción (sin eso la base avisa vencimiento a los 30 días). |
| — | pendiente | Mudar `pcm_produccion` a un servidor con HTTPS (anotado en la KB). Hoy es local y por http en 127.0.0.1. |

> No usar `pcm_demo` (sandbox) para nada productivo: tiene una cuenta de
> prueba vieja llamada «Primate Producción» con un ID ficticio (123456789012).

## F2 · IAM en la cuenta real (Daryl, en la consola)

1. [ ] **Rol `pcm-ssm-role`** (instance profile de las EC2): si no existe en
   la cuenta real, crearlo como dice `RUNBOOK_ONBOARDING.md` §2
   (`AmazonSSMManagedInstanceCore` + inline `s3:GetObject`/`s3:PutObject`
   sobre `arn:aws:s3:::pcm-*/*`). Sin `GetObject` la EC2 no puede bajar el dump.
2. [ ] Usuario **`pcm-prod-ro`** con `docs/iam/pcm-prod-readonly.json`:
   reemplazar `<ACCOUNT_ID>`; **borrar** el statement
   `SalidaDelDiscoveryPorS3Opcional` si no se usa un bucket para la salida del
   discovery. Nada más adjunto.
3. [ ] Documento SSM **`pcm-readonly-discovery`** en **cada región con
   servidores** (comando en `docs/iam/README.md`, desde el JSON del commit de
   hoy: incluye el script sin escrituras laterales).
4. [ ] Usuario **`pcm-prod-prov`** con `docs/iam/pcm-prod-provision.json`:
   reemplazar `<ACCOUNT_ID>`; **borrar** el statement
   `DnsSoloNombresDePCMEnLaZonaReal` (Barraca Julia va sin Route 53: así esa
   identidad no puede tocar ningún DNS).
5. [ ] **Policy Simulator** (`docs/iam/README.md`, casos 1–3) con un ID de
   instancia productiva real:
   - `TerminateInstances`/`StopInstances` sobre la importada → **explicitDeny** (ro) / **implicitDeny** (prov);
   - `SendCommand` con `AWS-RunShellScript` → **implicitDeny**;
   - `SendCommand` con `pcm-readonly-discovery` → **allowed**;
   - `CreateTags`/`DeleteTags` de `primate:managed_by` sobre una existente → **explicitDeny** (los dos usuarios).
6. [ ] **Recursos ya etiquetados**: el comando de solo lectura de
   `docs/iam/README.md` («Antes de importar»), con el perfil de `pcm-prod-ro`.
   **Esperado: solo `-- fin del listado --`.** Si aparece algo, se frena.
7. [ ] **Bucket del dump** (ver F4): crearlo antes de subir.
8. [ ] En PCM Producción (Daryl carga las claves; Claude no las ve):
   - Cuenta **«Prod — solo lectura»** con las claves de `pcm-prod-ro`
     (*Solo aprovisionamiento*: apagado). **Validar conexión.**
   - Cuenta **«Prod — aprovisionamiento»** con las de `pcm-prod-prov` y
     **Solo aprovisionamiento** marcado. **Validar conexión.**

## F3 · Importación de solo lectura (PCM, con «Prod — solo lectura»)

### AWS
1. [ ] PCM → **Importar servidores**, cuenta «Prod — solo lectura», **todas
   las regiones con recursos**. Todo entra `adopted_readonly`, con el entorno
   marcado «Importado de AWS».
2. [ ] Reporte de la importación → **«Sin acceso SSM»**: a esos servidores
   **no se les agrega** nada (sería modificarlos); su gemelo va por archivo
   como el de Cloudpepper.
3. [ ] Por cada servidor con SSM, el gemelo se captura en la importación;
   «Capturar gemelo (solo lectura)» para re-capturar.

### Cloudpepper (PCM no se conecta: ni API ni SSH)
1. [ ] Por cada servidor, **desde tu máquina** (lo corre Daryl), en la raíz del repo:

   ```bash
   ssh <servidor> 'sudo bash -s' \
     < primate_cloud_manager/data/pcm_readonly_discovery.sh \
     > gemelo-<servidor>.txt
   ```

   El script viaja por la entrada de ssh y el gemelo vuelve por la salida:
   **no se copia nada al servidor ni se escribe nada allá** (tampoco
   `.git/index` ni `.pyc`: el script los apaga). Si el usuario de ssh ya es
   root, sacar el `sudo`. La salida **no es JSON plano**: son líneas
   `PCM_TWIN_B64:…` (JSON comprimido y en base64) cerradas por
   `PCM_TWIN_OK`. Ese marco permite que PCM detecte una salida cortada, y es
   lo que espera el asistente. Chequeo rápido:
   `tail -1 gemelo-<servidor>.txt` → `PCM_TWIN_OK`. Lo único que queda en
   el servidor es lo que deja cualquier ssh: el login en los logs del
   sistema y la línea de `sudo` en el log de autenticación.
2. [ ] PCM → Inventario → **Importar servidor de Cloudpepper** → nombre del
   servidor + archivo. Queda marcado «Importado de Cloudpepper».

### Revisión del gemelo (por instancia, pestaña «Gemelo»)
- [ ] Cada Odoo del servidor aparece como instancia propia y con su base.
- [ ] Repos en el orden del `addons_path`, con remote, rama y SHA.
- [ ] **ALERTAS de cambios locales** (archivos sin commitear / commits sin
      pushear): la mudanza por git LOS PERDERÍA. Resolver con el dueño del
      repo ANTES de mudar (commitear y pushear en origen, nunca desde PCM).
- [ ] Odoo core/enterprise con commit, Python y `pip freeze`.
- [ ] Base (versión de PostgreSQL y tamaño) y filestore (ruta y tamaño).
- [ ] nginx: dominios y vencimiento de certificados.
- [ ] Crons del sistema.
- [ ] Clasificar cada instancia en su proyecto (cliente).

### Cero acciones sobre lo importado
- [ ] En entornos, servidores e instancias importados se ve el banner
      **IMPORTADO — SOLO LECTURA** y **ningún** botón que modifique (solo
      «Capturar gemelo»).
- [ ] Bitácora: cualquier intento queda como «Bloqueo de solo lectura».
- [ ] El grupo «PCM: operaciones destructivas» sigue **vacío**.

## F4 · Base de Barraca Julia (CC en la Mac de Daryl; Daryl sube el dump)

**La base:** `entrega_v19_zip/bj_v19_neutralizada_2026-09-14.zip` (en
`~/dumps/barracajulia/`). Es la última completa: sale de `bj_v19`, la cabeza de
la cadena cerrada el 11-sep (ensayo `20260910-050727`), con los datos del dump
de origen cargado el 7-sep. Zip de 11,2 GB = `dump.sql` de 12,8 GB +
filestore de 9,7 GB (261.891 archivos). Restaurada ocupa ~19 GB de base (la
`bj_v19` local mide 43 GB por bloat: nunca pasó por `VACUUM FULL`). 115
módulos, todos Community; ninguno de LocalizacionUy.

> ⚠ Esa base **no está validada contablemente**: `05_cuadre` falló el 11-sep
> (33 diferencias en caja y bancos 2022–2024). Sirve para el uso real de PCM,
> no para declarar la migración terminada.

1. [ ] **Daryl:** OK de que es esa base.
2. [ ] **CC:** generar el artefacto neutralizado (Mac; ~25 min; necesita ~36
   GB libres; hoy hay ~51):

   ```bash
   cd ~/dumps/barracajulia
   /Users/darylyturraldelopez/Odoo/clients/19.0/Primate-Cloude-Manager/.venv/bin/python \
     _herramientas/pcm_artefacto_neutralizado.py \
     entrega_v19_zip/bj_v19_neutralizada_2026-09-14.zip pcm_produccion --etiqueta bj_v19
   ```

   Carga el zip en una base temporal, saca credenciales, aplica la **misma**
   neutralización que PCM y corre el checklist de SELECTs (SMTP apagado y sin
   credenciales, crons apagados, `web.base.url` fuera de producción, websites
   sin dominio, cola de correo vacía, Sibra/Google/tokens/contraseñas). **Sin
   todo en verde no arma el zip.** El `administrator` conserva la contraseña
   de la entrega (llavero: `bj-v19-zip-admin`); las demás quedan invalidadas.
   Sale `pcm_produccion/bj_v19_neutralizada_pcm_<fecha>.zip` + `SHA256SUMS.txt`.
3. [ ] **Daryl, consola S3:** bucket **`pcm-dumps-<ACCOUNT_ID>`** (tiene que
   existir antes de subir; el nombre `pcm-*` es el que habilitan las policies):
   - región **us-east-1** (la misma del servidor: bajada rápida y sin costo
     de transferencia entre regiones);
   - **Block all public access**: ON (default) · Object Ownership: *Bucket
     owner enforced* (default);
   - cifrado por defecto **SSE-S3** (AES-256). No SSE-KMS: el rol de la EC2
     necesitaría además `kms:Decrypt`;
   - **versionado apagado** (si no, el borrado que hace PCM deja la versión
     vieja del dump guardada);
   - lifecycle: regla «incoming» con prefijo `incoming/` → **expirar a los 7
     días** + **borrar multipart incompletos a 1 día**.
4. [ ] **Daryl:** subir (desde la Mac, con tu perfil de admin de la cuenta real):

   ```bash
   F=bj_v19_neutralizada_pcm_<fecha>.zip
   aws s3 cp ~/dumps/barracajulia/pcm_produccion/$F \
     s3://pcm-dumps-<ACCOUNT_ID>/incoming/barraca-julia/$F \
     --region us-east-1 --sse AES256 --checksum-algorithm SHA256 \
     --profile <tu-perfil-admin-real>
   ```

   11,2 GB: a 50 Mbit/s de subida son ~30 min; a 200 Mbit/s, ~8.

## F5 · Montaje en us-east-1 (PCM, con «Prod — aprovisionamiento»)

1. [ ] **Proyecto** «Barraca Julia» (cliente Barraca Julia) con la cuenta
   «Prod — aprovisionamiento».
2. [ ] **Nuevo entorno** `barraca-julia` · proyecto Barraca Julia · **Tipo:
   Staging**. Staging porque es una copia neutralizada: PCM neutraliza
   todo lo que no es producción, y un restore sobre un staging no pide la
   triple confirmación (la base recién creada se pisa sin ceremonia).
3. [ ] **Aprovisionar**: región us-east-1 · **Verificar región** (security
   group, subred y perfil `pcm-ssm-role`) · dominio provisorio
   `barraca-julia.pcm.invalid` · contraseña admin (master del odoo.conf) ·
   nombre `barraca-julia` · **t3.xlarge** · disco **200** · Ubuntu 24.04 ·
   PostgreSQL local, base `barraca_julia` · **sin** Route 53 · **sin**
   «Emitir HTTPS» (todavía no hay IP). El volumen sale **gp3** (la AMI de
   Canonical es la de `ebs-gp3`).
4. [ ] Con la IP: instancia → **URL principal** =
   `barraca-julia.<ip-con-guiones>.sslip.io` → PCM reescribe el vhost y
   emite HTTPS (Let's Encrypt producción). Esperar **«HTTPS activo»**.
   ⚠ Sin Elastic IP: **no detener el servidor** (al arrancar cambia la IP y
   sslip.io deja de apuntar).
5. [ ] **Restaurar desde un dump…** (panel lateral): origen «Key S3
   manual» = `s3://pcm-dumps-<ACCOUNT_ID>/incoming/barraca-julia/<archivo>` ·
   formato: detectar · **Eliminar el dump de S3 al finalizar: sí** · base
   destino `barraca_julia` · **desmarcar «Respaldar el destino antes»** (viene
   marcado; la base es la vacía del install y no hay política de backup) →
   Restaurar → Ok. No hay casilla de neutralizar: PCM neutraliza solo porque
   la instancia es Staging. Neutraliza **antes** de arrancar Odoo y corre el
   checklist de SELECTs **en el servidor**; si algo da rojo o falla la carga,
   Odoo queda **detenido** y el restore figura fallido/parcial con la cola del
   log (pasó en el ensayo: ver «Lo que enseñó el ensayo»).
6. [ ] Checks post-montaje:
   - [ ] Chatter del entorno: «Checklist de SELECTs en verde» con
         `modules_installed=115`, `smtp_active=0`, `smtp_with_credentials=0`,
         `crons_active=0`, `mail_pending=0`, `website_with_domain=0`,
         `web_base_url=https://barraca-julia.<ip>.sslip.io`.
   - [ ] Adjuntos: `PCM_FILESTORE_FILES` ≈ 261.891 y `attachments_stored`
         ≈ 264.000; abrir un PDF de una factura en el Odoo.
   - [ ] Login web en `https://barraca-julia.<ip>.sslip.io` con
         `administrator` (contraseña del llavero `bj-v19-zip-admin`).
   - [ ] HTTPS activo (candado + badge en PCM) y http redirige a https.
   - [ ] En el Odoo (`?debug=1`): **Aplicaciones** → quitar el filtro
         «Aplicaciones» y elegir «Instalado» → **115**. **Acciones
         planificadas** → quitar el filtro «Todos» (incluye las archivadas) →
         **ninguna**. **Servidores de correo saliente** → ninguno activo; con
         el filtro «Archivado» aparece el de origen **sin usuario ni
         contraseña**. **Parámetros del sistema** → `web.base.url` = la URL
         sslip.io.
7. [ ] LocalizacionUy: la base **no usa ningún módulo** de LocalizacionUy (0
   instalados). Si se quiere el repo disponible, «Clonar y registrar»
   `primateuy/LocalizacionUy` rama **`19.0_staging`** (la única de v19) con un
   token de GitHub que carga Daryl. No hace falta para que la base abra.

### Tiempos medidos en el ensayo (sandbox, 23-oct) y proyección
Ensayo con la base **reducida** (zip 0,36 GB, `dump.sql` 1,25 GB, 2.470
archivos), en **m7i-flex.large** (el plan gratuito rechaza t3.xlarge),
us-east-1, gp3 200 GB. Video: `pcm_f5_sandbox_barraca_julia.mp4` en la KB.

| Paso | Medido |
|---|---|
| Artefacto neutralizado en la Mac (reducida) | 1,5 min |
| EC2 creada + bootstrap | ~15 s después del pedido |
| Instalación de Odoo | 2 min 36 s |
| Dominio → sslip.io + HTTPS (Let's Encrypt producción) | 30 s |
| Restore (bajar, descomprimir, cargar, filestore, neutralizar, checklist) | 2 min 30 s |
| Terminar (3 pasos) | inmediato |

Proyección para la **completa** (×10 de SQL, ×100 de archivos) en t3.xlarge:
artefacto en la Mac ~20–25 min · subida al bucket según tu conexión ·
restore **~30–45 min** (la carga del SQL y los 262 mil archivos mandan). El
comando SSM ahora pide 4 h al agente (antes AWS lo mataba a la hora).

### Espacio en disco (200 GB)
Pico ≈ zip 11,2 + descomprimido 22,5 + base ~19 (+ WAL) + filestore 9,7 ≈
**65 GB** + sistema ~8 GB. El restore chequea el espacio para bajar y
descomprimir antes de tocar nada.

### Lo que enseñó el ensayo (ya corregido)
- El plan gratuito de la sandbox rechaza t3.xlarge: PCM informa el error de
  AWS y no queda nada creado; «Aprovisionar» se puede repetir.
- **Restore fallido por permisos**: el zip del generador traía los archivos
  en modo 000 → `dump.sql: Permission denied` DESPUÉS del drop. Odoo quedó
  **detenido** a propósito (base sin neutralizar). Arreglos: el generador
  escribe 644 y PCM normaliza permisos al descomprimir
  (`chmod -R u+rwX,go+rX`) — un zip raro de cualquier origen ya no rompe.
  Reintento: OK.
- El error del restore ahora muestra la cola del log (antes decía sólo
  «exit status 1»).

### Plan B si SSL falla
La instancia queda **activa en HTTP** (nunca rota): badge «Sin HTTPS» con el
motivo. Corregir la causa (IP cambiada, puerto 443) y **«Activar HTTPS
ahora»**. El reintento automático corre cada 10 min (solo gasta intento de
certbot con la propagación confirmada; tope 6 intentos y 24 h esperando).

### Rollback limpio de lo nuevo (sin tocar nada importado)
- Falló el install con la EC2 creada → «Reintentar instalación» (reusa la
  EC2, no crea otra).
- Falló el restore → el chatter dice en qué paso. Antes del drop la base
  vacía sigue; después, se repite el restore (el dump sigue en S3: solo se
  borra si salió bien).
- Hay que deshacer todo → con el grupo destructivo asignado a vos: terminar
  el servidor nuevo (triple confirmación: impacto → nombre exacto → tu
  contraseña/2FA). IAM impide igual tocar lo importado: `pcm-prod-prov` solo
  opera recursos con `primate:managed_by=pcm`.
- Al terminar, quitarte el grupo destructivo.

## F6 · Cierre y aviso a INCO

- [ ] Bitácora de PCM revisada: ningún «Bloqueo de solo lectura» inesperado.
- [ ] Grupo destructivo vacío.
- [ ] Video del recorrido completo (credenciales y datos de clientes blureados).
- [ ] KB actualizada (estado + decisiones).
- [ ] **Aviso a INCO**: servidor `barraca-julia` montado en us-east-1
      (t3.xlarge, gp3 200 GB, id de instancia e IP) para que configuren
      **backups** (snapshots del EBS), **DR** y **alarmas** (CPU, disco,
      estado del sistema). PCM no tiene política de backup en este entorno.
- [ ] Recordatorio: revocar la access key de `cc-lab-domain`.
