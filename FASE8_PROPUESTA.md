# Propuesta de diseño — Fase 8: Respaldos + Staging robusto

> Estado: **APROBADA (2026-07-01) con ajustes**: DNS fuera de la fase (pasa a Fase 8.5,
> mini-fase inmediata post-8); restore a producción exige además tipear el nombre del
> entorno destino; el test de tags con literales entra al Bloque 1; freno adicional antes
> del Bloque 4 (revisar diseño del wizard de restore). `ARCHITECTURE.md` ya actualizado.
> Fuentes: spec funcional §10 y §14, spec técnica §5.9 y §7.2–7.3, `ARCHITECTURE.md`,
> código actual de staging (`environment.py`), `neutralization.sql`, servicios `aws_*`.

## 0. Deltas y contradicciones detectadas (leer primero)

1. **`ARCHITECTURE.md` define Fase 8 como "DNS y respaldos"**, no "respaldos + staging
   robusto". El CRUD de `dns.record` contra Route53 NO existe hoy (el modelo no tiene
   acciones; `aws_route53` solo tiene `list_zones/list_records/create_record`, sin
   update/delete). **Resuelto: pasa a Fase 8.5** (mini-fase independiente inmediatamente
   después de cerrar la 8). No va al backlog. `ARCHITECTURE.md` actualizado.
2. **La spec (§10) define respaldos SOLO como comparación esperada vs detectada.** La
   ejecución de backups por PCM (pg_dump + filestore vía SSM → S3) es un **delta nuevo**,
   pero necesario: en entornos con PostgreSQL local (la mayoría de los que crea el módulo)
   AWS no tiene nada que detectar — sin ejecutor, la política siempre daría "Sin respaldo".
3. **La nota del backlog sobre el SQL de neutralización estaba desactualizada:** el filtro
   frágil de crons y el `mail.catchall.alias` ya se corrigieron en Fase 7 (delta D5,
   documentado en el header de `data/neutralization.sql`). Lo pendiente REAL contra la spec
   §14.3 es: invalidar tokens externos, y dos huecos detectados ahora (cola `mail_mail`
   pendiente de prod, dominio de `website`). Ver §2.3.
4. **Estados intermedios del staging (spec §14.2):** el código persiste solo
   `provisioning/active/error` y muestra los pasos por bus + log. Divergencia ya aceptada en
   Fase 7; no propongo cambiarla.
5. **`operation.log`:** el refresh de staging hoy loguea `action_type='staging_create'`.
   Agregar tipos nuevos (ver §3.8).

---

## 1. Respaldos

### 1.1 Modelos

**`primate.cloud.backup.policy`** (catálogo, spec §5.9 + delta de ejecución):

| Campo | Tipo | Origen |
|---|---|---|
| `name` | Char | spec |
| `policy_type` | Selection none/basic/standard/critical/custom | spec |
| `expected_frequency` | Selection daily/twice_daily/hourly/manual | spec |
| `expected_retention_days` | Integer | spec |
| `description` | Text | spec |
| `managed_by_pcm` | Boolean — PCM ejecuta el backup (además de validar) | **delta** |
| `s3_bucket` / `s3_prefix` | Char — destino de los dumps gestionados | **delta** |
| `execution_hour` | Float — hora UTC de la ventana de ejecución | **delta** |

Data XML `noupdate=1` con las 4 políticas estándar de la spec §10.1 (Sin respaldo, Básica
7d, Estándar 30d, Crítica 2×/90d).

**`primate.cloud.backup`** (registro de backups efectivos — **modelo nuevo, delta**, no está
en la lista de la spec): `environment_id`, `database_id`, `backup_type`
(rds_automated/rds_snapshot/pcm_dump), `backup_date`, `s3_key` (dump), `s3_filestore_key`,
`size_mb`, `state` (in_progress/completed/failed/expired), `source` (detected/executed),
`error_message`, `expiry_date` (backup_date + retención de la política). Inmutable estilo
log (sin edición manual); `ondelete` restrict a environment.

**`primate.cloud.environment`** suma: `backup_policy_id` (spec), y (delta) el resultado del
validador: `backup_compliance` Selection (`ok` Cumple / `non_compliant` No cumple /
`unverifiable` No verificable / `no_policy` Sin política — §10.2), `backup_compliance_detail`
(Text) y `last_backup_check` (Datetime).

### 1.2 Ejecución (backups gestionados)

- **Cron programador** (`_cron_run_managed_backups`, cada hora): busca entornos activos con
  política `managed_by_pcm` cuya ventana venció (daily/twice_daily/hourly según
  `execution_hour` y el último `primate.cloud.backup` exitoso) y encola
  `environment.with_delay().job_run_backup()`. El cron solo selecciona y encola;
  el trabajo va en `queue_job` (regla del proyecto).
- **`job_run_backup`**: por cada BD del entorno con `ec2_instance_id` (¡usa el vínculo, no
  `[:1]`!): script SSM en ESA instancia = `pg_dump -Fc` + `tar` del filestore de la BD +
  `aws s3 cp` de ambos al bucket de la política, key
  `<prefix>/<client>/<env>/<db>/<timestamp>.dump` / `...-filestore.tar.gz`. La EC2 sube a S3
  con su instance profile (ya exige S3 desde Fase 7 para el dump de staging; sin credenciales
  en el script). Crea el registro `primate.cloud.backup` en `in_progress` ANTES de correr y
  lo cierra en `completed/failed`; loguea en `operation.log`. BDs RDS: no se dumpean (las
  cubre el backup automático de RDS); BDs locales sin `ec2_instance_id`: backup `failed` con
  mensaje claro (incentiva asociar la instancia).
- **Retención**: regla de **lifecycle de S3 por prefijo** (AWS borra solo; robusto ante caídas
  del cron) + cron liviano que marca `expired` los registros vencidos. Requiere extender
  `aws_s3` (ver §3.7).
- **Botón "Respaldar ahora"** en el entorno: valida y encola el mismo job.
- **Diseño de la ejecución en la EC2 (decisiones previas al Bloque 3):**
  1. **Streaming a S3, sin dump en disco.** `pg_dump -Fc | aws s3 cp - s3://...` y
     `tar -czf - <filestore> | aws s3 cp - s3://...`: el dump **nunca toca el disco** de la
     instancia, lo que elimina de raíz el riesgo de llenar el disco de producción. Igual se
     mantiene una guarda `df` como cinturón (aborta si hay <1 GB libre en /tmp, que usan los
     temporales de pg_dump/aws-cli) y un `trap cleanup EXIT` que borra cualquier temporal
     propio en éxito o error. `set -euo pipefail` para que un fallo en cualquier lado del
     pipe aborte todo (y `aws s3 cp` aborta su multipart al fallar: no quedan objetos a medias).
  2. **Cero credenciales en scripts y logs.** El dump local usa `sudo -u postgres` (peer
     auth: sin contraseña ni connection string); la subida a S3 usa el instance profile de
     la EC2 (rol IAM, sin claves). El script solo imprime marcadores (`PCM_DUMP_SIZE_BYTES`,
     `PCM_BACKUP_OK`) y nombres de bucket/key/BD; nada sensible viaja a los logs de SSM ni a
     la bitácora. No se activa `xtrace`.
  3. **Instancia stopped/unreachable a la hora del backup:** se registra el intento como
     backup **`failed` con motivo explícito** ("instancia detenida" — chequeo barato contra
     el inventario ANTES de tocar SSM; "SSM inaccesible" si falla el comando) +
     `message_post` en el entorno. **No se enciende la instancia** (un backup nunca arranca
     infra) y no hay tormenta de reintentos: el cron corre cada hora y reintenta mientras la
     ventana de la política siga vencida (máx. 1 intento/hora, cada uno con su registro).
     La alerta de fondo la da el validador: si la ventana queda descubierta, el próximo
     check marca "No cumple".
  4. **Retención**: al ejecutar, PCM asegura la regla de **lifecycle S3** del prefijo
     (merge por rule-id, sin pisar reglas ajenas del bucket) con la retención de la
     política; un cron liviano marca `expired` los registros vencidos (AWS ya borró el
     objeto solo).
- **Buckets S3 (decisión FIJA, prueba real 2026-07-02): PCM NO crea buckets.** El bucket de
  una política debe existir de antemano, creado por un admin (no se agrega
  `s3:CreateBucket` a ninguna identidad de PCM). `ensure_bucket` verifica con
  `head_bucket` y, si falta, falla con el error accionable ("crealo a mano…") — ese es el
  comportamiento correcto, no un fallback. Permisos mínimos que SÍ necesita la política
  IAM del user (`pcm-operator`), alcance `arn:aws:s3:::pcm-*` y `arn:aws:s3:::pcm-*/*`:
  `s3:ListBucket`, `s3:GetObject`, `s3:PutLifecycleConfiguration`, `s3:DeleteObject`
  (limpieza/`delete_object`). El instance profile de las EC2 necesita `s3:PutObject` y
  `s3:GetObject` sobre el mismo alcance (las subidas/bajadas corren en la instancia).
- **Zona horaria (decisión previa al Bloque 2):** `execution_hour` se almacena e interpreta
  en **UTC**, y el validador compara en **UTC** — misma zona en toda la cadena. Motivo: los
  `Datetime` de Odoo se guardan en UTC, el cron de queue_job corre en el servidor y los
  timestamps de AWS (RDS `LatestRestorableTime`, snapshots) son UTC; interpretar la política
  en otra zona reintroduciría el bug clásico ("la política dice a las 3, el backup corrió a
  las 3 locales, el validador comparó en UTC y marcó incumplimiento"). En la UI el campo lo
  dice explícito en el label ("Hora de ejecución (UTC)"); NO se convierte a la tz del usuario
  en v1 — es un Float (el widget no convierte solo) y una conversión a medias sería peor que
  un UTC declarado. Si molesta en el uso real, se evalúa un campo computado de display.

### 1.3 Validador (esperado vs detectado, spec §10.2–10.3)

Cron diario `_cron_check_backup_compliance` → job por entorno:

- **Evidencia RDS**: `backup_retention_days` ya sincronizado + `LatestRestorableTime` /
  snapshots (extensión de `aws_rds`, §3.7) → frecuencia y retención reales.
- **Evidencia PCM**: registros `primate.cloud.backup` `completed` recientes (opcional
  `head_object` a S3 para confirmar que el objeto sigue ahí).
- **Regla**: cumple si la frecuencia real cubre la esperada (daily → hay backup < 26 h;
  twice_daily → < 14 h; hourly → < 2 h) Y retención real ≥ esperada. Errores de
  permisos/timeout → `unverifiable` (no `non_compliant`). Sin política → `no_policy`.
- Al pasar a `non_compliant`: `message_post` en el entorno + detalle en
  `backup_compliance_detail`. (Sin mail por ahora; el hub muestra el badge.)

### 1.4 Restore — DISEÑO DETALLADO (Bloque 4, aprobar antes de codear)

**Wizard `primate.cloud.backup.restore.wizard`** (solo `group_cloud_admin`), lanzado desde
el registro de backup (botón "Restaurar…" en form nativo y luego en el hub).

Campos:
- `backup_id` (readonly): el origen — entorno/BD/fecha/keys/tamaño a la vista.
- `target_environment_id` (required): **default = el entorno del backup SOLO si NO es
  producción; si el backup viene de producción, queda VACÍO** — nunca producción por
  default, ni siquiera como "vuelta al origen".
- `target_instance_id` (required, running, del entorno destino) y `target_db_name`
  (default: nombre de la BD origen).
- `confirm_environment_name` (Char): visible y required **solo si el destino es
  producción**; el job valida server-side que coincida EXACTO con el nombre del entorno
  (no alcanza el estado del botón en el cliente).
- `pre_backup` (Boolean, default True): "Respaldar el destino antes de restaurar".
- El botón lleva `confirm=` (ConfirmationDialog) ⇒ en producción hay **doble confirmación +
  nombre tipeado**.

**Orden del job `environment.job_restore_backup(params)`** (sobre el entorno DESTINO;
regla de oro: **validar todo antes de dropear nada**):
1. Validar el registro (estado `completed`, no `expired`) y **`head_object` de dump y
   filestore**: si el lifecycle ya los borró, error claro sin tocar el destino.
2. Instancia destino `running` (no se enciende infra, igual que en backups).
3. **Pre-backup del destino** (red de seguridad, default): reusa el ejecutor del Bloque 3
   con key `.../pre-restore/...`; deja su propio registro `primate.cloud.backup`
   ("Pre-restore …"). **Si el pre-backup falla, se ABORTA el restore.** Se omite (con nota
   en bitácora) solo si la BD destino no existe todavía — no hay nada que respaldar.
4. Script de preparación en el destino: guarda `df` (espacio libre ≥ tamaño conocido del
   dump × 1.5 — acá sí se baja a disco: `pg_restore` necesita el archivo para validar y el
   destino no es producción-origen), descarga, y **chequeo de compatibilidad PostgreSQL**:
   `pg_restore -l` extrae "dumped from database version X" del header del dump custom y se
   compara la major con `show server_version` del destino. **Origen > destino ⇒ aborta con
   marcador `PCM_ERROR_PG_MISMATCH origen=16 destino=14`** y mensaje accionable ("el dump
   viene de PostgreSQL 16 y el destino corre 14; actualizá PostgreSQL del destino o usá
   otra instancia"). Origen ≤ destino sigue (pg_restore es forward-compatible). Nada
   críptico de pg_restore a mitad de un drop.
5. Restore propiamente: `dropdb --if-exists` + `createdb` + `pg_restore` + **filestore**
   (untar + chown al usuario odoo) + restart de servicios. `trap` limpia el dump temporal
   en éxito o error.
6. **Neutralización**: si el destino NO es producción, se corre el pipeline completo de la
   Fase 7 post-restore (mail servers, fetchmail, crons, pagos, webhooks, web.base.url,
   tokens/API keys, cola de mail, dominio website — con las extensiones del Bloque 5)
   apuntando a la URL del destino. Si el destino ES producción, NO se neutraliza (el
   peligro es exactamente el inverso).
7. **Bitácora**: `backup_restore` sobre el entorno destino con name
   "Restaurar <backup> → <entorno>", resultado y error si lo hay; `log_operation` ya
   registra usuario y fecha. El pre-backup queda además como `backup_run` + su registro.
   `message_post` en el destino con backup de origen y resultado.

**BD + filestore JUNTOS** (sin checkbox "solo BD" en v1): el filestore es parte del estado
consistente — los `ir.attachment` de la BD referencian archivos por hash; restaurar solo la
BD deja adjuntos rotos silenciosos. Si el backup no tiene filestore (marcador
`PCM_FS_SIZE_BYTES=0` al ejecutarlo), se restaura solo la BD y queda dicho en la bitácora.
Si aparece un caso real de "solo BD", se agrega como opción avanzada después.

Micro-deltas del Bloque 4: el wizard (+ access admin), `backup.action_restore`,
`environment.job_restore_backup` + script builders. Sin campos nuevos de modelo.

### 1.4.b Restore (diseño original resumido)

- **Wizard `backup_restore_wizard`** desde un registro de backup: destino = BD/instancia de
  un entorno (default: el mismo entorno si es staging/testing). **Restaurar sobre
  `production` = operación destructiva**: doble confirmación (patrón del terminate) + log,
  y además el wizard exige **tipear el nombre exacto del entorno destino** para habilitar
  el botón de confirmación (ajuste aprobado 3.a).
- **Job**: script SSM en la instancia destino = `aws s3 cp` del dump y filestore →
  `dropdb/createdb` + `pg_restore` + untar del filestore + restart de servicios. Si el
  destino es un staging, re-ejecuta la neutralización después (reusa
  `_staging_neutralize`).
- **Sinergia con staging**: opción en el wizard de staging/refresh "usar el último backup
  del origen" en vez de dump fresco (evita cargar producción y reusa el mismo camino
  S3 → restore).

---

## 2. Staging consciente de instancias (reemplazo del `[:1]`)

### 2.0 Decisiones previas al Bloque 5 (patrón de siempre)

**Reuso del pipeline de Bloques 3-4 — SÍ, camino único.** La copia origen→staging deja de
tener camino propio (`_staging_copy_database` + `_build_dump_script`/`_build_restore_script`
de Fase 7) y pasa a ser: **backup gestionado del origen** (Bloque 3, prefix `staging/`; u
opcionalmente el **último backup existente**, sin recargar producción) + **restore estándar**
en la instancia del staging (Bloque 4, inline en el job). Además de "un solo camino
testeado", el reuso arregla dos deudas reales del camino propio: el dump de Fase 7 bajaba a
disco en la EC2 de PRODUCCIÓN (el Bloque 3 es streaming) y **no copiaba el filestore** (el
staging perdía los adjuntos). Bonus: cada staging deja su fuente como registro en
`primate.cloud.backup` (trazabilidad §14.4 real), la neutralización queda integrada en el
restore (destino no-prod), y `staging_origin_backup` pasa a guardar la key real del registro.
El código propio de Fase 7 se elimina ([REF] dentro del bloque).

**Origen RDS por endpoint — el ejecutor es la instancia EC2 de origen elegida.** Una RDS no
tiene EC2 propia: el `pg_dump` corre vía SSM en la **instancia origen del wizard**
(`staging_origin_instance_id`), que es la que tiene red hacia esa RDS (misma VPC/SG: es el
Odoo que la usa). Credenciales: **leídas in-situ del `odoo.conf` de esa instancia** dentro
del script (`db_user`/`db_password` → `PGPASSWORD` en el mismo proceso) — nunca viajan por
el input de SSM, ni se imprimen, ni tocan la bitácora. Si el entorno origen no tiene NINGUNA
EC2 (RDS pura sin servidor gestionado), staging desde RDS no está soportado en v1: error
claro pidiendo asociar una instancia. (Descartado ejecutar en la instancia DESTINO: cruza
VPC/SG entre entornos y complica la red sin ganancia.)

**Refresh:** mismo pipeline (backup fresco del origen o último backup + restore sobre la
instancia/BD propia del staging, resuelta por vínculo explícito), con wizard de opciones
(§14.5): base sola o base+repos, re-neutralizar (default sí), usar último backup. Sin
pre-backup del staging en el refresh (v1): el staging es descartable y el origen ya tiene su
backup registrado.

Hoy: `_enqueue_staging`/`job_create_staging`/`job_refresh_staging` y
`_staging_copy_database` resuelven origen y destino con `origin.ec2_instance_ids[:1]`,
`origin.database_ids[:1]`, `self.ec2_instance_ids[:1]`, `self.database_ids[:1]`.

### 2.1 Origen explícito

- El wizard de staging suma `origin_instance_id` (Many2one a `ec2.instance`, domain por
  entorno origen) y `origin_database_id` (Many2one a `database`, domain por entorno y
  coherente con la instancia elegida). **Defaults**: si el origen tiene exactamente 1
  servidor y 1 BD se preseleccionan (el caso común no cambia de UX); si hay varios, el campo
  queda requerido y el usuario elige.
- El dump corre vía SSM **en la instancia origen elegida**; si `origin_database_id` es RDS,
  el `pg_dump` apunta al `rds_endpoint` en vez de localhost (el script gana parámetro host).
- Trazabilidad persistida en el entorno staging: `staging_origin_instance_id` y
  `staging_origin_database_id` (readonly). El refresh los reusa — se acabó el `[:1]` también
  en `job_refresh_staging`.

### 2.2 Destino

- La creación sigue montando el staging en **una instancia nueva** (flujo actual). El
  registro `primate.cloud.database` del staging se crea con `ec2_instance_id` seteado, así
  el refresh resuelve destino por el vínculo y no por posición.
- **Decisión explícita a aprobar**: un staging replica UNA instancia+BD del origen, no el
  cluster completo. Multi-instancia se maneja eligiendo el origen; si algún día hace falta
  staging de N instancias, es otra fase.
- **Refresh con opciones (spec §14.5)**: wizard `staging_refresh_wizard` (drawer) con:
  refrescar solo base / base+repos, re-neutralizar (default sí), usar último backup del
  origen (§1.4). "Mantener DNS" y "mantener tamaño de instancia" son el comportamiento de
  siempre (el refresh no toca infra); no se exponen como opciones.

### 2.3 Neutralización — completar contra spec §14.3

El SQL ya está corregido (D5). Se agregan bloques guardados con `to_regclass`:

- **Cancelar la cola de correo pendiente**: `UPDATE mail_mail SET state='cancel' WHERE state
  IN ('outgoing','exception')` — sin esto, si alguien reactiva un mail server en el staging,
  salen los mails encolados de producción.
- **Invalidar tokens externos** (ítem de spec no implementado): `DELETE FROM
  res_users_apikeys` (API keys de Odoo). No tocar `totp_secret` (dejaría a los usuarios sin
  poder entrar).
- **Reapuntar dominio de website**: `UPDATE website SET domain = '%%STAGING_URL%%'` (guardado;
  evita redirecciones del staging a prod).

### 2.4 Backlog que esta fase paga

- `[:1]` de staging (origen y refresh) → §2.1/§2.2.
- Constraint de coherencia entorno en `database.ec2_instance_id` → §3.5.
- Asociar BD suelta desde la UI → §3.6.
- Label honesto "Sincronizar servidores desde AWS" → Bloque 6.
- (El "sync a nivel entorno" NO entra: no es staging ni respaldos; queda en BACKLOG.)

---

## 3. Micro-deltas de modelo (lista explícita para aprobación)

1. Modelo nuevo `primate.cloud.backup.policy` (spec) **+ campos de ejecución**
   `managed_by_pcm`, `s3_bucket`, `s3_prefix`, `execution_hour` (delta).
2. Modelo nuevo `primate.cloud.backup` (delta completo — registro de backups efectivos).
3. `environment`: `backup_policy_id` (spec) + `backup_compliance`,
   `backup_compliance_detail`, `last_backup_check` (delta).
4. `environment`: `staging_origin_instance_id`, `staging_origin_database_id` (delta,
   trazabilidad §14.4 "backup/base utilizada" a nivel registro, no solo Char).
5. **Constraint** (api.constrains, cruza tablas): si `database.ec2_instance_id` y
   `database.environment_id` están ambos seteados → deben apuntar al mismo entorno. No
   aplica si falta uno (el sync de AWS puede traer RDS sin entorno).
6. `database.action_assign_instance` — write de `ec2_instance_id` desde el hub con selector
   (solo instancias del mismo entorno, coherente con el constraint).
7. Servicios: `aws_rds` += `list_snapshots` / lectura de `LatestRestorableTime`; `aws_s3` +=
   `put_lifecycle_rule`, `list_objects`, `head_object` (siguen siendo adaptadores puros).
8. `operation.log.action_type` += `backup_run`, `backup_restore`, `backup_check`,
   `staging_refresh` (y `dns_update`/`dns_delete` si entra el Bloque 7).
9. Wizards nuevos: `backup_restore_wizard`, `staging_refresh_wizard`; el
   `staging_create_wizard` gana los dos selectores de origen.

## 4. UI en el hub

- **Detalle de entorno — sección "Respaldos"**: badge de cumplimiento (Cumple / No cumple /
  No verificable / Sin política) + política asignada (editable) + lista de últimos backups
  (fecha, BD, tipo, tamaño, estado) + botones "Respaldar ahora" y "Restaurar…" (wizard en
  drawer; doble confirmación si el destino es producción).
- **Detalle de BD**: sus backups + botón "Asociar a instancia" para BDs sueltas (§3.6).
- **Staging**: botón "Crear staging desde esta instancia" en el detalle de servidor
  (preselecciona `origin_instance_id`/`origin_database_id`); el botón del hub de entorno
  abre el wizard con los selectores. "Refrescar staging" pasa a abrir el wizard de opciones.
- **Sidebar**: "Respaldos" en Operaciones (lista global con filtro por estado/entorno);
  "Políticas de respaldo" en Configuración.
- Label del botón del hub → "Sincronizar servidores desde AWS".

## 5. Plan por bloques (orden y testeo)

| # | Bloque | Contenido | Testeo |
|---|---|---|---|
| 1 | Modelo y cimientos | policy + seeds, `primate.cloud.backup`, campos environment, constraint + `action_assign_instance`, action_types, **test de tags con literales** (ajuste 3.b) | TransactionCase puro: constraint (misma/otra env), seeds, estados |
| 2 | Detección y validador | extensiones `aws_rds`/`aws_s3`, job compliance + cron | mocks unitarios + **moto** (retención RDS, listing S3); casos ok/non_compliant/unverifiable/no_policy |
| 3 | Ejecución de backups | scripts SSM dump+filestore, `job_run_backup`, cron programador, lifecycle/expiración | mock SSM con golden de scripts; moto para S3/lifecycle; ventanas de frecuencia con freeze de fechas |
| 4 | Restore (**FRENO previo**: revisar diseño del wizard antes de codear) | wizard + job restore, doble confirmación + tipear nombre del entorno si destino prod, re-neutralización si staging | mocks; **smoke_ui**: wizard en drawer (render, validación, cancelar) |
| 5 | Staging instancia-consciente | selectores de origen, `staging_origin_*`, dump con host RDS, refresh wizard, neutralización ampliada, botón en servidor | unit multi-instancia (2 servidores/2 BDs: elige bien, constraint respeta), golden del SQL, **smoke_ui** del wizard |
| 6 | UI hub respaldos | sección respaldos + badges + drill, label sincronizar | **smoke_ui**: sección visible, drill-through, wizards cancelar |
| — | Prueba real controlada | como la de EC2: entorno demo → backup real a S3 → restore a staging → staging eligiendo instancia → terminar y limpiar bucket | manual guiada, cuenta 603011031378 |

(El ex-Bloque 7 de DNS CRUD pasó a **Fase 8.5**, fuera de esta fase.)

Cada bloque cierra con la suite completa verde antes de avanzar. Lo que requiere navegador
va a `tools/smoke_ui`; lo que toca AWS real queda para la prueba controlada final.
