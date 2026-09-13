# Propuesta de diseño — Fase 9: Observabilidad (CloudWatch + logs) y Costos (Cost Explorer)

> Estado: **PROPUESTA, pendiente de aprobación. Cero código escrito.**
> Fuentes: spec funcional §11 (logs), §12 (monitoreo), §13 (costos); spec técnica §5 (modelos
> `cost.entry`/`monitor.snapshot`), §8.3 (IAM); `ARCHITECTURE.md` (Fase 9); y el código actual
> (`build_resource_tags` de Fase 1, `aws_ssm`, dashboard con KPI de alertas ya derivado).

## 0. Qué hay hoy y qué se apoya en lo existente

- **Tags `primate:*` desde la Fase 1.** `build_resource_tags(client, environment)` etiqueta
  todo recurso con `primate:client = project.name`, `primate:environment = env.name`,
  `primate:managed_by = pcm`. **La atribución de costos se apoya en ESTOS tags** — no se
  reinventa nada. (Consecuencia importante: el valor del tag `primate:environment` es el
  **nombre** del entorno; la atribución mapea por nombre — ver §2.)
- **KPI "Alertas" ya es DERIVADO** (dashboard: cuenta repos divergentes + entornos en error +
  cuentas con conexión fallida). No hay modelo de alerta. La Fase 9 **extiende** esa
  agregación, no crea un modelo de alerta (ver §5).
- **`aws_ssm` ya corre comandos** en las EC2 (usado en provisioning/backups). Los logs se
  apoyan en él (§4), sin agente extra.
- **Modelos `cost.entry` y `monitor.snapshot` NO existen** aún (nombrados en spec/arquitectura,
  sin sección de campos detallada → se diseñan acá, delta explícito).

## 0.1 Paralelo a "PCM no crea buckets/zonas"

**PCM NO activa cost allocation tags.** Para que Cost Explorer agrupe por
`primate:environment`, ese tag debe estar **activado como *cost allocation tag*** en la
consola de Billing → *Cost allocation tags* (lo hace un admin, no PCM). Sin activarlo, la
atribución por tag NO funciona aunque los recursos estén etiquetados. Va al runbook. (Además:
un tag recién activado solo empieza a discriminar costos **desde esa fecha**, no retroactivo.)

---

## 1. Decisión — Cost Explorer: retardo, costo, granularidad, honestidad

**Se snapshotea en `cost.entry` (persistido); la UI SIEMPRE lee de ahí, nunca llama a CE.**
Es lo correcto dado el retardo (datos con horas/hasta un día de atraso) y el costo por request
(~USD 0.01 c/u).

- **Un cron diario** por cuenta hace el *pull* y persiste. Consultas por request (CE permite
  **máx. 2 GroupBy** por request):
  1. `get_cost_and_usage` mes en curso, `Granularity=DAILY`, `GroupBy=[TAG primate:environment,
     DIMENSION SERVICE]` → costo por (entorno, servicio) del mes.
  2. `get_cost_and_usage` mes anterior, `Granularity=MONTHLY`, mismo GroupBy → comparativa.
  3. `get_cost_forecast` mes en curso → tendencia/proyección a fin de mes.
  Total ≈ **3 requests/día/cuenta ≈ USD 0.03/día** por cuenta. Barato y acotado.
- **Botón "Actualizar costos"** (a nivel cuenta): encola el mismo job, con **guarda de
  frescura** — si el último pull fue hace < N horas (configurable, default 6), no vuelve a
  pegarle a CE (evita pagar de más y respeta el retardo, que igual no cambia en minutos).
- **Honestidad de la UI:** toda vista de costos muestra **"datos al `<pulled_at>`"** y una nota
  del retardo de CE. Nunca aparenta tiempo real.

## 2. Decisión — Atribución por cliente/entorno (el pago de los tags)

> ⚠️ **SUPERSEDIDO por "Opción A" (ver más abajo).** La atribución NO se hace por nombre
> (`primate:environment`) sino por un identificador estable `primate:environment_id = pcm_ref`,
> con match exacto por `pcm_ref` — sin el caveat de colisión de nombres. El bucket "Sin
> atribuir" y la exclusión del reparto §13.3 siguen valiendo. Se deja el texto original abajo
> como contexto de la decisión.

- **Requisito (runbook):** activar `primate:client` y `primate:environment` como cost
  allocation tags (admin, en Billing). Sin eso, CE devuelve todo bajo un solo bucket.
- **Mapeo:** el GroupBy por TAG devuelve el **valor** del tag (= nombre del entorno / proyecto).
  El pull matchea ese valor contra los registros PCM por nombre y persiste el `cost.entry` con
  `environment_id` resuelto. **Caveat a asumir:** si dos entornos comparten nombre exacto, la
  atribución colisiona — se documenta; una mejora futura sería etiquetar por id.
- **Costo NO atribuible → bucket "Sin atribuir", SIN inventar reparto.** Todo costo cuyo tag
  `primate:environment` esté vacío/ausente (cargos de cuenta, data transfer, RDS compartida sin
  tag, recursos creados fuera de PCM) se persiste con `environment_id = False` y se muestra
  como **"Sin atribuir"**. No se prorratea.
- **Reparto de recursos compartidos (spec §13.3: % fijo / por almacenamiento / por usuarios /
  custom): FUERA de v1** (delta anotado). Requiere un modelo de reglas de distribución; en v1
  el costo compartido cae en "Sin atribuir" y el operador lo ve explícito. Se construye después
  si hace falta (encaja con el módulo `primate_cloud_contracts` de la spec §9).

## 3. Decisión — CloudWatch: métricas, resolución, dónde viven

**Snapshot por cron a `monitor.snapshot` (historial + base de alertas) + refresh on-demand al
abrir el detalle** (con cache corto para no repetir `GetMetricData`). Ambos: el cron sostiene
el historial y las alertas; el on-demand da frescura al mirar una instancia.

- **Costo controlado:** `GetMetricData` empaqueta **todas las métricas de una instancia en UN
  request** (hasta 500 queries/request), facturado por métrica-query. Un request por instancia
  por snapshot; con cron **horario** (configurable) el costo es marginal.
- **EC2 (v1, SIN agente CloudWatch):** `CPUUtilization`, `NetworkIn`, `NetworkOut`,
  `StatusCheckFailed` (system + instance). Resolución 5 min (o 1 min detallada si se habilita).
  ⚠️ **RAM y disco NO están disponibles sin el agente CloudWatch** (la spec §12.1 los lista vía
  "agente instalado en EC2", que `install_odoo.sh` NO instala) → **delta:** RAM/disco quedan
  para cuando se agregue el agente al provisioning (necesitaría `cloudwatch:PutMetricData` en el
  rol `pcm-ssm-role`). Uptime se saca por SSM (`uptime`), no por CloudWatch.
- **RDS (v1, completo — RDS publica sin agente):** `CPUUtilization`, `FreeStorageSpace`,
  `DatabaseConnections`, `ReadIOPS`, `WriteIOPS`.
- **Status checks = la señal de error REAL de EC2.** A diferencia de `instance_state` (que
  anotamos que no tiene estado de error propio), `StatusCheckFailed >= 1` es una falla objetiva
  → **entra como fuente de alertas** (§5) y como el "Crítico" del estado del entorno (spec §12.3).
- **Estado del entorno (spec §12.3):** `ok / warn / critical` computado de la última snapshot
  contra umbrales (CPU > 80% warn, > 95% crit; status check failed = crit). Umbrales como
  constantes en v1; política de monitoreo configurable = delta menor.

## 4. Decisión — Logs (on-demand, sin persistir)

- **Mecanismo v1: SSM (reusa `aws_ssm`), NO CloudWatch Logs.** Los logs se leen en vivo de la
  EC2 que PCM aprovisiona (que ya tiene los archivos locales): `journalctl -u odoo`
  (Odoo/systemd), `tail`/`grep` de `/var/log/nginx/*.log`, log de PostgreSQL, `journalctl`/
  `dmesg` (OS). No requiere agente ni configuración extra — funciona con lo que PCM ya instala.
  CloudWatch Logs queda como camino **opcional/futuro** (solo si el entorno define un log group).
- **NO se persisten** salvo el "descargar como archivo" explícito (spec §11.2), que arma el
  texto al vuelo y lo entrega — no crea registros en BD.
- **Interfaz (spec §11.2):** filtro por fuente (Odoo/PostgreSQL/Nginx/OS), rango de fechas
  (`journalctl --since/--until`, o `tail`), búsqueda por texto (`grep`), cantidad de líneas
  (100/500/1000), y descargar.
- **Seguridad:** los comandos de log **no** vuelcan credenciales ni el `odoo.conf`; se leen solo
  los archivos de log. Nada sensible viaja a la UI.

## 5. Decisión — Alertas (derivadas, sin modelo de estado en v1)

**Se mantiene DERIVADO** (agregación de solo lectura), extendiendo el patrón que ya existe en
el dashboard. NO se crea un modelo de alerta con estado (ack/mute) en v1 — eso es un delta
futuro. En v1 cuenta como alerta:

- **Status check failed** (de la última `monitor.snapshot`) → la señal nueva y más fuerte.
- **Backup "No cumple"** (`environment.backup_compliance == 'non_compliant'`, Fase 8).
- **DNS/Repo divergente** (`sync_state == 'divergent'`, Fases 5 y 8.5).
- **Entorno en error / cuenta con conexión fallida** (ya existían).
- **(Opcional) Costo sobre umbral:** solo si el entorno tiene un `cost_budget_monthly` seteado
  y el costo del mes en curso lo supera. Se puede **diferir** para mantener v1 chico; lo
  propongo como opcional detrás de un campo de presupuesto.

El KPI del dashboard suma estas fuentes; cada alerta linkea al recurso (igual que hoy).

---

## 6. Micro-deltas (lista explícita para aprobación)

### Modelos nuevos
1. `primate.cloud.cost.entry`: `account_id`, `environment_id` (False = sin atribuir),
   `service` (Char: EC2/RDS/Route53/S3/CloudWatch/otros), `period_start`, `period_end`,
   `granularity` (daily/monthly), `amount` (Monetary), `currency_id`, `source` (cost_explorer),
   `is_forecast` (Boolean), `pulled_at` (Datetime). Inmutable estilo evidencia (como backups).
2. `primate.cloud.monitor.snapshot`: `ec2_instance_id` **o** `database_id` (uno u otro),
   `snapshot_date`, y métricas: `cpu`, `network_in`, `network_out`, `status_check_failed`
   (EC2); `free_storage_gb`, `db_connections`, `read_iops`, `write_iops`, `cpu` (RDS).
   Append-only.

### Servicios nuevos (adaptadores puros)
3. `aws_cost_explorer`: `get_cost_and_usage(start, end, granularity, group_by)`,
   `get_cost_forecast(start, end)`, `get_tags(tag_key)`. Devuelven datos normalizados.
4. `aws_cloudwatch`: `get_ec2_metrics(instance_id, region, metrics, period, window)`,
   `get_rds_metrics(rds_identifier, region, ...)` — ambos vía `get_metric_data` batcheado.
5. **Logs: sin servicio nuevo persistente.** Un método de modelo sobre `aws_ssm`
   (`ec2.instance._fetch_logs(source, since, until, lines, grep)`) arma el comando y devuelve
   texto. (Un `aws_cloudwatch.get_log_events` queda anotado para el camino CW Logs futuro.)

### Campos nuevos (cache para UI rápida)
6. `environment`: `cost_current_month`, `cost_prev_month`, `cost_forecast` (Monetary, cache del
   último pull), `cost_pulled_at`, `monitor_state` (ok/warn/critical, compute de la última
   snapshot), `cost_budget_monthly` (opcional, para la alerta de costo).
7. `ec2.instance`: `last_cpu`, `last_status_check_failed`, `last_metric_date` (cache de la
   última snapshot para mostrar sin recomputar). `database`: `last_cpu`, `last_connections`,
   `last_free_storage_gb`, `last_metric_date`.

### Crons
8. `_cron_pull_costs` (diario, por cuenta conectada) y `_cron_snapshot_metrics` (horario,
   configurable, por instancia/RDS activa). Ambos encolan `queue_job` (nunca en el worker web).

### Bitácora / seguridad
9. `operation.log.action_type` += `cost_pull`, `metrics_snapshot` (para auditar las consultas).
   Los tres modelos nuevos: lectura para viewer, sin escritura desde UI (append-only por código).

---

## 7. IAM — permisos nuevos para `pcm-operator` (verificados contra la doc de AWS)

Cost Explorer y CloudWatch métricas **no tienen resource-level** (obligan `Resource: "*"`).
CloudWatch Logs sí soporta ARN por log-group, pero `DescribeLogGroups` obliga `*`.

```json
{
  "Sid": "PcmCostExplorer",
  "Effect": "Allow",
  "Action": ["ce:GetCostAndUsage", "ce:GetCostForecast", "ce:GetTags"],
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
```

- **Logs por SSM (camino v1): NO necesita permisos nuevos** — `ssm:SendCommand`/
  `GetCommandInvocation` ya están. El statement de `logs:*` (`FilterLogEvents`, `GetLogEvents`,
  `DescribeLogGroups`, `DescribeLogStreams`) **solo hace falta si se usa el camino CloudWatch
  Logs** (futuro); lo anoto en el runbook como opcional.
- **El rol `pcm-ssm-role` NO necesita nada nuevo en v1** (las consultas CE/CloudWatch corren
  desde Odoo). Solo si en el futuro se agrega el **agente CloudWatch** para RAM/disco, el rol
  necesitaría `cloudwatch:PutMetricData` — anotado, no en v1.

---

## 8. UI en el hub

- **Costos — pantalla nueva a nivel cuenta/cliente** (`Costos` en el sidebar): mes en curso,
  mes anterior, proyección, **desglose por servicio** y **por entorno** (con el bucket "Sin
  atribuir" explícito), y la leyenda **"datos al `<fecha>`"** + botón "Actualizar costos".
  A nivel **entorno** (hub): tarjeta de costo del entorno (su slice) con la misma leyenda.
- **Métricas — en el detalle de servidor/instancia:** última snapshot (CPU, red, status check)
  + mini-historial, y en el detalle de RDS (storage/conexiones/IOPS/CPU). Badge `monitor_state`
  del entorno en el hub. Refresh on-demand con "datos al `<fecha>`".
- **Logs — visor on-demand en el detalle de servidor** (drawer/pantalla): filtros
  (fuente/fechas/texto/líneas), botón "Traer logs" (SSM) y "Descargar". No persiste.
- **Alertas:** el KPI del dashboard suma las fuentes nuevas; sin pantalla nueva.

---

## 9. Plan por bloques (orden y testeo)

| # | Bloque | Contenido | Testeo |
|---|---|---|---|
| 1 | Servicios + modelos | `aws_cost_explorer`, `aws_cloudwatch`; modelos `cost.entry`, `monitor.snapshot`; IAM al runbook | **moto** (round-trip de get_cost_and_usage / get_metric_data — datos vacíos, valida forma) + unit de parsing de respuestas sintéticas |
| 2 | Costos: pull + atribución | `_cron_pull_costs`, job, mapeo tag→entorno, bucket "Sin atribuir", cache en environment, guarda de frescura, action_type | mocks con respuestas CE agrupadas (env+servicio, sin-tag); atribución por nombre; forecast; honestidad `pulled_at` |
| 3 | Métricas + estado + alertas | `_cron_snapshot_metrics`, job, `monitor_state` por umbrales, status-check como alerta, extensión del agregador de alertas del dashboard | mocks CloudWatch; umbrales ok/warn/critical; status check → alerta; conteo de KPI |
| 4 | Logs + UI | logs on-demand por SSM (sin persistir), pantalla de costos, métricas en detalle, visor de logs | mock SSM para logs; **smoke_ui**: pantalla de costos con "datos al", visor de logs (traer/cancelar) |
| — | **Prueba real controlada** (barata, sin infra) | 1 `get_cost_and_usage` real + 1 `get_metric_data` real sobre la cuenta; verificar parsing de respuestas reales, que la atribución matchea tags reales, y **si los cost allocation tags están activados** (si no, reportarlo) | manual guiada, cuenta 603011031378 |

**Por qué prueba real pese a moto:** moto responde el *round-trip* de CE y CloudWatch pero con
**datos vacíos/sintéticos** (no genera costos ni métricas reales), así que valida la forma pero
no los valores ni la atribución real por tag. Una consulta real (barata: CE ~USD 0.01/request,
GetMetricData marginal, **sin infra que facture por hora**) confirma el parsing de respuestas
reales y si los tags de asignación están activos.

---

## 10. Contradicciones / notas contra spec y código

- **Spec §12.1 (RAM/disco de EC2 vía agente CloudWatch):** NO disponible en v1 —
  `install_odoo.sh` no instala el agente. v1 EC2 = CPU/red/status checks; RAM/disco = delta
  (agregar agente + `cloudwatch:PutMetricData` al rol). RDS sí trae todo sin agente.
- **Spec §13.3 (reparto de recursos compartidos):** FUERA de v1 → bucket "Sin atribuir". Delta
  futuro (encaja con `primate_cloud_contracts`).
- **Spec/arquitectura nombran `cost.entry`/`monitor.snapshot` sin sección de campos:** los
  campos se diseñan acá (delta).
- **KPI "Alertas" derivado (sin modelo de estado):** se mantiene como está el código hoy; un
  modelo de alerta con ack/mute es delta futuro.
- **Atribución por nombre de entorno** (el tag guarda `env.name`, no id): funciona pero colisiona
  si hay nombres repetidos — caveat documentado.
- **PCM no activa cost allocation tags** (los activa un admin en Billing) — paralelo a "no crea
  buckets/zonas", explícito en §0.1 y en el runbook.

---

# Opción A — Identificador estable de atribución + re-tagging (Bloque 1 de la Fase 9)

> **Decisión tomada:** la atribución de costos NO se apoya en `primate:environment` = nombre
> (mutable, no único, parte la serie histórica al renombrar). Se agrega un **identificador
> estable** como clave y se re-taggean los recursos existentes. Toca la Fase 1
> (`build_resource_tags`), así que es DISEÑO primero. **Reemplaza el mapeo por nombre de §2.**
> Estado: **PROPUESTA, cero código, FRENADO hasta aprobación.**

## A.1 — Qué identificador estable se usa como clave

**Un slug inmutable dedicado por entorno: `pcm_ref = "pcm_env_" + uuid4().hex`** (ej.
`pcm_env_85d2a04d64324d82992cb0896f1aa210`). Análogo por proyecto/cliente:
`pcm_ref = "pcm_cli_" + uuid4().hex`.

- **Inmutable:** se genera UNA vez al crear el registro y no cambia al renombrar (`readonly`,
  `copy=False`; el `copy=False` evita que un duplicado herede el ref).
- **Único global:** `uuid4` → sin colisiones ni dentro de la cuenta ni entre cuentas/clientes
  que compartan recursos. (Descarto usar el `id` de BD: no es único entre bases/instancias de
  Odoo y filtra un detalle interno al tag; descarto un `external_id`/xmlid: no está garantizado
  ni es estable.)
- **Legible en Cost Explorer:** los valores de tag de AWS admiten hasta 256 chars del set
  `[A-Za-z0-9 _.:/=+@-]`. El slug (40 chars, solo `[a-z0-9_]`) **cumple** — verificado. Humanos
  lo mapean de vuelta al entorno vía PCM (el valor es la clave de agrupación, no un nombre).

**`primate:environment` (nombre) se conserva SOLO para lectura humana** en la consola de AWS;
deja de ser la clave de atribución. La Fase 9 agrupa por `primate:environment_id`.

## A.2 — Cómo queda `build_resource_tags`

Se agregan dos constantes y dos parámetros (retrocompatibles, opcionales):

```
ENVIRONMENT_ID_TAG = "primate:environment_id"   # clave estable de atribución
CLIENT_ID_TAG      = "primate:client_id"        # clave estable de cliente/proyecto

build_resource_tags(client, environment, client_ref=None, environment_ref=None, extra=None)
  -> primate:managed_by = pcm
     primate:client        = client        (nombre, humano — se conserva)
     primate:environment   = environment   (nombre, humano — se conserva)
     primate:client_id     = client_ref        (estable, si se pasa)
     primate:environment_id= environment_ref   (estable, si se pasa)
```

- Los 3 llamadores actuales (`_provision_ec2`, `_provision_database`,
  `account.job_create_ec2`) pasan además `client_ref=project.pcm_ref`,
  `environment_ref=env.pcm_ref`.
- **`primate:client` también gana ID estable** (`primate:client_id = project.pcm_ref`), con el
  mismo criterio: hoy `primate:client` = `project.name` (mutable). Nota: la atribución "por
  cliente" queda a nivel **proyecto** (que es lo que el tag representa hoy); si el negocio
  quiere agrupar por **partner** (varios proyectos → un cliente), se usaría `project.partner_id`
  — anotado como opción, no en v1 para no cambiar la semántica actual.
- **Compatibilidad:** los tags viejos por nombre se conservan; los `_id` se **suman**. Nada se
  rompe.

## A.3 — Re-tagging de lo existente (idempotente, por vínculo del modelo)

**Acción por cuenta** (`account.action_retag_resources()` → `queue_job`; solo `group_cloud_admin`),
re-ejecutable. NO cron: es un backfill de tags en AWS; los recursos NUEVOS ya nacen con los
tags al crearse. El botón se puede volver a apretar sin efecto adverso.

- **A qué entorno pertenece cada recurso: por el VÍNCULO en el modelo, no por el tag viejo.**
  El job recorre los `ec2.instance` de la cuenta que tienen `environment_id` seteado y
  `aws_instance_id`, calcula los tags estables de ese entorno/proyecto y llama
  `ec2:CreateTags`. (El tag viejo por nombre es justo lo poco fiable que evitamos.)
- **Idempotente por naturaleza:** los tags de AWS son un mapa clave→valor; `CreateTags` con la
  misma clave **fija** el valor (no duplica). Re-ejecutar produce el mismo estado.
- **PCM solo taggea lo que gestiona:** recursos que PCM ve por sync pero **no** tiene vinculados
  a un entorno (`environment_id` vacío) **se dejan sin re-taggear** (no son atribuibles a un
  entorno; caerían igual en "Sin atribuir"). Documentado.
- **Alcance v1: EC2 (instancia + volúmenes).** RDS necesitaría `rds:AddTagsToResource`
  (ver A.4); los entornos reales usan PostgreSQL local (sin recurso RDS separado que taggear
  más allá de la EC2), así que EC2 cubre el caso común. RDS = delta si se usa.
- **Errores por recurso, sin abortar el lote:** cada `CreateTags` en su try/except; si un
  recurso falla (terminado, permiso) se registra y el job sigue. Devuelve conteo
  `{tagged, skipped, failed}` + una entrada en `operation.log` (`retag`).

## A.4 — IAM

- **EC2:** `ec2:CreateTags` **ya está** en el statement `Ec2Provision` de `pcm-operator`
  (confirmado). El re-tagging de EC2 no necesita permiso nuevo.
- **RDS (solo si se re-taggean RDS):** `rds:AddTagsToResource` **no** está en la política actual
  (y RDS tampoco tiene hoy permisos de creación en el runbook, porque los entornos usan
  PostgreSQL local). Si se habilita RDS: sumar `rds:AddTagsToResource` (+ los de creación) al
  runbook. Anotado.
- El rol `pcm-ssm-role` no necesita nada (el re-tagging corre desde Odoo).

## A.5 — Cost allocation tags (va al runbook, no lo hace PCM)

- El tag NUEVO **`primate:environment_id`** (y `primate:client_id`) debe **activarse como cost
  allocation tag** en Billing por un admin, igual que los demás. Sin activarlo, Cost Explorer
  no agrupa por él.
- **La atribución de la Fase 9 usa `primate:environment_id`**, NO `primate:environment`.
  Documentado en el runbook.
- ⚠️ **La activación NO es retroactiva:** Cost Explorer solo discrimina por el tag **desde la
  fecha de activación / desde que el recurso lo lleva**. Aun re-taggeando lo existente, los
  costos de meses anteriores no se atribuyen por la clave nueva y quedan en "Sin atribuir" para
  ese período. La atribución fina arranca **hacia adelante**. Limitación honesta a comunicar.

## A.6 — Plan (este es el Bloque 1 de la Fase 9; va ANTES de Cost Explorer)

| Paso | Contenido | Testeo |
|---|---|---|
| a | `pcm_ref` en `environment` y `project` (default `uuid4`, readonly, copy=False) + **backfill** de existentes vía `migrations/19.0.x/post-migrate.py` (bump de versión), sin colisiones (uuid) | unit: create genera ref único; backfill llena los vacíos y no pisa los existentes |
| b | `build_resource_tags` emite `primate:environment_id`/`primate:client_id` + los 3 llamadores pasan los refs | unit: los tags nuevos salen con el valor del `pcm_ref`; los viejos se conservan |
| c | `aws_ec2.create_tags(ids, tags, region)` + `account.action_retag_resources()`/job (idempotente, por vínculo, errores por recurso) + `operation.log` `retag` | **moto**: re-tag idempotente (2 corridas = mismo estado), skip de no-vinculados, un recurso que falla no aborta |
| — | **Pasada real (barata, opcional):** re-taggear una EC2 **corriendo** y confirmar en AWS (`describe_instances`) que `primate:environment_id` quedó. Si no hay instancia viva (las de prueba están terminadas), moto alcanza y se verifica en el próximo provision real | manual, cuenta 603011031378 |

Cost Explorer (Bloque 2 en adelante de la propuesta original) agrupa por
`primate:environment_id` y resuelve el `environment_id` **por `pcm_ref`** (exacto, estable),
no por nombre. El bucket "Sin atribuir" (§2) sigue igual para lo no vinculado / sin tag.

## A.7 — Contradicciones / notas

- **§2 de esta propuesta (atribución por nombre) queda SUPERSEDIDO** por A.1–A.5: la clave es
  `primate:environment_id = pcm_ref`, el match es por `pcm_ref` (sin caveat de colisión de
  nombres).
- **Activación de cost allocation tag no retroactiva** (A.5): la atribución fina es hacia
  adelante; histórico previo en "Sin atribuir".
- **RDS re-tag necesita `rds:AddTagsToResource`** (A.4), fuera de v1 mientras se use PostgreSQL
  local.
- **Backfill = migración con bump de versión del manifest** (`19.0.1.x.0`): los entornos
  existentes reciben `pcm_ref` sin que el usuario haga nada, en el `-u`.

---

# Bloque 2 — Mapeo de vuelta pcm_ref → entorno (mini-freno, diseño antes de codear)

> Cost Explorer devuelve costos agrupados por el **valor** del tag
> (`primate:environment_id` = un `pcm_ref`, ej. `pcm_env_85d2...`). La UI necesita el
> **nombre** del entorno. El detalle crítico: **un entorno borrado puede seguir teniendo costos
> en el histórico** (el tag persiste en los registros de billing de AWS aunque el registro PCM
> ya no exista). El mapeo NO puede asumir que el entorno todavía existe.

## Decisión: la clave durable vive en el `cost.entry`, no en el vínculo al entorno

Cada `cost.entry` persiste **tres** datos de atribución, de más a menos durable:

1. **`pcm_ref` (Char)** — el valor CRUDO del tag que devolvió Cost Explorer. **La clave real
   e inmutable.** Siempre se guarda tal cual vino de AWS, exista o no el entorno.
2. **`environment_name` (Char)** — **snapshot denormalizado** del nombre del entorno *al
   momento del pull*. Es lo que muestra la UI. Sobrevive al borrado del entorno (no es un
   related; es una copia).
3. **`environment_id` (Many2one, `ondelete='set null'`)** — vínculo VIVO de conveniencia, solo
   para drill-through cuando el entorno todavía existe. Si el entorno se borra después, este
   campo se pone en `False` **pero el `cost.entry` conserva `pcm_ref` + `environment_name`** →
   el costo no se pierde ni se rompe la vista.

## Resolución en el pull (por `pcm_ref` EXACTO, nunca por nombre)

Para cada grupo que devuelve CE (valor = `pcm_ref`):

- **`pcm_ref` con valor y entorno existe:** `search([('pcm_ref','=',valor)])` →
  `environment_id = env`, `environment_name = env.name`. (Match exacto por la clave estable:
  cero colisiones — el pago de la Opción A.)
- **`pcm_ref` con valor pero el entorno YA no existe** (borrado antes del pull): `environment_id
  = False`, `environment_name = _("Entorno eliminado")`, y se conserva el `pcm_ref`. El costo se
  persiste igual (es real) y se muestra como "Entorno eliminado (`pcm_env_...`)".
- **Sin valor de tag** (recurso sin `primate:environment_id`, cargos de cuenta, etc.):
  `pcm_ref = False`, `environment_id = False`, `environment_name = _("Sin atribuir")`.

## Por qué es robusto ante entornos eliminados

- **Borrado DESPUÉS del pull:** `environment_id` → `False` (set null), pero `environment_name`
  (snapshot) y `pcm_ref` quedan. La UI muestra el nombre capturado + marca "(eliminado)".
- **Borrado ANTES del pull:** nunca capturamos su nombre; se muestra el `pcm_ref` crudo. El
  costo NO se pierde ni tira la vista — el operador ve la clave y puede investigar.
- **La UI nunca depende de que el entorno viva:** lee `environment_name` (siempre presente);
  el link a `environment_id` se ofrece solo si sigue seteado.

## Notas
- **Mejora opcional (no v1):** un mini-registro `pcm_ref → último nombre conocido` poblado al
  provisionar/re-taggear, para que incluso un entorno borrado ANTES de cualquier pull tenga
  nombre. En v1 alcanza con el snapshot en el pull (los entornos suelen vivir mientras acumulan
  costo). Anotado.
- El mismo esquema aplica a `client_id` → partner (`partner_ref` + `partner_name` denormalizado
  + M2o `ondelete=set null`), por si un cliente se borra.

---

# Bloque 4 — Decisión de retención de monitor.snapshot (antes de acumular)

> El cron horario genera ~24 filas/instancia/día (~720/mes, ~8760/año) append-only. Sin
> límite crece sin techo. `cost.entry` no lo necesita (upsert por período), pero las métricas
> sí. Decisión CONSCIENTE ahora (más barato que con millones de filas).

## Decisión: ventana cruda + downsample diario + tope duro

Esquema híbrido (bounded, sin perder tendencia de largo plazo):

1. **Ventana cruda `SNAPSHOT_RAW_DAYS = 14`:** las snapshots horarias se conservan tal cual los
   últimos 14 días (detalle fino reciente, que es lo que se mira en un incidente).
2. **Downsample a DIARIO** para lo más viejo que 14 días: se conserva **una snapshot por
   (recurso, día)** —la más nueva del día— y se borran las demás. El histórico largo queda a
   granularidad diaria (suficiente para la tendencia; el alerting usa solo la ÚLTIMA snapshot
   vía el cache de la instancia, no el histórico).
3. **Tope duro `SNAPSHOT_MAX_DAYS = 365`:** se borra todo lo más viejo que un año.

Estado estacionario acotado: ~`24×14 + 351` ≈ **700 filas/instancia** (no crece sin techo).

- **Cron diario de limpieza** (`monitor.snapshot._cron_cleanup_snapshots`) por SQL (DELETE en
  bloque; eficiente con muchas filas). El `DISTINCT ON (recurso, día)` conserva la más nueva de
  cada día en la franja a downsamplear.
- Constantes ajustables; si un cliente quiere más/menos historia, se tocan sin migración.
- No afecta a `cost.entry` (upsert) ni a `operation.log` (que es inmutable por auditoría y no
  crece por métricas).

## Prueba real de cierre (2026-07-03) — parsing/métricas OK, cuadre real DIFERIDO

Se corrió la pasada real contra la cuenta `603011031378` (`pcm-operator`, tras aplicar los
statements IAM `PcmCostExplorer` + `PcmCloudWatchMetrics`). Resultado:

- **Parsing CE real: OK.** `get_cost_and_usage` agrupado por `[TAG primate:environment_id, SERVICE]`
  devolvió el formato EXACTO que asume el código: keys `['primate:environment_id$<valor>', '<Servicio>']`
  (split en `$` correcto), amounts→float, paginación y currency sin problemas. Período vacío
  (mes anterior) → 0 grupos, tolerado sin error.
- **Métricas CloudWatch reales: OK con datos NO-CERO.** `get_ec2_metrics` sobre una instancia ya
  terminada (CW retiene ~15 días) devolvió cpu≈6%, network_in/out reales y `status_check_failed=0.0`
  **tipo `float`** (como asume el código); la lógica de alerta (`>=1` = crítico) valida.
- **Grupos en 0.0:** se aplicó la mejora de NO persistir filas de detalle en cero (ruido), sin
  afectar el cuadre (reconcilia contra `result["total"]` de CE, no contra la suma persistida).
  Tests `test_grupos_cero_no_se_persisten_pero_cuadran` y `test_repull_a_cero_elimina_fila_vieja`.

### ⚠️ Validación del cuadre contra montos reales — DIFERIDA (no es deuda de código)

El mecanismo de `_reconcile_costs` está implementado y cubierto por tests con mock
(suma=total, detecta descuadre, con entorno borrado cuadra, saltar ceros no rompe). PERO la
verificación **contra datos reales** solo pudo ejercitarse en el **caso cero**: la cuenta
`603011031378` es free-tier / sin gasto facturable (~USD 0.0), así que el cuadre se validó real
únicamente con total=0. **La validación real del cuadre contra montos no-cero queda PENDIENTE
hasta la primera cuenta con gasto real.** No se pierde como "ya validado": es una verificación
real diferida por falta de datos, anotada también en BACKLOG.md.

### Cost allocation tags — NO propagados aún (atribución fina diferida)

En la pasada, el valor tras `primate:environment_id$` vino VACÍO en todos los grupos → los tags
de asignación de costos aún no están activos/propagados (activación tarda hasta 24h, no es
retroactiva; es paso de admin del runbook, no del módulo). La verificación fina de atribución
por tag (que los `pcm_ref` aparezcan en el desglose) queda para una **segunda pasada** cuando
propaguen.
