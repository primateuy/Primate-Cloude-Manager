# Propuesta de diseño — Fase 8.5: DNS CRUD sobre Route 53

> Estado: **PROPUESTA, pendiente de aprobación. Cero código escrito.**
> Mini-fase posterior a la Fase 8 (respaldos + staging, cerrada). Fuentes: spec funcional §9,
> spec técnica §5.8, `ARCHITECTURE.md` (Fase 8.5), y el código actual de
> `primate.cloud.dns.record` + `services/aws_route53`.

## 0. Qué hay hoy y qué falta

- **Modelo `primate.cloud.dns.record`** (Fase 2): campos `name`, `account_id`,
  `environment_id`, `hosted_zone_id`, `record_type` (A/CNAME/TXT/MX), `record_value`, `ttl`,
  `state` (draft/active/deleted), `last_sync_date`. Constraint único
  `(account, zone, name, type)`. Solo tiene `_sync_from_aws` (lectura). **Sin acciones.**
- **`aws_route53`**: `list_zones`, `list_records`, `create_record` (¡ya hace `UPSERT`!).
  **Falta** `delete_record` y consulta de propagación (`get_change`).
- **Sync hoy** = a nivel cuenta (`account.job_sync_resources` itera zonas y hace upsert).
- **UI**: sección "Registros DNS" en el hub del entorno (solo listar + "Agregar" que abre un
  form nativo) y pantalla `dns_detalle` de solo lectura.
- **Provisión/staging** ya crean el registro A automáticamente (`_provision_dns` +
  `create_record`); eso NO se toca, se complementa con el CRUD manual.

## 0.1 Decisión de alcance de recursos (paralelo a "PCM no crea buckets")

**PCM gestiona REGISTROS dentro de zonas existentes, NO crea ni borra hosted zones.** La
zona (dominio) la administra un admin en Route 53 (igual que el bucket de respaldos). PCM
consume la `hosted_zone_id` ya existente. Crear/borrar zonas cambia la delegación NS del
dominio entero — fuera del alcance de v1, y sin `route53:CreateHostedZone` en la política.

---

## 1. Decisión — Modelo de sincronización (Route 53 es la fuente de verdad)

**PCM aplica el cambio y re-sincroniza para confirmar; nunca asume éxito.** Toda operación de
escritura va en `queue_job` (regla del proyecto): el método de acción valida, pone el registro
en `pending` y encola; el job llama a `change_resource_record_sets`, guarda el `ChangeId`
devuelto y **relee el registro real de Route 53** para confirmar el estado antes de marcar
`synced`.

**Divergencia — se reusa el patrón de trazabilidad de repos**, no se inventa uno nuevo. El
repo tiene `sync_state` (`unknown/updated/outdated/divergent/error`); DNS gana un
`sync_state` análogo con la semántica propia del dominio:

| `sync_state` | Significado |
|---|---|
| `unknown` | Sincronizado por inventario, aún no verificado activamente |
| `synced` | El valor/TTL en PCM coincide con el real en Route 53 |
| `pending` | Cambio aplicado, esperando propagación de Route 53 (ver abajo) |
| `divergent` | El registro real en AWS difiere del de PCM (alguien tocó la consola) |
| `error` | La última operación falló (mensaje en el chatter/log) |

La **detección de divergencia** cuelga del sync que ya existe: `dns.record._sync_from_aws`
(hoy hace upsert ciego) se ajusta para que, si el registro ya existe en PCM y el valor/TTL
real difiere, marque `divergent` en vez de pisar en silencio — el operador ve que AWS y PCM
no coinciden y decide (re-aplicar el de PCM o adoptar el de AWS). Un botón "Sincronizar este
registro" (`action_check_sync_state`, análogo al de repos) reevalúa uno puntual.

**Pendiente de propagación — sin mentir.** `change_resource_record_sets` devuelve un
`ChangeInfo` con estado `PENDING` que pasa a `INSYNC` cuando Route 53 propaga (segundos a
minutos). Se guarda `last_change_id`; el registro queda en `sync_state=pending` y un job de
seguimiento (encolado por el mismo job de cambio, con reintento acotado, o el cron de
verificación) consulta `get_change(ChangeId)` y recién con `INSYNC` pasa a `synced`. **Nunca
se muestra `synced` mientras Route 53 dice `PENDING`.** (Nota de testeo: moto siempre
responde `INSYNC` de inmediato; el estado `pending` real se cubre con un test unitario que
mockea `get_change`→`PENDING`, y opcionalmente la prueba real controlada.)

---

## 2. Decisión — Borrado (la operación peligrosa), protección nivel-restore

**No hay soft-delete-primero.** Como Route 53 es la fuente de verdad, marcar "borrado" en PCM
dejando el registro vivo en AWS sería una mentira. El flujo es: confirmar fuerte → borrar en
Route 53 (en `queue_job`) → marcar el registro PCM `state='deleted'` (se conserva la fila para
auditoría y trazabilidad, no se hace `unlink`) + `sync_state` acorde.

**Wizard de borrado en drawer** (`group_cloud_admin`, como toda escritura DNS según spec §9.1),
con la protección al nivel del restore de la Fase 8:
- **Tipear el nombre exacto del registro** (`confirm_name`) para habilitar el botón, validado
  **server-side** (patrón de `ec2.terminate.wizard` y `backup.restore.wizard`). No alcanza el
  estado del botón en el cliente.
- **Guardia de producción reforzada:** si el registro pertenece a un entorno
  `env_type='production'`, además del nombre se exige un `acknowledge` explícito ("entiendo
  que esto puede tirar abajo el dominio de producción `<name>`") y el alert del drawer lo
  marca en rojo. **No bloqueo duro** (un admin legítimamente necesita borrar registros de
  prod), pero fricción máxima.
- El botón lleva `confirm=` (ConfirmationDialog) ⇒ en producción: diálogo + nombre tipeado +
  acknowledge. Tres barreras.
- El borrado se **audita** en `operation.log` (`dns_delete`) con el registro, el valor que
  tenía y el `ChangeId`.

---

## 3. Decisión — Alcance de tipos de registro (v1)

**v1 = A, CNAME, TXT, MX** — exactamente los que la spec §9.2 lista y los que el modelo YA
declara (`SUPPORTED_RECORD_TYPES` y la Selection) y el sync ya maneja. **No se acota a solo
A/CNAME** (sería una regresión: el sync de Fase 2 ya trae TXT/MX y quedarían visibles pero no
editables). El modelo ya está preparado para ampliar (basta sumar a la Selection).

**Qué entra en v1:**
- Los 4 tipos, con **TTL editable** y **múltiples valores** (A con varias IPs, MX con varias
  prioridades, TXT con varias cadenas): el wizard toma valores separados por línea → lista a
  `change_resource_record_sets`. Route 53 guarda la prioridad de MX como parte del valor
  (`"10 mail.forum.cloud"`), sin campo aparte.
- Validación de forma por tipo (A = IPv4; CNAME = un solo valor y un hostname; TXT = se
  entrecomilla; MX = `prioridad host`) en el wizard, antes de encolar.

**Qué queda anotado FUERA de v1:**
- **AAAA, NS, SRV, CAA, PTR**: no en la spec; se suman a la Selection cuando haga falta.
- **Alias records de Route 53** (apuntar a ELB/CloudFront/S3 con `AliasTarget` en vez de
  `TTL`+`ResourceRecords`): forma distinta en la API, sin TTL. El `_normalize_record` ya los
  LEE para mostrar (Fase 2), pero **crear/editar alias queda fuera de v1** — es otra pantalla.
  Documentado como delta futuro.
- **Weighted/latency/geolocation routing policies**: fuera de alcance (v1 = simple routing).

---

## 4. Micro-deltas (lista explícita para aprobación)

### Servicio `aws_route53` (adaptador puro)
1. `delete_record(hosted_zone_id, name, record_type, value, ttl)` — `change_resource_record_sets`
   con `Action: DELETE` (Route 53 exige el RRSet exacto —nombre, tipo, TTL, valores— para
   borrar; se toma del registro PCM). Devuelve el `ChangeId`.
2. `get_change_status(change_id)` — `get_change` → `"INSYNC"` / `"PENDING"`.
3. (Opcional) `get_record(hosted_zone_id, name, record_type)` — relee un RRSet puntual para
   confirmar/comparar (o reusar `list_records` + filtro; se decide en el Bloque 1).
   `create_record` (UPSERT) ya existe → cubre crear y editar.

### Modelo `primate.cloud.dns.record`
4. `sync_state` (Selection `unknown/synced/pending/divergent/error`, `tracking=True`).
5. `last_change_id` (Char, readonly) — último `ChangeId` de Route 53.
6. `is_production` (Boolean compute, related de `environment_id.env_type == 'production'`) —
   para la guardia del borrado en la vista y el wizard.
7. Acciones: `action_open_edit` (abre wizard create/edit en drawer), `action_open_delete`
   (abre wizard de borrado), `action_check_sync_state` (re-sync puntual). Jobs:
   `job_apply_change(vals)` (UPSERT + confirmación), `job_delete(...)`,
   `job_poll_propagation(change_id)` (get_change hasta INSYNC, reintento acotado).
8. Ajuste de `_sync_from_aws`: al encontrar divergencia real→PCM, marcar `divergent` en vez
   de pisar (con `record_value_aws` mostrado para comparar).

### Bitácora
9. `operation.log.action_type` += `dns_update`, `dns_delete` (ya existe `dns_create`).

### Wizards nuevos (drawer, `group_cloud_admin`)
10. `primate.cloud.dns.record.wizard` — crear/editar (zona, nombre, tipo, valores, TTL) con
    validación por tipo.
11. `primate.cloud.dns.delete.wizard` — borrado con `confirm_name` + `acknowledge` de prod.

### Seguridad
12. `ir.model.access` de los wizards (admin). Las operaciones de escritura DNS: perfil
    **Admin** (spec §9.1) — el modelo ya da write/create/unlink a operator; se restringe la
    **acción** (los botones/wizards) a `group_cloud_admin` vía `groups=`.

---

## 5. IAM — permisos Route 53 nuevos para `pcm-operator`

Verificado contra la doc oficial de AWS (*Service Authorization Reference*, "Actions,
resources, and condition keys for Amazon Route 53"). Route 53 es **global**: los ARN no
llevan región ni cuenta (`route53:::`) y el ID de zona va **sin** el prefijo `/hostedzone/`.

Resource-level: `ChangeResourceRecordSets` y `ListResourceRecordSets` se restringen por zona;
`ListHostedZones` **obliga** `Resource: "*"`; `GetChange` se restringe por `change/*`.

```json
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
}
```

> Se puede acotar `hostedzone/*` a las zonas concretas del cliente
> (`arn:aws:route53:::hostedzone/Z0123...`) si se quiere menor superficie; `*` es lo práctico
> cuando el cliente tiene varias zonas. Estos statements se suman al `PcmS3Backups`/EC2/SSM
> ya existentes; van al **runbook de onboarding** (`RUNBOOK_ONBOARDING.md`). El rol
> `pcm-ssm-role` NO necesita nada de Route 53 (el CRUD corre desde Odoo, no desde la EC2).

---

## 6. UI en el hub (sin modales stock, drawer + drill-through)

- **Sección "Registros DNS" del detalle de entorno** (`entorno_detalle`):
  - "Agregar registro" abre el **wizard create/edit en el drawer** (`env.pcm.openWizard`),
    no el form nativo actual.
  - Cada fila gana: badge de `sync_state` (con el widget `pcm_status_badge` — hay que sumar
    los buckets de color `synced/pending/divergent` a `pcm_status.js`), botón **editar**
    (lápiz → mismo wizard precargado) y **borrar** (tacho → wizard de borrado). Drill-through
    al `dns_detalle` que ya existe.
  - Si el registro está `divergent`, la fila lo marca y ofrece "Ver diferencia" (valor PCM vs
    AWS) y "Re-sincronizar".
- **Pantalla `dns_detalle`**: gana los mismos tres botones (editar/borrar/verificar) en la
  barra de acciones + el `sync_state` y `last_change_id` en la cabecera. El serializer
  `get_dns_detail` suma `sync_state`, `record_value_aws` (si divergente), `is_production`.
- **Vista nativa** (`primate_cloud_dns_record_views.xml`): botones de acción en el form
  (hoy es solo lectura), columna `sync_state` en la lista.

---

## 7. Plan por bloques (orden y testeo)

| # | Bloque | Contenido | Testeo |
|---|---|---|---|
| 1 | Servicio + modelo | `aws_route53.delete_record`/`get_change_status`; campos `sync_state`/`last_change_id`/`is_production`; action_types; ajuste de `_sync_from_aws` (divergencia) | **moto** (create/upsert/delete/get_change end-to-end); TransactionCase: recompute de `sync_state`, detección de divergencia, validación de forma por tipo |
| 2 | Jobs + propagación | `job_apply_change`/`job_delete`/`job_poll_propagation`; re-sync de confirmación; cron/botón de verificación puntual | mocks + **moto**; **unit con `get_change`→`PENDING`** (moto siempre da INSYNC) para ejercitar el estado `pending`; casos synced/divergent/error |
| 3 | Wizards + UI | wizard create/edit + wizard delete (confirm-name + prod guard); sección DNS del hub con drawer/editar/borrar; `dns_detalle` con acciones; buckets de color nuevos | TransactionCase de los wizards (validación, prod guard server-side); **smoke_ui**: crear/editar/borrar en drawer (cancelar), badge de estado |
| — | **Prueba real controlada** (recomendada) | Sobre una **hosted zone de test real**: crear un registro → verificar en Route 53 (`get_change` INSYNC + `dig`) → modificar → forzar divergencia tocando la consola y ver que PCM lo detecta → borrar con el wizard → confirmar que desapareció → limpiar | manual guiada, cuenta de pruebas |

**Por qué una prueba real y no solo moto:** moto cubre la forma de la API (create/delete/
get_change) pero **siempre responde `INSYNC`**, así que oculta la realidad de la propagación
(`PENDING`→`INSYNC`) y no valida que un registro creado por PCM realmente resuelva por DNS ni
que la detección de divergencia funcione contra un cambio hecho fuera de PCM. DNS es
user-facing y borrar mal tira un dominio: conviene una pasada real acotada (un registro de
prueba en una zona de test, crear/modificar/divergir/borrar/limpiar), como se hizo con EC2 y
respaldos. Es barata (Route 53: ~USD 0.50/zona/mes + fracciones de centavo por query) y no
levanta infra que facture por hora.

---

## 8. Contradicciones / notas contra el código actual

- **Spec §5.8** define `dns.record.state = draft/active/deleted` (ya en el modelo). Se
  **mantiene** como ciclo de vida en PCM; el `sync_state` que se agrega es **otra dimensión**
  (acuerdo con AWS), no lo reemplaza. Delta explícito.
- **Spec §9.1** marca las 3 operaciones como perfil **Admin** → las acciones/wizards se
  restringen a `group_cloud_admin` (el modelo ya da CRUD a operator a nivel ORM; se gatea la
  UI de acción).
- **`create_record` ya es UPSERT** (Fase 4): sirve para crear y editar sin método nuevo; solo
  falta `delete_record`. No duplicar.
- **Alias records**: el `_normalize_record` ya los lee (Fase 2) y podrían aparecer en la lista
  como registros con valor pero sin TTL editable; el wizard de edición debe **detectarlos y
  bloquear la edición** en v1 (con nota "registro alias, gestionar en consola"), para no
  romper un alias al reescribirlo como registro simple.
- **Contradicción con "PCM no crea buckets"** → paralelo resuelto: **PCM no crea/borra zonas**,
  solo registros dentro de zonas existentes (§0.1).
