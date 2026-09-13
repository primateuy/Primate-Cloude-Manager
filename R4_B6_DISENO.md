# R4-B6 — Hub: pantalla SERVIDOR vs pantalla INSTANCIA + DNS best-effort

> **DISEÑO. Cero código. FRENO al final.** Último bloque de UI del recableo: partir el hub que
> hoy muestra "el servidor y su Odoo" mezclados en una sola pantalla (`get_server_detail` +
> `servidor_detalle.js` con `PCM_PATHS` hardcodeado) en dos pantallas honestas — la MÁQUINA y
> la INSTANCIA — y retirar los shims que B1–B5 dejaron para no romper la UI mientras se
> recableaba el backend. Sobre el código real (serializers y JS auditados).

---

## 0. Punto 1 del pedido — RETIRAR los shims, no dejarlos convivir

Tres shims sobreviven al recableo del backend y **mueren en B6** (con test que fija que ya no
existen). Todos comparten el mismo pecado: **resuelven "la primaria"**, la suposición que el
recableo mató.

| Shim | Dónde | Qué asume | Se reemplaza por |
|---|---|---|---|
| **A — `PCM_PATHS`** | `servidor_detalle.js:11-19` + `get paths()` (`:185`) + `shellCommands` (`:196`) | rutas legacy `/opt/odoo`, unit `odoo`, `databases[0]` | rutas del backend: la pantalla de instancia sirve `conf_path/service_name/python_bin/odoo_bin/log_path/addons_dir` desde `get_odoo_instance_detail`; los comandos copy-paste se arman con ESOS |
| **B — wrappers dashboard por EC2-id** | `dashboard.py:449-575` (`get_instance_logs/…_stream/get_instance_config/save_instance_config/list_instance_db_users/login_as/add_instance_addon`) reciben un id de **`primate.cloud.ec2.instance`** y llaman `inst.fetch_config()` (ec2) → resuelve primaria | operar "el Odoo del servidor" | wrappers nuevos que reciben un id de **`primate.cloud.instance`** y llaman la API canónica que B2/B3 pusieron en el modelo instancia (`instance.fetch_config()`, `instance.action_login_as()`, …) |
| **C — `_panel_target(odoo_instance=None)`** | `ec2_instance.py:262` default a `environment_id.primary_instance_id` | "sin instancia = la primaria" | el parámetro pasa a **REQUERIDO**: todo llamador declara la instancia. La única resolución de primaria que queda es la del `deployment._target_odoo_instance` (que es un fallback legítimo con su propia lógica), no un default silencioso del panel |

**Tests del retiro (el inverso, como en cada bloque):**
- `PCM_PATHS` no existe en el JS (grep-guard en el smoke o un `node --check` sobre el archivo
  + assert de ausencia del literal).
- Los métodos dashboard `get_instance_*` viejos (por EC2-id) NO existen; los nuevos exigen un
  id de `primate.cloud.instance`.
- `_panel_target()` sin `odoo_instance` **levanta** (o el método deja de aceptar el default) —
  fija que ningún camino del panel vuelve a resolver la primaria a ciegas.

⚠️ **Regla del bloque:** ningún shim sale de B6 "por las dudas". Dos caminos para lo mismo =
el de compat envejece resolviendo la primaria y alguien lo usa sin darse cuenta.

---

## 1. Punto 2 del pedido — `env_type` a nivel SERVIDOR: el badge que MIENTE

Hoy `get_server_detail` devuelve `is_production = env.env_type == "production"` (sale de la
primaria). En un servidor compartido que hospeda la **prod de X** y el **staging de Y**, ese
badge es una mentira: dice "producción" sobre una máquina que es mitad y mitad. No es un gate
(esos ya miran la instancia, B5-audit), pero **induce a error operativo**.

**Decisión (D-B6.1): el servidor NO tiene `env_type`.** La propiedad es de la instancia, no de
la máquina. En la pantalla de servidor:
- **Se retira** el badge de "producción" a nivel servidor (y el campo `is_production` del
  serializer de servidor).
- La **lista de instancias hospedadas** muestra el `env_type` chip de CADA instancia (esa es la
  verdad).
- Un **resumen honesto** arriba de la lista: `"Hospeda: 1 producción · 2 staging"` (contadora
  derivada de las instancias no archivadas). Si hay ≥1 producción, un realce sutil en el
  resumen (no un badge "producción" sobre la máquina) para que la operación sepa que ahí vive
  algo sensible — sin afirmar que la máquina "es" producción.

Esto cierra el "badge que miente" sin inventar un env_type de servidor.

---

## 2. Punto 3 del pedido — el corte servidor vs instancia (lo que ve Daryl)

Dos serializers y dos pantallas. El `get_server_detail` mega-mezclado se PARTE.

### 2.1 Pantalla SERVIDOR (la MÁQUINA) — `get_server_detail` reescrito

Solo lo de la máquina + navegación a sus instancias.

- **Identidad/infra:** IP pública/privada, tipo EC2, estado de la máquina (running/stopped/…),
  región, SG/subnet, disco, AMI/OS, `provisioned_by_pcm`, fecha de creación AWS, último sync.
- **Costo crudo** del servidor (el `cost.entry` por servidor de Fase 9 — se muestra tal cual;
  el reparto por instancia es R5, fuera de R4).
- **Acciones de máquina:** Start / Stop / Restart / **Terminate** (EC2), Sincronizar desde AWS.
- **Instancias hospedadas** (la lista): por cada instancia no archivada — nombre, **cliente
  (project)**, `env_type` chip, estado del Odoo, dominio, puerto, link a su pantalla de
  instancia. Más el resumen §1.
- **Bootstrap multi-Odoo** (estado `multiodoo_ready`) + botón "Agregar instancia" (el wizard de
  R3-B3, ya existe).
- **Se van de acá** (pasan a la instancia): config, logs de Odoo, addons, login-as, backups,
  repos, runtime probe, `is_production`, `main_url`, backup_compliance.

### 2.2 Pantalla INSTANCIA (el ODOO) — `get_odoo_instance_detail` (nuevo)

Todo lo del panel de la Fase B, que **siempre fue de la instancia** aunque colgara de la
máquina. Recibe un id de `primate.cloud.instance`.

- **Identidad:** nombre, cliente (project), `env_type` chip, versión/edición Odoo, dominio,
  puerto HTTP/gevent, unit systemd, estado del Odoo, servidor donde vive (link a §2.1).
- **Rutas** (para los comandos copy-paste, reemplazo del `PCM_PATHS`): `conf_path`,
  `service_name`, `python_bin`, `odoo_bin`, `log_path`, `addons_dir`, `data_dir`, `pg_user`.
- **BD** de la instancia (`database_id` + las montadas).
- **Config** (B3, por `conf_path` de la instancia), **Logs** (B2, `log_path`/unit propios),
  **Addons/Repos** (B4/B2, `addons_dir` propio), **Backups** (por la BD de la instancia),
  **Login-as** (B3, claves/URL/pcm_ref propios), **Staging** (crear/refrescar sobre esta
  instancia), **DNS** (incl. `dns_pending`, §3).
- **Deploys** de la instancia.

### 2.3 Confirmación destructiva del servidor — "Terminar" dice a QUIÉNES se lleva puestos

`Terminate` en el servidor mata la EC2 y con ella **todas** las instancias que hospeda — de
varios clientes. La confirmación **no puede ser el "¿seguro?" genérico** de antes.

**Decisión (D-B6.2):** la confirmación de terminar el servidor:
1. **Lista explícita** de lo que se destruye: cada instancia no archivada con **su cliente** y
   su dominio (`"Vas a terminar 3 Odoo: Forum Prod (cliente A), Tienda (cliente B), Blog
   staging (cliente A)"`).
2. **Realce si hay producción** entre ellas (`"⚠ incluye 1 de PRODUCCIÓN"`).
3. **Tipear el nombre del servidor** para confirmar (patrón restore/config), no solo un
   checkbox — porque el radio de daño es multi-cliente.
4. Sigue registrando en bitácora (ya lo hace) con el detalle de las instancias afectadas.

El wizard de terminación de EC2 ya existe (doble confirmación admin); B6 le **suma la lista de
instancias afectadas + el conteo de producción** al cuerpo, y el tipeo del nombre del servidor.

### 2.4 Gobierno de Terminate cross-cliente (D-B6.5 — pregunta nueva del modelo compartido)

El modelo compartido introduce un problema de **autorización**, no solo de fricción: hoy un
operador del proyecto X puede terminar la EC2 que hospeda la **producción del cliente Y** solo
por compartir máquina. La fricción de §2.3 (listar + realzar prod + tipear) reduce el accidente,
pero **no es un control de autorización** — un operador decidido igual puede tumbar a un tercero.

> **⚠ D-B6.5 CORREGIDA (hallazgo del usuario, R4-B6.3).** La regla de abajo asumía que se
> puede saber en runtime si un servidor es "del operador". **No se puede: el modelo NO tiene
> asignación operador→cliente** — `project.partner_id` es la EMPRESA cliente (no un empleado
> de Primate), y no existe campo de operador responsable. Un chequeo
> `user.partner_id == project.partner_id` da SIEMPRE cross-cliente para un operador real →
> "admin-only siempre" disfrazado de imperativo frágil. **Resolución: Terminate es admin-only
> DECLARATIVO (ACL), sin excepción** — control por grupo, no por un chequeo que siempre da
> True. El wizard igual muestra el radio de daño (afectados + producción), que le sirve al
> admin. Si algún día Primate agrega asignación operador→proyecto, se reevalúa la regla de
> abajo con ese dato REAL. (La regla de abajo queda como registro de la intención descartada.)

**~~Decisión (D-B6.5): Terminate de un servidor con proyectos AJENOS al del operador es
admin-only.~~** ~~Regla:~~
- Si TODAS las instancias no archivadas del servidor pertenecen al/los proyecto(s) del operador
  (o el servidor es dedicado a su cliente) → alcanza la fricción de §2.3 (operator+).
- Si el servidor hospeda **al menos un proyecto que no es del operador** → **solo
  `group_cloud_admin`** puede terminar (además de la fricción). Un operador de X no puede matar
  la máquina que también corre lo de Y; lo escala a un admin.
- La confirmación admin, además, hace **ack por cada CLIENTE afectado** distinto (no por
  instancia): "afecta a: cliente A, cliente B" — el admin reconoce explícitamente el radio
  multi-cliente.
- Validación **server-side** (no solo ocultar el botón): `action_terminate` del servidor
  chequea el grupo contra el conjunto de proyectos hospedados; un operador que llegue por API
  igual es rechazado.

Esto vale para **Terminate** (destruye la máquina y todas las instancias). Stop/Restart del
servidor también afectan a todos pero son **reversibles** → se quedan en operator+ con la
advertencia de "afecta a N instancias de M clientes" (sin admin-only). Terminate es el único
irreversible con radio multi-cliente, y por eso el único que sube a admin.

---

## 3. Punto 4 del pedido — DNS best-effort §8.1, las 3 condiciones

Hoy `job_add_instance` (env `:1184-1192`), si Route53 falla, deja la instancia activa y hace
`message_post` de advertencia — pero la razón y los parámetros (hosted zone, ttl) se pierden en
el chatter. B6 los **persiste** y da el reintento aislado.

**Campo nuevo (D-B6.3): `instance.dns_pending`** (Char/JSON, `copy=False`): al fallar el DNS en
`job_add_instance`, se guarda `{"reason": <error>, "hosted_zone_id": …, "ttl": …, "domain": …,
"record_type": "A"}`. Vacío = sin DNS pendiente.

Las tres condiciones (§8.1 del diseño R3):
1. **Advertencia visible:** badge/alerta `dns_pending` en la pantalla de instancia (no solo
   chatter) — muestra el motivo del fallo.
2. **Accesible mientras tanto:** con `dns_pending` activo, la pantalla muestra el acceso por
   **IP + header Host** (hint `curl -H "Host: <dominio>" http://<ip>/`) — la instancia YA
   responde por nginx, solo falta el registro DNS.
3. **Reintento aislado:** botón **"Crear DNS ahora"** → `instance.action_retry_dns` → reusa
   `environment._provision_dns(..., instance=self)` con los params guardados en `dns_pending`,
   **sin re-correr ningún install** (cero riesgo de `PCM_ERR_DIRTY_SLUG`). Éxito → limpia
   `dns_pending` + crea el registro `primate.cloud.dns.record` real (atado a la instancia).
   Fallo → refresca el motivo en `dns_pending` (sigue reintentable).

`action_retry_dns` corre como job (muta Route53) con `queue_job`, patrón botón→job.

---

## 4. Frontend (OWL) — qué cambia

- `screens/servidor_detalle.{js,xml}` → **pantalla SERVIDOR** pura (máquina + lista de
  instancias). Muere `PCM_PATHS`; los comandos copy-paste se mueven a la pantalla de instancia
  y salen del backend.
- `screens/instancia_detalle.{js,xml}` (**nuevo**) → pantalla INSTANCIA (todo el panel B por
  `primate.cloud.instance` id). Reusa los componentes de badge/chip existentes.
- Router del hub (`pcm_app.js`): la lista de instancias del servidor y el drill-through de
  proyecto/BD/backup abren la pantalla de instancia. `DETAIL_TYPES` gana `odoo_instance`.
- Los wrappers `env.pcm.*` (openWizard/newRecord/…) no cambian; cambian los serializers y las
  llamadas ORM.

---

## 5. Deuda que B6 cierra (anotada en B5)

- **Refresh de staging por-instancia:** el staging montado como instancia (B5) no tenía UI de
  refresco (el `job_refresh_staging` es env-based). B6 cablea el refresco desde la pantalla de
  la instancia staging: reusa `_staging_source_backup` + `job_restore_backup` (ya por
  instancia) leyendo `instance.origin_instance_id` (el que B5 dejó bien, no el `[:1]`).

### 5.1 Stagings VIEJOS y `origin_instance_id` vacío (D-B6.6 — mismo cuidado que el s3_bucket)

`instance.origin_instance_id` está bien poblado para los stagings creados por B5. Pero hay dos
poblaciones con el campo **vacío o dudoso**:
- **Stagings env-based** (Fase 7/8): el staging es un ENTORNO; su verdad de origen vive en
  `environment.staging_origin_instance_id` / `staging_origin_database_id` (EC2 + BD), no en la
  instancia. La primaria de ese staging tiene `origin_instance_id` seteado por R1 a la primaria
  del origen — que puede ser la suposición `[:1]` si el origen era multi.
- **Migrados por R1** con el campo vacío.

**Decisión (D-B6.6): resolución con fallback explícito, sin adivinar la primaria.**
`instance._resolve_refresh_origin()` devuelve `(origin_instance, origin_database)`:
1. **Instance-based (B5):** `self.origin_instance_id` seteado → usar esa instancia y su
   `database_id`. (Verdad nueva, inequívoca.)
2. **Env-based (Fase 8):** si `origin_instance_id` vacío pero el staging es un entorno con
   `staging_origin_database_id` → derivar la instancia origen de **`staging_origin_database_id
   .instance_id`** (la BD lleva su instancia por el mixin R1 — el dato correcto, no la
   primaria). El `staging_origin_instance_id` (EC2) queda como ejecutor.
3. **Ninguno resoluble:** **fail claro** ("este staging no tiene un origen registrado: indicá
   la instancia de origen"), NUNCA fallback a la primaria del origen (ese es justo el `[:1]`
   que matamos).

**Sin backfill masivo** (como con el s3_bucket): la resolución en runtime cubre los viejos sin
tocar datos; si un staging viejo no resuelve, el refresh lo dice y el operador elige — no se
adivina. (Un backfill opcional del `origin_instance_id` desde `staging_origin_database_id
.instance_id` se puede correr después si se quiere limpiar, pero no es requisito de B6.)

---

## 6. Tests

- **Retiro de shims (§0):** los tres inversos (PCM_PATHS ausente, dashboard por instancia-id,
  `_panel_target` sin default).
- **Serializers partidos:** `get_server_detail` no trae config/logs/is_production;
  `get_odoo_instance_detail` trae rutas/config/backups/dns_pending por instancia.
- **env_type servidor (§1):** el serializer de servidor NO trae `is_production`; trae el
  resumen "N prod · M staging" derivado de las instancias.
- **Terminar servidor (§2.3):** la confirmación lista las instancias afectadas + conteo de
  producción; exige el nombre del servidor.
- **DNS best-effort (§3):** `dns_pending` se puebla al fallar el DNS en `job_add_instance`;
  `action_retry_dns` reusa los params y NO corre install (mock: solo Route53), éxito limpia el
  flag y crea el record; el hint IP+Host aparece con dns_pending activo.
- Smoke UI del hub (servidor→instancia→drill-through) si el harness lo permite.

---

## 7. Decisiones a aprobar (D-B6.1 … D-B6.4)

| # | Decisión | Propuesta |
|---|---|---|
| D-B6.1 | El servidor NO tiene `env_type`; se retira el badge "producción" de la máquina; la verdad vive en cada instancia + resumen "N prod · M staging" | **Sí** |
| D-B6.2 | Terminar servidor: confirmación lista las instancias afectadas (con cliente) + realce si hay producción + tipear el nombre del servidor | **Sí** |
| D-B6.3 | `instance.dns_pending` (JSON) + `action_retry_dns` (job, reusa `_provision_dns`, sin install) | **Sí** |
| D-B6.4 | Retirar los 3 shims en este bloque (PCM_PATHS, dashboard por EC2-id, default-primaria de `_panel_target`) con test del inverso | **Sí** |
| D-B6.5 | Terminate de servidor con proyectos AJENOS = admin-only (server-side) + ack por cliente afectado; Stop/Restart quedan operator+ (reversibles) | **Sí** |
| D-B6.6 | Refresh resuelve el origen: instancia (B5) → `staging_origin_database_id.instance_id` (env-based) → fail claro; nunca la primaria. Sin backfill masivo | **Sí** |

## 8. Bloques de implementación (tras aprobar)

| # | Sub-bloque | Testeo |
|---|---|---|
| B6.1 | Serializers partidos (`get_server_detail` máquina-only + `get_odoo_instance_detail`) + retiro shim C (`_panel_target` requerido) | serializers + `_panel_target` levanta |
| B6.2 | Wrappers dashboard por instancia-id (retiro shim B) + pantalla INSTANCIA OWL (retiro shim A: PCM_PATHS→backend) | dashboard por instancia-id, PCM_PATHS ausente |
| B6.3 | Pantalla SERVIDOR reescrita (máquina + lista + resumen env_type) + confirmación de Terminar con lista de afectados | serializer sin is_production, confirmación |
| B6.4 | `dns_pending` + `action_retry_dns` + las 3 condiciones en la pantalla de instancia + refresh staging por-instancia | DNS best-effort + refresh |

---

**FRENO.** Espero revisión de D-B6.1…D-B6.4 antes de escribir código. Orden: B6.1 → B6.2 →
B6.3 → B6.4. Después, **R4-B7** (pruebas reales, con el caso cruzado de impersonación de B5).
