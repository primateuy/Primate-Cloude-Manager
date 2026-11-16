# Guía del día D — Mudanza de PCM Producción a su propio servidor (`pcm-self`)

Plan y análisis: [`MUDANZA_PCM_SELF.md`](../MUDANZA_PCM_SELF.md). Formato y
reglas: las mismas del runbook de bj-uat
([`docs/CHECKLIST_MUDANZA_PROD.md`](CHECKLIST_MUDANZA_PROD.md)).

**Quién hace cada paso:**
- **YO** = Daryl, a mano (consola de AWS, tu Mac, el navegador).
- **CC** = Claude Code: prepara, guía y verifica en tu Mac. Nunca toca la cuenta real.
- **PCM** = lo hace PCM cuando apretás el botón.

## ⛔ Regla del día (para Claude, sin excepción)

Claude **no ejecuta ningún comando directo contra la cuenta real ni contra el
servidor nuevo** (ni `aws` cli, ni ssh, ni comandos por SSM). Todo pasa por
PCM con tus manos. Las credenciales (keys IAM, PAT, maestra, contraseñas) las
cargás **vos**: Claude no las pide, no las lee, no las ve. Si algo requiere
salirse de eso, Claude **frena y pregunta**.

**Resumen del día:** el servidor nuevo se monta ANTES de la ventana de corte
(con la base todavía en la Mac). La ventana real de congelamiento es
**F1→F4: ~1 h – 1 h 30** (estimado con los tiempos medidos abajo), y hasta F5
**todo es reversible** sin pérdida.

---

## HOY, ANTES DE FIJAR EL DÍA (2026-11-13) — YO

1. [ ] YO · **Update del stack `pcm-prerequisitos`** en la cuenta real
   (CloudFormation → Update → Replace template → subir
   `infra/cloudformation/pcm-prerequisitos.yaml`), parámetros: `Sufijo`
   **vacío**, `ZonaDns=Z06178081BO0Q1OK7MTAO` (zona
   **primatecloudmanager.com**, 2026-11-16),
   `NombresDns=primatecloudmanager.com,*.primatecloudmanager.com`.
   Change set: **un único Modify de
   `RolSSM`** (suma `CloudWatchAgentServerPolicy`). Acá el Sufijo vacío pega
   en `pcm-ssm-role`, el rol que usan los montajes (la trampa del `-cfn` era
   solo de la sandbox).
2. [ ] YO · **Activar el monitoreo en bj-uat**: PCM → Entornos → bj-uat →
   «Monitoreo CloudWatch» → **Activar** → Activo en ~1 min → CloudWatch →
   Metrics → **CWAgent**: `mem_used_percent` + `disk_used_percent` de
   `i-0617ee15ddadb0a71` a los ~2 min. Detalle:
   [`docs/MONITOREO_CLOUDWATCH.md`](MONITOREO_CLOUDWATCH.md).
3. [ ] YO · Actualizar el módulo y precargar el formulario (CC lo dejó listo,
   el clasificador le bloquea —bien— tocar la base productiva):

   ```bash
   ~/Odoo/clients/19.0/pcm_produccion/levantar.sh -u primate_cloud_manager --stop-after-init --no-http
   ~/Odoo/clients/19.0/pcm_produccion/levantar.sh shell --no-http \
       < ~/Odoo/clients/19.0/Primate-Cloude-Manager/infra/precargar_pcm_self.py
   ```

   La primera línea sube la base a **19.0.1.8.0** (CC ya hizo el respaldo
   previo: `~/dumps/pcm_produccion/pcm_produccion_pre_u180_2026-11-13.dump`).
   La segunda termina en «LISTO: entorno pcm-self en Borrador…». Pegale la
   salida a CC.

   > Prompt para CC: «corrí el -u y la precarga, esta es la salida: …»

---

## PRERREQUISITOS MÍOS (días antes del corte) — YO

- [ ] **PAT de GitHub** (para el clone del montaje, un solo uso):
  GitHub → Settings → Developer settings → **Fine-grained tokens** →
  Generate new token:
  - Resource owner: **primateuy** · Expiration: **7 days**.
  - Repository access: **Only select repositories** → `Primate-Cloude-Manager`.
  - Permissions → Repository permissions → **Contents: Read-only**
    (Metadata read-only se marca sola). Nada más.
  - Guardalo en **`~/.pcm_produccion/pat_mudanza_pcm.txt`** con `chmod 600`
    (misma convención que el resto de `~/.pcm_produccion/`). De ahí lo pegás
    en PCM → Repositorios → `primate_cloud_manager` → **Token** (queda
    cifrado y enmascarado). **Se revoca en GitHub apenas el clone termine.**
- [ ] **2FA activo** en tu admin de `pcm_produccion` **antes del corte**
  (Preferencias → Seguridad → A2F). El TOTP viaja dentro del dump: en F4 lo
  verificás en el dominio nuevo ANTES de cargar ningún secreto.
- [ ] **Contraseña maestra nueva** (master password del `odoo.conf` del
  servidor nuevo): generala y guardala en el llavero. Va en el campo
  «contraseña admin» del wizard al aprovisionar. No es la del login.
- [ ] **Access keys NUEVAS** para `pcm-prod-ro` y `pcm-prod-prov`
  (IAM → Users → Security credentials → Create access key → «Application
  running outside AWS»). Guardalas SIN cargarlas en ningún lado todavía: se
  cargan recién en F4 **en el PCM nuevo**, y las viejas (las que conoce la
  Mac) se **desactivan** en F5 — ese es el fencing: aunque la Mac arrancara
  por error, sus keys ya no sirven. (Si un usuario ya tiene 2 keys, borrá
  antes la que sobre: IAM admite máximo 2 por usuario.)
- [ ] **Bucket `pcm-backups-512060111009`** (lo creás vos en la consola; PCM
  no crea buckets, decisión fija). Configuración exacta:
  - Región **us-east-1** · Object Ownership: **ACLs disabled (bucket owner
    enforced)** · **Block *all* public access: ON** (las 4 casillas).
  - Versioning **desactivado** · Cifrado **SSE-S3 (AES-256)**, bucket key default.
  - Lifecycle rule `pcm-retencion-30d` (todo el bucket): **Expire current
    versions: 30 días** + **Delete incomplete multipart uploads: 7 días**
    (el 30 coincide con `expected_retention_days` de la política en PCM).
  - Permisos: ya alcanzan (el rol del servidor tiene `s3:Put/GetObject` sobre
    `pcm-*/*`; el usuario, lifecycle sobre `pcm-*`). No hay que tocar IAM.
- [ ] Verificá que `subir_dump.sh` sigue apuntando al perfil
  `pcm-prod-provision` (`aws sts get-caller-identity --profile
  pcm-prod-provision` → `user/pcm-prod-prov`).

---

## Orden del día del corte

| # | Tramo | Quién | Detalle |
|---|---|---|---|
| P1 | Montaje de `pcm-self` (puede ser el día antes) | PCM · YO | [§P1](#p1--montaje-de-pcm-self-pcm--yo) |
| P2 | Dominio **`primatecloudmanager.com`** (apex) + clone del repo + revocar PAT | PCM · YO | [§P2](#p2--dominio--repo--revocar-pat) |
| F1 | Congelar la Mac (abre la ventana) | YO | [§F1](#f1--congelar-la-mac-yo--abre-la-ventana) |
| F2 | Dump espejo + subida | YO · CC | [§F2](#f2--dump-espejo--subida-yo) |
| F3 | Restore sin neutralizar (disparado desde el PCM de la Mac) | PCM | [§F3](#f3--restore-pcm-desde-la-mac) |
| F4 | Despertar y verificar el PCM nuevo (checklist en verde) | YO · PCM | [§F4](#f4--despertar-y-verificar-yo--pcm) |
| F5 | Fencing de keys + apagado de la Mac (cierra la ventana) | YO | [§F5](#f5--fencing-y-apagado-de-la-mac-yo) |
| F6 | Día después | YO · CC | [§F6](#f6--día-después) |

### Tiempos medidos (ensayo sandbox 2026-11-10, toma 12) y proyección

| Paso | Ensayo (medido) | Día D (proyección) |
|---|---|---|
| Montaje: Aprovisionar → activo con HTTPS | **4 min 50 s** | 5–8 min (t3.medium) |
| Verificación del perfil PCM por SSM | 1 min 40 s | igual |
| Dump espejo (copia + desinstalar + dump) | 30 s (base de demo) | **5–15 min** (los 13 Enterprise sobre 51 MB) |
| Subida a S3 + verificación | 8 min (manual) | **~2 min** (zip ≈ 30–60 MB) |
| Restore sin neutralizar | 34 s (reducida) | **2–5 min** (51 MB + filestore 11 MB) |
| Despertar: parámetros + crons + checks | ~1 min | 5–10 min |
| Recarga de credenciales (Daryl) | 25 min (keys creadas en el momento) | **~10 min** (keys ya creadas en F0) |
| **Ventana F1→F4 (PCM congelado)** | — | **~1 h – 1 h 30** |

Durante la ventana: PCM Mac queda **vivo pero sin crons** (hace falta para
disparar el restore); bj-uat sigue operando solo (solo pierde el snapshot de
métricas de PCM durante la ventana; el agente CloudWatch del servidor sigue
publicando solo).

---

## P1 · Montaje de `pcm-self` (PCM · YO)

1. [ ] PCM · Entornos → **`pcm-self`** (Production, Borrador) →
   **«Aprovisionar»**. El formulario viene **precargado**: cuenta
   **pcm-prod-prov** · us-east-1 · `pcm` · **t3.medium** · disco **50** (gp3)
   · **Ubuntu 24.04** · PostgreSQL local 16, base **`pcm_produccion`** ·
   **IP fija** · **«Dominio sslip.io automático»** · **«Emitir HTTPS al
   terminar»** · Route 53 apagado · **«Perfil PCM (hospeda un PCM)»** ·
   **«Monitoreo CloudWatch»**. Falta solo:
   - **«Verificar región»** → verde;
   - **contraseña maestra** (la nueva del llavero);
   - **Aprovisionar**.
2. [ ] PCM · Chatter, en orden: IP fija asociada → dominio provisorio
   `pcm.<ip>.sslip.io` → «Entorno aprovisionado y activo» → **«HTTPS
   activo»** → **«Monitoreo CloudWatch: activar … Exitoso»** (el agente
   queda **Activo** en la ficha: nace monitoreando).
3. [ ] YO · CloudWatch → CWAgent: métricas del server nuevo a los ~2 min.
   Si el agente da Error: el motivo queda en la ficha; casi seguro falta el
   update del stack de «HOY, ANTES».

   > Prompt para CC: «pcm-self activo, te pego el chatter: …» (CC verifica
   > la secuencia y el perfil contra los goldens del ensayo).

**Rollback P1:** falló el install con la EC2 creada → «Reintentar
instalación». Hay que deshacer todo → grupo destructivo + «Terminar
servidor» (triple confirmación); la IP fija se libera sola. Nada de la Mac
se tocó: **rollback total, costo = minutos de EC2**.

## P2 · Dominio + repo + revocar PAT

1. [ ] PCM · Instancia `pcm` → **«Crear DNS ahora»** → propone
   `pcm.primatecloudmanager.com` (slug + sufijo default) y se **reemplaza
   por el APEX `primatecloudmanager.com`** (el manager vive en el dominio
   raíz; registro **A en el apex** con la IP fija — la validación del wizard
   acepta el apex desde el patch del 2026-11-16) → en ~70 s:
   registro A en la zona, certificado de producción, sslip retirado,
   `web.base.url` actualizado (igual que bj-uat el 28-oct).
2. [ ] YO · Pegá el PAT (de `~/.pcm_produccion/pat_mudanza_pcm.txt`) en
   Repositorios → `primate_cloud_manager` → **Token** → Guardar.
3. [ ] PCM · Desplegar el repo en la instancia `pcm` (clone por SSM con
   GIT_ASKPASS; el token nunca va en la URL ni en argv). queue_job ya lo
   dejó el perfil en el montaje (symlink en el addons dir).
4. [ ] YO · **Revocar el PAT en GitHub ya mismo** (aunque le queden días) y
   borrar `~/.pcm_produccion/pat_mudanza_pcm.txt`. Las actualizaciones
   futuras de código van por la deploy key SSH del BACKLOG.
5. [ ] YO · Login en `https://primatecloudmanager.com/web/login`: candado
   válido; `/web/database/manager` → **404**. (La base todavía es la vacía
   del install: el login real llega con el restore.)

   > Prompt para CC: «dominio y repo listos, PAT revocado».

**Rollback P2:** si el certificado del dominio falla, PCM desarma lo nuevo y
la instancia **sigue en sslip.io con HTTPS** (ficha: «No se pudo pasar a…»,
Reintentar/Descartar). Reversible siempre.

## F1 · Congelar la Mac (YO) — abre la ventana

1. [ ] YO · Avisar/cerrar sesiones; no se toca más el PCM de la Mac salvo lo
   que sigue.
2. [ ] YO · PCM Mac → Ajustes técnicos → Acciones planificadas → desactivar
   los **8 crons «Primate Cloud:»** (anotá cuáles — hoy la base tiene 26/27
   crons activos en total; los no-PCM quedan como están, viajan en el dump).
3. [ ] YO · Esperar la cola de queue_job en 0 (pending/started).
4. [ ] CC · Verifica en la Mac (lectura): crons PCM off, cola queue_job
   vacía, `mail_mail` sin sorpresas (hoy: `exception=2`, 0 SMTP activos —
   nada que pueda soltarse).

   > Prompt para CC: «F1 hecho, verificá la Mac».

**Rollback F1:** reactivar los 8 crons. Cero impacto.

## F2 · Dump espejo + subida (YO)

Espejo del ensayo: la base real **no se toca**; todo sale de una copia.

1. [ ] YO · Copia + desinstalación de los 13 Enterprise EN LA COPIA + dump
   (CC te deja los comandos exactos listos para pegar ese día; a grandes
   rasgos):

   ```bash
   createdb -U odoo -T pcm_produccion pcm_mudanza          # copia (la real queda quieta)
   # desinstalar EN pcm_mudanza, dos pasadas (cascadea ai_fields/ai_server_actions):
   #   1ª: web_enterprise web_mobile mail_mobile mail_enterprise web_cohort
   #       web_gantt web_grid web_map ai iap_extract
   #   2ª: ai_auto_install
   # → quedan 30 módulos, 0 OEEL (verificación por SQL)
   pg_dump -U odoo -Fc pcm_mudanza > pcm_self_<fecha>.dump
   # + tar del filestore (11 MB) + zip + sha256
   ```

   El bundle **no** está instalado en `pcm_produccion` (verificado
   2026-11-13): solo los 13 Enterprise.
2. [ ] YO · `infra/subir_dump.sh <zip>` → verifica sha256, sube a
   `s3://pcm-dumps-<cuenta>/incoming/…`, termina en **«LISTO»** → **copiá la
   ruta** (va en F3). Tamaños reales: base 51 MB + filestore 11 MB → zip
   ≈ 30–60 MB, subida ~2 min.
3. [ ] CC · Verificación del dump en la Mac antes de subir: checklist de
   SELECTs sobre `pcm_mudanza` (0 OEEL, módulos=30, crons PCM presentes
   aunque desactivados, 0 SMTP).

   > Prompt para CC: «copia hecha, verificá pcm_mudanza antes del dump» y
   > después «subido, esta es la ruta S3: …».

**Rollback F2:** `dropdb pcm_mudanza` y reactivar crons. La base real nunca
se tocó.

## F3 · Restore (PCM, desde la Mac)

1. [ ] PCM (Mac) · Entorno `pcm-self` → **«Restaurar desde un dump…»**: Key
   S3 manual = la ruta de F2 · base destino **`pcm_produccion`** · instancia
   destino la de `pcm-self` (**Production → SIN neutralizar**, R4-B5:
   crons, 2FA y parámetros viajan intactos) · **borrar el dump de S3 al
   finalizar: sí** · **desmarcar «Respaldar el destino antes»** (la base
   destino es la vacía del install).
2. [ ] PCM · Job en verde + bitácora; el script arranca la unit al final.
   Si se corta: el job queda fallido SIN relanzarse solo, candado
   `/var/lock/pcm-restore-<base>.lock` en el servidor; mirar el servidor por
   PCM antes de «Reintentar» (lección bj-uat).

   > Prompt para CC: «restore en verde, te pego el chatter».

**Rollback F3:** el restore falló → el chatter dice el paso; se repite (el
dump sigue en S3 si falló). Peor caso: terminar `pcm-self`, volver a P1
mañana. La Mac sigue intacta y puede reactivar crons en 1 min.

## F4 · Despertar y verificar (YO · PCM) — checklist ANTES de apagar nada

El orden importa: **2FA primero, secretos después**.

1. [ ] YO · Login en `https://primatecloudmanager.com` con tu admin → **el 2FA
   pide código** (viajó en el dump). Sin 2FA verde NO se carga ninguna key.
2. [ ] YO · Parámetros: `web.base.url = https://primatecloudmanager.com` +
   `web.base.url.freeze = True`; `report.url = http://127.0.0.1:<puerto>`
   (lección wkhtmltopdf). Confirmar que NO existe
   `primate_cloud_manager.fernet_key` en parámetros (la clave vive en
   `/etc/odoo/pcm.env` del servidor, la dejó el perfil).
3. [ ] YO · Cargar las keys IAM **NUEVAS** en «pcm-prod-ro» y
   «pcm-prod-prov» + **«Validar conexión»** (ARN esperado en verde, 12
   dígitos). El repo queda SIN token (PAT ya revocado).
4. [ ] YO · Marcar **`is_self`** en la instancia `pcm` → botones
   destructivos desaparecen; probar un stop → bloqueado («hospeda a ESTE
   PCM») y en bitácora.
5. [ ] Checklist final (todo verde antes de F5):
   - [ ] `web.base.url` = dominio nuevo y congelado; `report.url` local.
   - [ ] Crons PCM **activos en el nuevo** (los 8); un job de prueba
         procesado (jobrunner de queue_job vivo).
   - [ ] Sync AWS manual OK: servidores listados, **bj-uat visible y
         operable** (abrir su ficha, ver métricas/estado).
   - [ ] Costos: «Actualizar costos» trae datos.
   - [ ] Backup manual de `pcm_produccion` al bucket
         `pcm-backups-512060111009` OK (objeto en S3) y **política diaria
         asignada** (retención 30).
   - [ ] **Agente CloudWatch Activo** en la ficha de `pcm-self` y métricas
         en CWAgent.
   - [ ] `/web/database/manager` → **404**; `/web/database/selector` ídem.
   - [ ] 2FA pidiendo código (ya verificado en 1).
   - [ ] La instancia «self» bloqueada (ya verificado en 4).
   - [ ] 0 SMTP activos (hasta configurar el real); cola `mail_mail` sin
         cambios (exception=2 esperado).

   > Prompt para CC: «F4: te pego el checklist con lo que me dio cada
   > punto» (CC cruza contra esta lista y canta falta/OK).

**Rollback F4 (si algo no está en verde):** NO ejecutar F5. Reactivar los 8
crons en la Mac → la Mac sigue siendo el PCM productivo (sus keys siguen
activas, nada se desactivó). El server nuevo queda congelado para
diagnóstico (detener su unit por SSM/consola si hace falta: el botón de PCM
está bloqueado por autogestión, y está bien que lo esté). Se reintenta otro
día: **hasta acá no ocurrió nada destructivo**.

## F5 · Fencing y apagado de la Mac (YO) — cierra la ventana

1. [ ] YO · IAM → **desactivar** (no borrar) las access keys viejas de
   `pcm-prod-ro` y `pcm-prod-prov` (las que conocía la Mac). Desde acá la
   Mac es inofensiva aunque arranque.
2. [ ] YO · Apagar el Odoo de la Mac (`pgrep -f pcm_produccion` antes y
   después — regla no-relanzar-sin-pgrep).
3. [ ] YO · Doble cerrojo anti-arranque:
   `mv levantar.sh levantar.sh.MUDADO-<fecha>` (queda un stub que imprime
   «MUDADO a primatecloudmanager.com») y
   `mv pcm_produccion.conf pcm_produccion.conf.MUDADO`.
4. [ ] YO · Archivar `~/.pcm_produccion/fernet.key` offline (se destruye al
   cerrar los 30 días). La base local queda como respaldo frío 30 días.

**Rollback F5 (post-corte, primeras horas/días):** todavía posible a mano —
reactivar las keys viejas en IAM, deshacer los `mv`, levantar la Mac,
apagar la unit del server nuevo. Se pierde solo lo operado en el PCM nuevo
desde F3 (bitácora/jobs nuevos). **Punto de NO retorno real:** borrar las
keys viejas + destruir `fernet.key` + borrar la base local (recién a los 30
días, F6+30).

## F6 · Día después

- [ ] YO · El backup diario automático llegó al bucket.
- [ ] YO · Cert vigente, `web.base.url` estable, crons corriendo, CWAgent
      publicando.
- [ ] YO · Verificar que el dump se borró de `pcm-dumps-*` (el restore lo
      borra si salió bien).
- [ ] YO · **Aviso a INCO** (texto abajo).
- [ ] CC · KB actualizada (estado + decisiones) + marcar esta guía.
- [ ] Pendiente post-corte: **mover bj-uat a
      `barracajuliav19.primatecloudmanager.com`** («Asociar dominio» o
      «Crear DNS ahora» en la zona nueva). Sigue en
      `barracajuliav19.erp.primateuy.com`, pero **PCM ya no puede escribir en
      esa zona** (el stack 2026-11-16 solo otorga `primatecloudmanager.com` y
      `*.primatecloudmanager.com`).

---

## Mensaje para INCO (completar ids el día D)

> Asunto: Nuevo servidor PCM (consola de gestión) — backups y alarmas
>
> Hola. Mudamos nuestra consola de gestión (PCM) a un servidor propio en la
> cuenta: **us-east-1, t3.medium, gp3 50 GB, Ubuntu 24.04**, instancia
> **`<i-xxxxxxxxxxxxxxxxx>`**, IP fija **`<x.x.x.x>`**,
> `https://primatecloudmanager.com`. No es productivo de clientes, pero sí
> crítico para nuestra operación.
>
> Les pedimos configurarle, como a bj-uat: **backups** (snapshots del EBS),
> **DR** y **alarmas** (CPU, disco, status check). **RAM y disco ya los
> publica el agente de CloudWatch** (namespace `CWAgent`,
> `mem_used_percent` / `disk_used_percent`, dimensión `InstanceId`, cada
> 60 s, con la config de ustedes): pueden enganchar sus alarmas directo.
>
> Aparte, PCM hace su backup lógico diario de la base a
> `s3://pcm-backups-512060111009` (retención 30 días) — es complementario,
> no reemplaza los snapshots.

---

## Preparado por CC (2026-11-13)

- [x] Respaldo de `pcm_produccion` previo al `-u`:
      `~/dumps/pcm_produccion/pcm_produccion_pre_u180_2026-11-13.dump` (3,2 MB, `-Fc`).
- [x] `infra/precargar_pcm_self.py` (idempotente; lo corre YO, ver «HOY»).
- [x] Esta guía. Números reales medidos hoy: base 51 MB, filestore 11 MB,
      13 Enterprise instalados (sin bundle), 26/27 crons activos, cola
      `exception=2`, 0 SMTP.
- [ ] Pendiente CC (cuando fijes el día): comandos exactos de F2 listos
      para pegar (desinstalación en la copia + dump + zip + sha256).
