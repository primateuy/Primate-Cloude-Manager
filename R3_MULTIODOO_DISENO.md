# R3 — Install multi-Odoo: diseño fino

> **DISEÑO. Cero código. FRENO al final.** La pieza técnica más difícil del recableo
> (RECABLEO_PLAN.md §5): montar N Odoo en un servidor sin que el install de uno toque a los
> que ya corren. Diseñado sobre el `install_odoo.sh` REAL (147 líneas, leído línea por línea),
> no de memoria. Las dos reglas permanentes aplican en el centro del diseño: health-check
> antes/después de TODO lo vivo, y ningún control de seguridad leído de caché.

---

## 0. Alcance y NO-alcance

**Entra:** partir el install en `bootstrap_server.sh` (una vez por servidor) +
`install_instance.sh` (una vez por instancia/slug); asignación de puertos; lock por servidor;
health-check + rollback; wizard real de "Agregar instancia"; la cadena de crear-entorno pasa a
usar el layout nuevo; prueba real 2-Odoo + rollback inducido.

**NO entra (anotado, no tapado):**
- **Adopción in-place de servidores legacy** (pre-R3, layout `/opt/odoo` + unit `odoo`): sus
  instancias siguen funcionando por los campos por-instancia de R1 (`conf_path/service_name/
  http_port`), pero **agregarles una segunda instancia queda gated** (el gate cambia de motivo:
  "servidor con layout legacy"). La adopción es una mini-fase posterior si hace falta (hoy no
  hay servidores legacy en producción — no existe producción).
- Recablear panel/backups/staging a los paths nuevos: **R4** (los campos ya existen; acá solo
  se anotan las costuras, §8).
- Usuarios unix por instancia: **v2** (D-R4 del plan; en v1 el aislamiento es a nivel
  aplicación — §5 lo fija con test golden).

---

## 1. Layout destino en disco

```
/opt/pcm/
├── .bootstrap-v1                    ← marker versionado del bootstrap
├── runtime/
│   └── odoo-19/                     ← COMPARTIDO por versión (D-R5 aprobada):
│       ├── src/                       git clone --depth 1 -b 19.0 (una vez)
│       └── venv/                      venv + requirements (una vez)
└── instances/
    └── <slug>/
        ├── data/                    ← data_dir (filestore + sessions) PROPIO
        ├── addons/                  ← custom addons PROPIOS (B4 del panel → R4)
        └── log/odoo.log             ← log PROPIO (+ logrotate propio)

/etc/odoo/<slug>.conf                ← conf PROPIO (640, root:odoo)
/etc/systemd/system/odoo-<slug>.service
/etc/nginx/sites-available/pcm-<slug>.conf   ← ¡nombrado por SLUG, no por dominio!
```

- **Sitio nginx nombrado por slug** (`pcm-<slug>.conf`), no por dominio como hoy: la propiedad
  de cada artefacto es determinista → el rollback sabe EXACTAMENTE qué borrar aunque el
  dominio cambie o esté repetido.
- El runtime compartido **jamás** se toca en un rollback de instancia.
- Usuario unix: `odoo` compartido (D-R4 v1); todo lo del slug con owner `odoo:odoo`.

## 2. `bootstrap_server.sh` — una vez por servidor (idempotente, re-ejecutable)

Se lleva del script actual las partes de MÁQUINA (hoy líneas 40-67): apt deps + aws-cli snap +
usuario `odoo` + **PostgreSQL siempre** (decisión D-R3.4: también en servers que usen RDS —
simplifica; un postgres idle es aceptable en v1) + nginx base. Suma:

- **Swapfile** (D-R3.7): 2 GB si no existe y la RAM < 4 GB — obligatorio para multi-Odoo en
  t3.micro (1 GB). `swappiness=10`.
- Layout `/opt/pcm/{runtime,instances}`.
- `rm -f /etc/nginx/sites-enabled/default` (seguro acá: el bootstrap solo corre en servidores
  nuevos o ya multi-Odoo, nunca sobre un legacy vivo).
- Marker `/opt/pcm/.bootstrap-v1` al final (el job lo chequea para no re-correr; si en el
  futuro el bootstrap cambia, se versiona el marker y se re-corre el delta).

**NO precarga runtimes de Odoo** (D-R3.5): el runtime por versión lo instala on-demand el
primer `install_instance.sh` que lo necesite (`ensure_runtime`, con `flock` sobre
`/opt/pcm/runtime/.lock-<ver>` como red local). Bootstrap queda chico, fijo y rápido.

Tokens: `%%SWAP_MB%%` (default 2048). Nada por-instancia.

## 3. `install_instance.sh` — una vez por instancia (slug)

Tokens: `%%SLUG%%`, `%%ODOO_VERSION%%`, `%%ODOO_EDITION%%`, `%%HTTP_PORT%%`,
`%%GEVENT_PORT%%`, `%%WORKERS%%`, `%%DB_HOST%%`, `%%DB_PORT%%`, `%%DB_NAME%%`, `%%PG_USER%%`,
`%%PG_PASSWORD%%`, `%%DB_LOCAL%%`, `%%DOMAIN%%`, `%%ADMIN_PASSWORD%%`, `%%IS_DEFAULT%%`.

Flujo (cada paso levanta bandera `CREATED_*` para el rollback §4):

1. **Guardas**: marker de bootstrap presente (si no → `PCM_ERR_NO_BOOTSTRAP`, error accionable);
   puerto HTTP y gevent LIBRES (`ss -ltn`, belt & suspenders del allocator §6); df de disco;
   slug sin residuos (si hay dir/unit/site del slug de una corrida rota → `PCM_ERR_DIRTY_SLUG`,
   se resuelve con el rollback explícito, no pisando).
2. **`ensure_runtime <ver>`**: si falta `/opt/pcm/runtime/odoo-<ver>` → clone + venv +
   requirements (bajo flock). Compartido: la 2ª instancia 19 NO recompila nada.
3. **Snapshot de vecinos** (defensa en el script, además del health del job §4): captura
   `systemctl is-active` + `curl 127.0.0.1:<puerto>` de TODAS las units `odoo-*` existentes.
4. **PostgreSQL**: usuario PROPIO `%%PG_USER%%` (= `odoo_<slug>`, D-R3.10) `WITH CREATEDB
   PASSWORD ...` — cada instancia con su credencial; la BD la crea Odoo al primer arranque
   (igual que hoy). La contraseña es transitoria estilo `fe_sendauth`-fix: se genera al
   encolar, viaja en los args del job, vive solo en el conf de SU instancia.
5. **Dirs + conf**: `/etc/odoo/<slug>.conf` con `http_port`/`gevent_port`/`workers` propios,
   `data_dir` propio, `logfile` propio, `addons_path = runtime/src/addons + instances/<slug>/
   addons`, `proxy_mode = True` **y el candado multi-tenant obligatorio**:
   `db_filter = ^%%DB_NAME%%$` + `list_db = False` (§5).
6. **Unit** `odoo-<slug>.service` (`After=postgresql`), `daemon-reload`, `enable --now`.
   **Jamás** escribe un unit cuyo nombre no sea el de su slug.
7. **nginx**: sitio `pcm-<slug>.conf` con `server_name %%DOMAIN%%` → `proxy_pass
   127.0.0.1:%%HTTP_PORT%%` (+ ruta websocket al gevent). `default_server` SOLO si
   `%%IS_DEFAULT%%` (la primera instancia del servidor responde al acceso por IP cruda, como
   hoy). **`nginx -t` SIEMPRE antes de `systemctl reload nginx` — nunca restart.**
8. **Salud de la nueva**: poll a `127.0.0.1:%%HTTP_PORT%%/web/login` hasta responder (timeout
   ~120s; la BD se crea en el primer arranque, tarda).
9. **certbot** por dominio, best-effort con advertencia (igual que hoy, D-R3.9).
10. **Re-verificar vecinos** del paso 3: si alguno dejó de responder → `PCM_ERR_NEIGHBOR_DOWN`
    → rollback + exit 1. (Con `reload` y artefactos por slug no debería pasar NUNCA; si pasa,
    es un bug que queremos ver rojo, no tapar.)

## 4. Garantía de no-romper: health-check + rollback

**Del lado PCM (el job, antes de tocar el servidor):**
- `environment._instances_health_snapshot()`: HTTP desde afuera a cada instancia ACTIVA del
  servidor (por su dominio vía la IP del server, header Host correcto). Patrón `PCM_HTTP_WAS`
  de B3: lo que servía ANTES tiene que servir DESPUÉS; lo que ya estaba roto antes no bloquea
  (no le achacamos al install un muerto previo).
- Después del script: re-snapshot. Vivo-antes y muerto-después → el job marca `failed`,
  dispara el rollback (si el trap no lo hizo) y lo dice con nombre y apellido en la bitácora.

**Rollback = desmontar SOLO lo creado en ESTA corrida** (D-R3.3, clean-slate):
`trap` en el script con las banderas `CREATED_*`; orden: stop+disable+rm unit → rm site nginx
+ `nginx -t` + reload → `dropdb --if-exists` **solo si la BD la creó esta corrida** (instancia
nueva = BD nueva vacía; JAMÁS se dropea una BD preexistente) → drop del pg_user propio → rm
dirs del slug. El runtime compartido y TODO lo ajeno quedan intactos. El retry tras rollback =
re-run limpio del mismo job (mismos args → misma contraseña; `action_retry_install` de R2 ya
cubre el re-encolado).

## 5. Aislamiento multi-tenant v1 (a nivel aplicación — explícito)

- `db_filter = ^<db>$` y `list_db = False` en el conf de CADA instancia: una instancia no ve
  ni lista las BD de las otras. **Test golden que lo FIJA** (si un cambio futuro lo pierde, la
  suite se pone roja).
- `data_dir` propio → filestore separado. Credencial PG propia por instancia (D-R3.10): el
  conf de A no sirve para conectarse a la BD de B.
- Recordatorio de D-R4 (aprobada con precisión): esto es aislamiento de APLICACIÓN, no de SO
  (usuario unix compartido). Antes de poner dos clientes con datos sensibles en un mismo
  servidor: cerrar v2 (usuarios por instancia). Va también al RUNBOOK.

## 6. Puertos: asignación por slots

- **Asigna PCM** (no el script): `environment._allocate_ports()` → primer slot libre de la
  secuencia `(8069+10k, 8072+10k)` mirando las instancias del servidor (terminadas/archivadas
  no liberan su slot en v1 — simple y sin colisiones con units residuales). Persistido en la
  instancia AL CREARLA (wizard/enqueue); la constraint `UNIQUE(environment_id, http_port)` de
  R1 es la red de atrás; el `ss -ltn` del script, la de adelante (puertos ocupados por fuera
  de PCM).
- Slot de a 10 (D-R3.8): deja aire para servicios auxiliares futuros por instancia.

## 7. Concurrencia: lock por servidor

Dos "agregar instancia" simultáneos sobre el MISMO servidor se serializan con advisory lock
por servidor (`pg_advisory_lock(env.id << 32 | crc32('instance_install'))`), patrón sesión +
unlock en `finally` del fix de B3 del SG (con la guarda de unlock sobre transacción abortada).
Servidores distintos no se bloquean entre sí. El `flock` del runtime (§3.2) cubre la ventana
residual dentro de la máquina.

## 8. Lado Odoo: jobs, wizard y cadena

- **`job_bootstrap_server()`** (mutación → queue_job): corre `bootstrap_server.sh` por SSM si
  falta el marker; escribe `environment.multiodoo_ready = True` (campo nuevo Boolean; el
  marker en disco es la verdad, el campo es caché de UI — y por la regla permanente, cualquier
  decisión de seguridad se re-verifica contra el marker, no contra el caché).
- **`job_add_instance(instance_id, params)`** (nuevo): lock §7 → ensure bootstrap → snapshot
  salud §4 → `install_instance.sh` por SSM → re-snapshot → instancia `active` + bitácora
  (action_type nuevo `instance_install`). La instancia se crea en `draft` desde el wizard con
  puertos/slug/pg_user ya asignados, pasa a `installing` al arrancar el job.
- **La cadena de crear-entorno (R2) adopta el layout nuevo**: `job_install_instance` pasa a
  ejecutar bootstrap+install_instance (D-R3.1: los servidores NUEVOS nacen multi-Odoo desde su
  primera instancia; `install_odoo.sh` queda solo como referencia histórica de los servers
  legacy). La firma, el resume y el retry de R2 no cambian.
- **Wizard `primate.cloud.instance.create.wizard`**: servidor fijo (del contexto), proyecto
  (required — el 2º Odoo puede ser de OTRO cliente: acá se materializa el server compartido),
  nombre, versión/edición, dominio, nombre de BD (único por servidor, validado), contraseña
  admin, workers (default 0, D-R3.6). Muestra los puertos asignados y una advertencia de RAM
  (nº de instancias vivas vs memoria del tipo de máquina — advisoria, no bloqueante en v1).
- **El gate de R2 se reemplaza**: `action_create_instance` abre el wizard si el servidor está
  `active` y es multi-Odoo (o virgen); si es LEGACY, mantiene el UserError con el motivo nuevo
  ("layout legacy: adopción pendiente").
- **Costuras para R4 (solo anotadas, no se tocan acá)**: logs B2 → `journalctl -u
  instance.service_name`; config B3 → `instance.conf_path`; addons B4 → `instance.addons_dir`;
  backups → `instance.data_dir`; staging = `job_add_instance` de una instancia staging.

**IAM: cero cambios** (todo va por `ssm:SendCommand`, ya concedido).

### 8.1 Patrón «accesorio no tumba instancia sana» (DNS best-effort, decidido en B2)

En `job_add_instance`, el DNS (Route53) corre DESPUÉS de que el Odoo quedó montado, sano y
con los vecinos verificados. Si Route53 falla en ese punto, **la instancia queda activa**
con una **advertencia visible** (chatter) — marcarla `error` forzaría un retry completo que
chocaría con `PCM_ERR_DIRTY_SLUG` (la instancia YA existe). Las 3 condiciones del patrón:
1. **Advertencia visible y accionable** (qué falló + que se reintente desde Registros DNS).
2. **Accesible mientras tanto**: el sitio nginx por dominio ya está montado; por IP responde
   la instancia default del servidor.
3. **Reintento del DNS aislado** (crear el registro desde el CRUD DNS), sin re-correr el
   install. Verificación de las 3 condiciones en la UI: B3.

## 9. Tests

- **Golden del conf**: contiene `db_filter = ^<db>$`, `list_db = False`, puertos y rutas del
  slug — y NO contiene credenciales ajenas.
- **Golden de propiedad**: renderizado con slug X, toda ruta escrita/borrada por el script
  contiene `X` (salvo runtime compartido y reload de nginx) — fija "jamás toco lo ajeno".
- **Golden del rollback**: orden stop-unit → rm-site+reload → dropdb-condicional → drop-user →
  rm-dirs; y `dropdb` SOLO bajo la bandera de creación.
- Allocator de puertos (slots, saltea ocupados, constraint), lock key por servidor,
  `job_add_instance` con SSM mockeado (feliz / vecino-caído → failed / bootstrap ausente),
  wizard (BD duplicada por servidor, proyecto de otro cliente OK), gate legacy.
- moto no aplica (es SSM/shell): los scripts se fijan por golden + la prueba real.

## 10. Prueba real (cierre de R3 — no negociable, plan §5.4 + precisión de R2)

t3.micro real, cuenta 603011031378, región de prueba, cero-config, barrido al final:
1. **Crear entorno** → servidor multi-Odoo + instancia **A** → HTTP 200 por su dominio.
2. **Agregar instancia B** (otro proyecto → servidor compartido de verdad) → HTTP 200 de B
   **y A intacta**: hash del conf de A idéntico, uptime del unit de A sin reinicio, HTTP 200
   de A re-verificado. Runtime compartido: el install de B NO recompila (se mide el tiempo).
3. **Instalación C con fallo inducido** (p. ej. puerto pisado a mano o token inválido) →
   rollback: A y B intactas (mismo triple check), cero residuos de C (unit/site/BD/user/dirs).
4. **Barrido**: terminar, archivar, caché de región; SG según permisos del momento.

## 11. Riesgos específicos de R3

1. **RAM**: 2 Odoo en 1 GB + swap = lento pero vivo (workers=0 + swap). La advertencia del
   wizard es advisoria en v1; el límite duro queda para cuando haya medición por instancia.
2. **pip/requirements en t3.micro**: el primer runtime tarda (~min); compartirlo lo paga UNA
   vez por versión. El flock evita la doble compilación concurrente.
3. **BD por nombre único en el cluster**: validado en el wizard por servidor; la colisión con
   BDs ajenas a PCM la reporta el script (`PCM_ERR_DB_EXISTS`) sin tocarla.
4. **certbot rate-limit / DNS**: best-effort explícito (como hoy), reintentable.
5. **default_server**: solo `IS_DEFAULT` (primera instancia); si esa instancia se elimina en
   el futuro, el acceso por IP cruda queda sin dueño — anotado para el des-instalar formal
   (fase futura; R3 solo desinstala vía rollback de su propia corrida).

## 12. Decisiones a aprobar (D-R3.1 … D-R3.10)

| # | Decisión | Propuesta |
|---|---|---|
| D-R3.1 | Servidores nuevos nacen multi-Odoo desde la 1ª instancia (la cadena R2 adopta el layout; `install_odoo.sh` queda para los legacy existentes) | **Sí** |
| D-R3.2 | Agregar instancia a servidor LEGACY sigue gated (adopción = mini-fase posterior si hace falta) | **Sí** |
| D-R3.3 | Rollback clean-slate (trap desmonta lo creado en esa corrida; dropdb solo si la BD nació en esa corrida; retry = re-run limpio) | **Sí** |
| D-R3.4 | PostgreSQL siempre en bootstrap (también con RDS) | **Sí** (v1, simple) |
| D-R3.5 | Runtime compartido on-demand (bootstrap no precarga versiones) | **Sí** |
| D-R3.6 | `workers = 0` default en toda instancia nueva (configurable en wizard) | **Sí** |
| D-R3.7 | Swap 2 GB en bootstrap si RAM < 4 GB | **Sí** |
| D-R3.8 | Puertos por slots de a 10 desde 8069/8072; los slots no se reciclan en v1 | **Sí** |
| D-R3.9 | certbot best-effort por sitio (advertencia, no aborta) | **Sí** |
| D-R3.10 | Credencial PG propia por instancia (`odoo_<slug>` + password transitoria al encolar) | **Sí** |

## 13. Bloques de implementación (tras aprobar; cada uno con su testeo)

| # | Bloque | Testeo |
|---|---|---|
| R3-B1 | `bootstrap_server.sh` + `install_instance.sh` + builders/tokens en Python | goldens §9 |
| R3-B2 | Allocator de puertos + lock por servidor + `job_bootstrap_server`/`job_add_instance` + health snapshot + orquestación del rollback; la cadena R2 adopta el layout | unit con SSM mockeado |
| R3-B3 | Wizard "Agregar instancia" + gate por layout + vistas | tests wizard + smoke |
| R3-B4 | **Prueba real §10** (A + B compartido + C-fallo-inducido + barrido) | evidencia real |

---

**FRENO.** Espero revisión de D-R3.1…D-R3.10 (en particular D-R3.1/D-R3.2 — el destino del
script legacy — y D-R3.3 — la semántica clean-slate del rollback) antes de escribir código.
