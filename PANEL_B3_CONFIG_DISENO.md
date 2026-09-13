# Bloque B3 — Editar `odoo.conf` desde el panel — Diseño final para revisión

> **FRENO. Cero código.** Diseño de la tab **Config** del panel de instancia: mostrar los
> parámetros del `odoo.conf`, editar los seguros, guardar en el archivo real y reiniciar, sin
> romper la instancia ni exponer credenciales. Cubre los 6 puntos pedidos. Se codea recién con tu OK.

---

## 0. Decisión de arquitectura (de la que cuelga todo): editar EN LA INSTANCIA

La rewrite del `odoo.conf` ocurre **en la instancia** (script remoto por SSM), **no** bajando el
archivo a PCM. Motivo: el conf contiene `db_password`/`admin_passwd`; si PCM lo descargara para
editarlo, esas credenciales pasarían por el worker (memoria, posibles logs). Con el editor
remoto, **el archivo completo nunca sale de la instancia** — PCM solo manda la lista de
`(clave → valor nuevo)` y el script aplica el cambio in situ.

Esto parte el bloque en dos caminos, alineados con la política de CLAUDE.md:

| Camino | Naturaleza | Ejecución |
|---|---|---|
| **Leer para mostrar** (`get_instance_config`) | read-only, corto | **síncrono** (política de lecturas interactivas) |
| **Guardar + reiniciar** (`job_save_config`) | muta + corte de servicio | **`queue_job`** |

Y resuelve dos requisitos de un saque:
- **Preservación (punto 1):** el script remoto lee el conf REAL y edita solo las claves dadas.
- **`db_password` nunca al front (punto 2):** la lectura para la UI usa **allowlist** (emite solo
  las claves permitidas); el guardado no necesita las credenciales (quedan intactas en el archivo).

---

## 1. Preservación — parseo y reescritura (el requisito no negociable)

### NO se usa `configparser`
`configparser` **descarta comentarios**, puede **reflowar** el `key = value` y no garantiza
conservar el archivo tal cual. Viola "preservar intacto todo lo que no se muestra".

### Edición quirúrgica por líneas (en la instancia, con el `python3` del venv)
El editor remoto:

1. Lee `/etc/odoo/odoo.conf` línea por línea (bytes).
2. Para cada clave editada, busca la línea `^\s*<clave>\s*=` y **reemplaza solo su valor**,
   dejando el resto del renglón/archivo igual.
3. Las claves editadas que **no existían** se agregan al final de `[options]`.
4. Toda otra línea —comentarios, líneas en blanco, orden, y **claves no mostradas como
   `db_password`**— se copia **byte por byte**, sin tocar.
5. Escritura **atómica**: escribe a `odoo.conf.pcm-tmp`, `fsync`, y `mv` sobre el original
   (rename atómico; nunca queda un conf a medio escribir si el proceso muere).

**Resultado:** orden preservado, comentarios preservados, claves no mostradas preservadas
(incl. `db_password`/`admin_passwd`). **Nunca se reconstruye el conf desde los campos de la UI** —
se parte del archivo real y se tocan solo los renglones editados.

### Cómo llegan los valores al script (sin inyección)
PCM manda los pares `{clave: valor}` como **JSON en base64** en el argumento del comando SSM; el
script hace `base64 -d | python3 -c "...json.load..."`. Así los valores viajan como **datos**, no
interpolados en el shell (cero inyección de shell ni de config). El script **revalida** contra su
propia allowlist y tipos antes de tocar nada (defensa en profundidad, ver punto 3).

---

## 2. Editables vs read-only vs nunca (allowlist exacta v1)

La lectura para la UI emite valores **solo de esta allowlist** (denylist sería frágil: si el conf
tuviera otro secreto, se filtraría). `db_password`/`admin_passwd` **ni se leen**.

### Editables (seguros — no rompen conectividad ni el arranque)
| Clave | Tipo | Rango/validación |
|---|---|---|
| `proxy_mode` | bool | `True`/`False` |
| `list_db` | bool | `True`/`False` |
| `workers` | int | 0–64 |
| `max_cron_threads` | int | 0–16 |
| `limit_time_cpu` | int | 0–86400 (s) |
| `limit_time_real` | int | 0–86400 (s) |
| `limit_request` | int | 0–2·10⁶ |
| `limit_memory_soft` | int | ≥ 0 (bytes) |
| `limit_memory_hard` | int | ≥ `limit_memory_soft` |
| `log_level` | enum | `debug/info/warn/error/critical/debug_sql/debug_rpc` |

### Read-only (se MUESTRAN para contexto, no se editan en v1)
`db_host`, `db_port`, `db_user`, `db_name`, `data_dir`, `http_port`, `addons_path`.
> `addons_path` se toca **solo** por el flujo controlado de "agregar addon" (B4), nunca como
> texto libre acá — un `addons_path` mal escrito deja Odoo sin arrancar.

### Nunca salen al front (el parser los OMITE al serializar)
`db_password`, `admin_passwd`. No se enmascaran: **no se incluyen** en la respuesta de lectura.
En el guardado quedan intactos porque el editor no los toca.

---

## 3. Validación antes de escribir (y qué pasa si algo inválido pasa igual)

**Tres capas:**

1. **PCM, antes de encolar** (función pura, testeable): cada valor se castea/valida según la
   tabla de arriba. `limit_time_cpu = "abc"` → **`UserError` claro**, no se encola nada, no llega
   al archivo. Bools solo `True/False`; enums solo valores de la lista; ints dentro de rango;
   coherencia cruzada (`limit_memory_hard ≥ soft`).
2. **Script remoto** (misma allowlist + tipos): ignora cualquier clave fuera de la allowlist y
   re-castea; si un valor no castea, **aborta** con `PCM_ERROR_INVALID` sin escribir.
3. **Red de seguridad**: aunque un valor malo pasara las dos capas, el **restart + health check +
   rollback** (punto 4) restaura el conf viejo. Una config inválida **no puede** dejar la instancia
   caída.

---

## Regla permanente (hallada en el E2E real) — health checks

> **El estado terminal no basta: hay que comparar el COMPORTAMIENTO antes vs. después del cambio.**
>
> El E2E de rollback lo destapó: un valor válido para PCM (`limit_memory_hard` bajo) dejaba Odoo
> `is-active active` pero **sin servir HTTP** (los workers mueren). La regla ingenua "activo = ok,
> sin-http = degradado" daba por bueno un Odoo roto y **no hacía rollback**. El fix: capturar el
> estado HTTP **antes** del cambio (con la config vieja aún corriendo) y comparar — si **servía
> antes y no después**, el cambio lo rompió → `failed` → rollback; solo es `degraded` si **ya no
> servía antes** (proxy_mode/socket legítimo).
>
> **Aplica a CUALQUIER acción futura con health check** (restart, deploy, provision, addon-add):
> un chequeo de salud debe preguntar "¿esto quedó peor que antes del cambio?", no solo "¿está
> arriba ahora?". El baseline pre-cambio es parte del chequeo.

## 4. Backup + rollback (una config mala no deja la instancia abajo)

Secuencia del script remoto (todo en la instancia, dentro del `queue_job`):

1. **CAS de concurrencia** (punto 6): re-lee el conf, compara su hash con el esperado; si cambió,
   **aborta** sin tocar nada.
2. **Backup**: `cp /etc/odoo/odoo.conf /etc/odoo/odoo.conf.pcm-bak-<ts>` (conserva las últimas N;
   borra las más viejas). Es la red de rollback.
3. **Editar** in situ (punto 1) → escritura atómica.
4. **Restart**: `systemctl restart odoo`.
5. **Health check** (no basta con "el comando volvió"): espera hasta ~30–45 s a que
   `systemctl is-active odoo` == `active` **y** `curl -sf localhost:<http_port>/web/health`
   responda 200. (Odoo puede arrancar y caerse a los segundos por un conf malo → por eso se
   chequea que **sirva**, no solo que systemd lo lance.)
6. **Rollback si el health check falla**: `mv` del `.pcm-bak-<ts>` sobre el conf, `systemctl
   restart odoo`, y devuelve **FAILED** con el motivo. La instancia queda con el **conf viejo,
   funcionando** — nunca abajo por un cambio de PCM.
7. Marcadores `PCM_*` en stdout para que el job distinga aplicado/rollback/abortado. El job lo
   traduce a `success`/`failed`, escribe en `operation.log` y avisa por bus.

---

## 5. Reinicio, confirmación por entorno y bitácora

- **Editar config = reinicia Odoo** (corte de servicio de segundos). La UI lo dice explícito
  ("Guardar reinicia Odoo; habrá unos segundos de corte").
- **Confirmación según entorno:**
  - **Prod** (`env_type == 'production'`): **tipear el nombre exacto del entorno**, validado
    **server-side** en `action_save_config` (mismo patrón que el restore; si no coincide,
    `UserError`, no se encola). Reiniciar el Odoo de un cliente es una interrupción de servicio.
  - **No-prod**: confirmación simple (`env.pcm.confirm`).
- **`operation.log` (inmutable)** — se registra un `action_type = "config_edit"` con:
  - **quién** (`self.env.uid`), **cuándo** (timestamp), entorno/instancia, y flag prod.
  - **diff de parámetros**: `clave: viejo → nuevo` por cada cambio. El "viejo" sale de la lectura
    allowlist previa al guardado; `db_password`/`admin_passwd` **no aparecen** en el diff (no son
    editables). Si hubo rollback, se loguea el intento + el motivo del fallo.
- Se agrega `config_edit` a los `ACTION_TYPES` del log.

---

## 6. Concurrencia (alguien edita por SSH mientras PCM tiene el conf "abierto")

**Optimistic locking por hash (compare-and-swap sobre el archivo):**

- La **lectura** (`get_instance_config`) devuelve, además de los valores, un **`config_hash`**
  (sha256 del archivo COMPLETO, calculado **en la instancia**; solo el hash sale de la caja, nunca
  el contenido → no filtra `db_password`).
- El **guardado** manda ese `config_hash` como `expected_hash`. El script remoto, **como primer
  paso** (punto 4.1), **re-lee el conf y recomputa el hash**:
  - Si `hash(actual) != expected_hash` → alguien lo cambió por SSH/otra vía desde que PCM lo
    abrió → **aborta con `PCM_ERROR_STALE`**, no escribe. PCM muestra *"El archivo cambió desde que
    lo abriste; recargá la configuración y volvé a intentar"*.
  - Si coincide → procede (backup + edit + restart).

Esto cubre lo pedido: **se re-lee justo antes de escribir** y **se detecta el cambio**, evitando
pisar una edición ajena. Además, la edición quirúrgica (punto 1) ya acota el daño: PCM solo
reescribe las claves que el usuario tocó, no el archivo entero — un cambio SSH a **otra** clave
sobreviviría igual; el CAS protege el caso de choque sobre la **misma** clave o cualquier cambio
concurrente.

---

## 7. Flujo completo (resumen)

```
Abrir tab Config
  └─ get_instance_config(instance_id)   [SÍNCRONO, read-only]
       SSM: emite SOLO allowlist (editable+readonly) + sha256 del conf
       → { editable:{...}, readonly:{...}, config_hash }   (sin db_password/admin_passwd)

Editar campos → "Guardar y reiniciar"
  └─ validación de tipos/rangos en PCM  (limit="abc" → UserError, corta acá)
  └─ prod? tipear nombre del entorno (validado server-side)
  └─ action_save_config(edits, expected_hash, typed_name)
       └─ with_delay().job_save_config(...)     [QUEUE_JOB]
            SSM script en la instancia:
              1. re-lee conf, hash CAS (≠ → PCM_ERROR_STALE, aborta)
              2. backup .pcm-bak-<ts>
              3. edición quirúrgica in situ (allowlist + tipos) + write atómico
              4. systemctl restart odoo
              5. health check (is-active + /web/health)
              6. falla → rollback al backup + restart + FAILED
              7. operation.log: config_edit con diff quién/cuándo (+ rollback si hubo)
            bus: notifica resultado
```

---

## 8. IAM / seguridad

- **Sin permisos IAM nuevos**: todo es `SendCommand` SSM ya concedido. Leer/editar el conf,
  backup, `systemctl restart`, health check por `curl` local — comandos de shell.
- El conf completo **jamás** sale de la instancia; solo salen: los valores allowlist (sin
  credenciales) y un hash. `db_password`/`admin_passwd` nunca se leen ni se muestran ni se loguean.
- El editor recibe datos por base64+JSON (sin inyección) y aplica solo la allowlist.

---

## 9. Piezas a construir (para estimar, NO se codea aún)

- **Backend `ec2.instance`**: `get_config` (allowlist read + hash, síncrono), `action_save_config`
  (valida + prod type-name + encola), `job_save_config` (script remoto CAS/backup/edit/restart/
  rollback + log), helpers puros `_validate_config(edits)` y `_config_diff(old,new)`, el
  editor remoto como script versionado (tipo `install_odoo.sh`, con tokens).
- **Dashboard**: `get_instance_config` wrapper.
- **Frontend**: tab Config (hoy deshabilitada) → form con editables (input/select) + read-only
  (grises) + "Guardar y reiniciar"; ruta prod con type-name; manejo del error "cambió
  externamente" (recargar).
- **`operation.log`**: `action_type` `config_edit`.
- **Tests**: `_validate_config` (rangos, "abc", coherencia soft/hard), edición quirúrgica preserva
  comentarios/orden/`db_password` (sobre un conf de muestra), CAS detecta hash distinto, allowlist
  omite `db_password`/`admin_passwd`, prod exige nombre, diff correcto, rollback ante restart
  fallido (SSM mockeado). **Peso: MEDIO-ALTO.**

---

## 10. Puntos abiertos para tu decisión

1. **`http_port` para el health check**: se lee del conf (default 8069). ¿OK asumir que Odoo
   escucha local en ese puerto (aunque nginx esté delante)? Sí en el layout PCM.
2. **`log_handler`**: lo dejé **fuera** de editables v1 (formato `:LEVEL` complejo, fácil de
   romper). ¿Lo querés dentro?
3. **Cuántos backups `.pcm-bak`** conservar (propongo 5) antes de borrar los viejos.
4. **`server_wide_modules`** y otros no listados: quedan **read-only implícito** (ni se muestran ni
   se editan en v1). ¿Alguno que quieras mostrar como read-only?

**FRENO. Espero tu revisión/aprobación de este diseño para codear B3.** Después intercalo B4
(agregar addon, sin freno) y cerramos con B5 (Login as, freno fuerte).
