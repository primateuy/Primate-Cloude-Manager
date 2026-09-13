# Panel de instancia estilo CloudPepper — Propuesta de diseño (pre-Fase 10)

> **Estado: DISEÑO + INVENTARIO. Cero código.** Rediseño de la pantalla de servidor/instancia
> (`ServidorDetalle`) para convertirla en un panel de gestión con tabs (Dashboard / Logs /
> Backups / Addons / Config) al estilo CloudPepper. Este documento clasifica CADA feature en
> **(A)** reorganizar lo que ya existe o **(B)** backend nuevo, responde las preguntas de diseño
> y propone un plan por bloques que hace primero todo (A) y después cada (B) con su freno.
>
> **FRENO al final. Nada se codea hasta aprobar bloque por bloque** (en especial *Login as* y
> *editar config*).

---

## 0. Estructura del panel (componentes OWL)

Reemplaza al `servidor_detalle.{js,xml}` actual (detalle plano) por un panel con tabs, dentro de
la app PCM (sidebar navy + acento configurable, sin modales stock, drill-through por `env.pcm`).

```
PanelInstancia (screens/panel_instancia/panel_instancia.{js,xml})
├── barra superior: nombre + PcmStatusBadge(estado) + acciones Stop / Restart / Delete
├── tabs (estado local state.tab, deep-link por hash #instancia/<id>/<tab>)
│   ├── TabDashboard   (tabs/tab_dashboard.{js,xml})
│   ├── TabLogs        (tabs/tab_logs.{js,xml})       ← streaming
│   ├── TabBackups     (tabs/tab_backups.{js,xml})
│   ├── TabAddons      (tabs/tab_addons.{js,xml})
│   └── TabConfig      (tabs/tab_config.{js,xml})     ← editar odoo.conf
```

- Cada tab es un sub-componente que recibe `serverId` y pide sus datos al montarse (lazy: no se
  carga Logs/Config hasta abrir la tab, así el streaming y las lecturas SSM no arrancan solas).
- Toda acción que llame a AWS/SSM y sea **mutante o larga** (backup, restart, config write, clone)
  va por `queue_job`; las **lecturas interactivas** (logs, leer conf, listar usuarios) van
  síncronas y acotadas — ver §7 (contradicción con la regla de queue_job y cómo se resuelve).

---

## 1. Inventario A/B por tab

Leyenda: **(A)** = UI sobre datos/acciones existentes · **(B)** = backend nuevo.

### Tab 1 — Dashboard

| Elemento | Cat. | De dónde sale / qué construir |
|---|---|---|
| Estado (running/stopped) | **A** | `ec2.instance.instance_state` (ya en `get_server_detail`) |
| Referencia AWS (instance id) | **A** | `aws_instance_id` |
| IP pública / privada | **A** | `public_ip` / `private_ip` |
| Tipo, región, disco, SO | **A** | `instance_type` / `region` / `disk_size_gb` / `os_type` |
| Base(s) de datos (nombre) | **A** | `database` con `ec2_instance_id = inst` (ya en `get_server_detail.databases[]`) |
| Botones Stop / Restart / Delete | **A** | `action_stop` / `action_restart` / `action_open_terminate_wizard` (Delete = terminate, ya con doble confirmación) |
| **Paths + comandos de shell** (source, logfile, config, python, comandos Odoo shell / -u módulo / -u all / pip install) | **A\*** | **Derivables** de la plantilla `install_odoo.sh`: `ODOO_HOME=/opt/odoo`, conf `/etc/odoo/odoo.conf`, python `/opt/odoo/venv/bin/python3`, `odoo-bin /opt/odoo/odoo/odoo-bin`, log `/var/log/odoo/odoo.log`, servicio `odoo.service`. Los comandos son **strings armados en el front para copiar** (cero backend). **\*Caveat**: sólo son ciertos para instancias aprovisionadas por PCM con el layout estándar (ver §6, contradicción #2) |
| Versión Python | **B** | No se almacena → SSM `…/venv/bin/python3 --version` |
| Versión Odoo | **B** | No se almacena → SSM `odoo-bin --version` (o leer del git del repo core) |
| Nº de workers | **B** | No se almacena → leer de `odoo.conf` (lo trae la Tab Config; en la default `workers` no está seteado ⇒ 0) |
| URL / dominio de producción | **B‑chico** | No hay campo dedicado; derivar del A‑record DNS del entorno o de `web.base.url` (SSM query a la BD) |
| **Login as…** | **B (pesado)** | Ver §2. Es la feature más potente y más delicada |

### Tab 2 — Logs (streaming)

| Elemento | Cat. | De dónde sale / qué construir |
|---|---|---|
| Traer logs por fuente (odoo/nginx/postgres/os), grep, nº líneas | **A** | `ec2.fetch_logs` + `dashboard.get_instance_logs` (ya existen, on-demand) |
| **Auto-refresh / streaming** (cada N s), pausa, búsqueda incremental | **B** | Polling incremental por SSM con cursor/offset (ver §4). Distinto del fetch completo actual |

### Tab 3 — Backups

| Elemento | Cat. | De dónde sale / qué construir |
|---|---|---|
| Lista de backups de la instancia | **A** | `primate.cloud.backup` filtrado por las BD del entorno/instancia (motor Fase 8). Nota: la granularidad del motor es entorno/BD, no "instancia" — la lista de la instancia = backups de las BD montadas en ella |
| Respaldar ahora | **A** | `environment.action_run_backup` / `job_run_backup` (Fase 8, `queue_job`) |
| Restaurar | **A** | `backup.action_restore` + wizard de restore (Fase 8, drawer) |
| Cumplimiento / política | **A** | `environment.backup_compliance` / `backup_policy_id` |

### Tab 4 — Addons

| Elemento | Cat. | De dónde sale / qué construir |
|---|---|---|
| Lista repos/addons (tipo, repo, branch, commit, estado sync) | **A** | `repository` (`repo_type`/`github_url`/`configured_branch`/`current_commit`/`sync_state`/`local_path`) + `module` (Fase 5). Nota: los repos cuelgan de `environment_id`, no de la instancia (ver §6, contradicción #3) |
| Settings (editar repo) | **A** | Form nativo del repo en drawer (`env.pcm.openRecord`) |
| Update | **A** | `deployment` tipo `pull`/`checkout_*`/`module_update` (Fase 6, `queue_job`) |
| Delete (quitar del registro) | **A** | `unlink` del repo (trazabilidad) |
| Sync commits / check estado / detectar módulos | **A** | `repository.action_sync_commits` / `action_check_sync_state` / `action_detect_modules` |
| **Agregar repo/addon a la instancia** (clonar en el server + cargarlo) | **B** | Ver §5. Clonado remoto ya existe (`_build_clone_script`), pero "que Odoo lo cargue" toca `addons_path` (depende de Config o de estandarizar un dir de custom-addons) |

### Tab 5 — Config

| Elemento | Cat. | De dónde sale / qué construir |
|---|---|---|
| Leer y mostrar `odoo.conf` (proxy_mode, list_db, addons_path, limit_time_*, db_*, workers…) | **B** | Leer/parsear el conf remoto por SSM. **Credenciales redactadas** (`db_password`, `admin_passwd` NUNCA a la UI) |
| Editar parámetros seguros y guardar en el archivo real | **B** | Whitelist editable (ver §3), validación, backup del conf, escritura por SSM |
| Reinicio del servicio | **B/A** | `systemctl restart odoo` por SSM (patrón de `service_restart` de deployment). Con fricción fuerte en prod |

**Resumen:** el panel **se ve y sirve completo con sólo lo (A)**. Lo (B) son 6 capacidades:
versiones/workers/URL (chico), logs streaming, config read+write, agregar addon real, y **Login
as** (el grande).

---

## 2. Q1 — Login as…

### El hallazgo crítico (cross-origin)
La app PCM corre en el Odoo de gestión (un dominio). La instancia gestionada es **otro servidor,
otro dominio/IP**. **PCM no puede setear la cookie de sesión de otro dominio desde el suyo.** Por
lo tanto *Login as* **no se puede resolver 100% del lado de PCM**: necesita un **endpoint de
impersonation del lado del Odoo remoto** que reciba un token firmado, cree la sesión y setee su
**propia** cookie (same-origin) antes de redirigir a `/web`.

### Diseño propuesto (password-safe)
1. **Componente remoto mínimo, provisto y controlado por PCM.** Un endpoint
   `/pcm/impersonate?token=…` (addon companion chico o controlador) desplegado en la instancia
   **por SSM** (drop del archivo + restart), **deshabilitado por default** y activable por
   instancia. Valida un **token firmado y de un solo uso, corta expiración (p. ej. 60 s)**, con
   la clave pública/compartida de PCM.
2. **Generación del token en PCM:** firma `(db, uid, login, exp, nonce)` con la clave de la
   cuenta (crypto ya existe). Nunca viaja ninguna contraseña.
3. **Montaje de la sesión:** el endpoint remoto crea la sesión server-side (session store de
   Odoo) para `(db, uid)`, setea la cookie same-origin y redirige a `/web`. **Sin tocar ni pisar
   contraseñas** (no se resetea password ni se crea apikey).
4. **Listado de `res_users`:** por **SSM query** (mismo patrón que la detección de módulos de
   Fase 5, `psql -tAF'|'` a la BD remota), **no XML-RPC** (XML-RPC exigiría credenciales/apikey;
   SSM peer evita credenciales, consistente con el principio del módulo). Filtrar a **usuarios
   internos activos** (`active=True`, `share=False`).
5. **Multi-BD:** el dashboard puede mostrar >1 "Database". El flujo es **elegir BD → listar
   usuarios de esa BD → elegir usuario → generar sesión para `(db, uid)`**. Si hay varias, selector
   de BD obligatorio.

### Seguridad / fricción (decisión de Daryl: habilitado en TODOS los entornos, prod incluida)
- **Prod:** confirmación explícita + **registro obligatorio en `operation.log`** de *quién entró
  como quién, cuándo, en qué BD* (auditoría crítica; el log es inmutable por diseño).
- Endpoint **off por default**, token **de un solo uso y corta expiración**, sólo `group_cloud_admin`
  puede disparar Login-as, y cada emisión de token se audita (aunque no se complete el redirect).
- **Peso: ALTO.** Es el bloque más grande y más delicado (sesión como otro usuario en un sistema
  en vivo + shipping de un componente remoto). **Freno de diseño fuerte antes de codear.**

---

## 3. Q2 — Config: qué es editable en v1

### Editable (seguro) v1 — no rompe la conectividad ni el arranque
`proxy_mode`, `list_db`, `limit_time_cpu`, `limit_time_real`, `limit_request`, `limit_memory_soft`,
`limit_memory_hard`, `workers`, `max_cron_threads`, `log_level`, `log_handler`.

### Read-only en v1 (romperían la instancia si se tocan mal)
`db_host`, `db_port`, `db_user`, `db_name`, `data_dir`, `admin_passwd`, y `addons_path`
(este último **sólo** se modifica por el flujo controlado de "agregar addon", nunca como texto
libre). **`db_password` y `admin_passwd` NUNCA se muestran** (se redactan al leer).

### Salvaguardas
- **Validación antes de escribir:** tipos (ints para los `limit_*`/`workers`, bool para
  `proxy_mode`/`list_db`), rango razonable, y un parse de sanidad del conf resultante.
- **Backup del `odoo.conf` antes de sobrescribir:** copia a `/etc/odoo/odoo.conf.bak-<timestamp>`
  → permite rollback si el restart falla.
- **Escritura + restart por `queue_job`** (es mutante). En **prod: fricción fuerte = tipear el
  nombre exacto del entorno** (patrón del restore) porque reiniciar el Odoo de un cliente es una
  **interrupción de servicio**; + registro en `operation.log`.
- Si el restart falla tras escribir, el job restaura el `.bak` y reintenta el restart (no dejar la
  instancia caída por un cambio de config).
- **Peso: MEDIO-ALTO.** Freno de diseño antes de codear.

---

## 4. Q3 — Logs streaming: mecanismo y costo

- **Polling incremental con cursor**, no re-traer todo:
  - `journalctl` (odoo/postgres/os): usar `--after-cursor=<cursor>`; journalctl emite un cursor
    por entrada → cada poll trae sólo lo nuevo desde el último cursor. Se muestra `-o` con cursor.
  - Archivos (nginx): offset por bytes (`tail -c +<offset>`) trackeando tamaño/inodo.
- **Cadencia:** el front pollea cada **~5 s** mientras la tab Logs esté **visible y no en pausa**;
  cada poll = un `SendCommand` SSM que devuelve sólo las líneas nuevas. **Se corta solo** al
  cambiar de tab, pausar, `visibilitychange` (pestaña oculta) o desmontar el componente.
- **No es streaming real:** SSM es comando-async + poll de salida, así que hay **lag de segundos**.
  La UI lo dice honestamente ("actualiza cada ~5 s"), no promete tiempo real.
- **Costo/carga:** cada poll es un `SendCommand` (cuota de la API SSM). Guardarraíles: intervalo
  mínimo (≥3 s), tope de líneas por poll, auto-stop al ocultar/salir. **Timeout corto** por
  llamada (no el `timeout=120` del fetch actual, que bloquearía el worker).
- **Peso: MEDIO.**

---

## 5. Q4 — Addons: "agregar repo a la instancia" en v1

Dos niveles posibles:
- **Sólo registrar en el modelo (trazabilidad):** crear el `repository` con `environment_id`. No
  toca el servidor. Insuficiente (no lo carga Odoo).
- **Registrar + clonar + cargar:** clonar en el server (ya existe `_build_clone_script`) **y**
  hacer que Odoo lo cargue (agregar al `addons_path` + restart).

### v1 propuesto: registrar + clonar a un **dir de custom-addons estándar ya en `addons_path`**
- **Delta a `install_odoo.sh`:** estandarizar `addons_path = /opt/odoo/odoo/addons,/opt/odoo/custom-addons`
  desde el aprovisionamiento. Así **agregar addon = clonar en `/opt/odoo/custom-addons/<repo>` +
  registrar el `repository` + restart**, **sin** editar el `addons_path` en caliente (que sería la
  capacidad riesgosa de la Tab Config).
- Para instancias legacy sin ese dir, el flujo cae a "registrar + avisar que el `addons_path` hay
  que ajustarlo por Config" (acoplado al Bloque de Config).
- Update/checkout de ese addon reusa `deployment` (Fase 6). Todo mutante por `queue_job`.
- **Peso: MEDIO** (depende del delta de la plantilla + restart controlado).

---

## 6. Q5 — IAM / SSM: ¿permisos nuevos?

**No hacen falta permisos IAM nuevos.** Todas las capacidades (B) corren como shell por SSM
`SendCommand` + `GetCommandInvocation`, ya concedidos (Fase 3). Leer/escribir archivos (`cat`,
`tee`, `cp` del conf), `tail`/`journalctl`, `psql` query a la BD, `git clone`, `systemctl restart`,
drop del endpoint de impersonation — todo es un comando SSM más. El agente corre como root ⇒
puede `sudo -u odoo`/`systemctl`. **Confirmado: IAM sin cambios.** Lo único "nuevo" es a nivel SO
(ya cubierto por el instance profile SSM). El endpoint de impersonation es código en la instancia,
no un permiso AWS.

---

## 7. Contradicciones con spec/código (a resolver antes de codear)

1. **Regla `queue_job` vs. lecturas interactivas por SSM.** `CLAUDE.md` dice *"Toda llamada a
   AWS/SSM… va en `queue_job`… nunca en un worker web"*. Pero `fetch_logs`/`get_instance_logs`
   (Fase 9) **ya llaman SSM síncrono en el worker web**. El panel suma más lecturas interactivas
   (logs streaming, leer conf, listar usuarios). **Decisión propuesta:** las **lecturas
   interactivas, read-only, cortas y acotadas** corren síncronas (continúan el precedente de
   `fetch_logs`, pero con **timeout corto** para no bloquear el worker); todo lo **mutante o largo**
   (backup, config write+restart, clone, lifecycle) sigue por `queue_job`. Hay que dejarlo escrito
   como excepción explícita, no como violación silenciosa.
2. **"Paths/comandos derivables" asume layout PCM.** Los paths del Dashboard salen de
   `install_odoo.sh`; una instancia **importada/legacy** puede tener otro layout ⇒ los comandos
   "listos para copiar" podrían ser falsos. **Propuesta:** marcar en el modelo si la instancia fue
   **aprovisionada por PCM** (layout garantizado) vs. importada (para éstas, leer los paths reales
   por SSM o mostrarlos como "asumidos, verificar").
3. **Repos/addons cuelgan del entorno, no de la instancia.** `repository.environment_id` (no hay
   `repository.ec2_instance_id`). En un entorno de **una** instancia es equivalente; en uno
   **multi-instancia** no está definido qué addon vive en qué instancia. **Propuesta v1:** la Tab
   Addons muestra los repos del **entorno** de la instancia (nota explícita en UI); el vínculo
   fino repo↔instancia queda para después (o se asume 1 instancia/entorno, que es el caso real hoy).
4. **Backups por entorno/BD, no por instancia.** La Tab Backups lista los backups de las **BD
   montadas en la instancia**; "Respaldar ahora" respalda a nivel entorno/BD (motor Fase 8). Es
   coherente, sólo hay que nombrarlo bien en la UI.
5. **"Sin lógica de negocio nueva fuera de lo que estas features requieran"** (`CLAUDE.md`).
   *Login as* y *editar config* **son** lógica de negocio nueva; este documento los lista y los
   somete a aprobación antes de construir (que es justo el contrato pedido).
6. **`operation.log` inmutable = destino de la auditoría** de Login-as y config-edit (quién, qué,
   cuándo). Encaja con el diseño; hay que sumar los `action_type` nuevos (`login_as`,
   `config_edit`, `addon_add`).

---

## 8. Plan por bloques (A primero; cada B con su freno)

> Regla: **primero todo (A)** → el panel se ve y sirve con lo existente. Después cada (B) como
> bloque propio. Los dos delicados (**Config** y **Login as**) llevan **freno de diseño** antes de
> codear.

| # | Bloque | Cat. | Peso | Freno |
|---|---|---|---|---|
| **A** | **Shell del panel + 4 tabs con lo existente**: tabs Dashboard (estado/IP/aws id/DB/paths-comandos copy/Stop-Restart-Delete), Backups (lista + respaldar + restore, Fase 8), Addons (lista + Settings/Update/Delete/sync, sin "agregar" real), Logs (con el fetch on-demand actual como base). Reorganización pura, cero backend | A | Medio (mucha UI, cero riesgo) | No (se ve y sirve) |
| **B1** | Dashboard completo: **versión Python/Odoo, workers, URL prod** (SSM read chico, síncrono acotado) | B | Bajo | Ligero |
| **B2** | **Logs streaming** (polling incremental con cursor, pausa, auto-stop) | B | Medio | Ligero |
| **B3** | **Config**: leer/parsear conf (redactar credenciales) → mostrar; whitelist editable + validación + backup del conf + write + restart con fricción prod | B | Medio-Alto | **FRENO de diseño** |
| **B4** | **Agregar addon a la instancia** (delta plantilla custom-addons + clone + restart) | B | Medio | Ligero |
| **B5** | **Login as** (endpoint remoto de impersonation vía SSM + token firmado + listar `res_users` + multi-BD + auditoría/fricción prod) | B | **Alto** | **FRENO de diseño fuerte** |

**Orden sugerido:** A → B1 → B2 → B3 (freno) → B4 → B5 (freno fuerte). B1/B2 completan el
dashboard y suman la mejor UX barata; B3 y B5, los delicados, se revisan antes de tocar código.

Cada bloque cierra con sus tests (mock de SSM/AWS, `moto` donde aplique) y su smoke_ui, igual que
las fases anteriores. Nada de tagging/lectura del panel puede hacer fallar la operación que la
instancia sirve (principio de siempre).

---

## 9. Qué apruebo hoy vs. qué se decide después

- **Aprobar para arrancar:** el **Bloque A** (reorganización sin backend) — no tiene riesgo.
- **Revisar antes de codear:** el diseño de **B3 (Config)** y **B5 (Login as)** — este documento es
  el borrador de ambos; cuando toque, se abre el diseño fino de cada uno.
- **Confirmar deltas:** (a) marcar instancia "aprovisionada por PCM" vs importada (contradicción
  #2); (b) estandarizar `custom-addons` en `install_odoo.sh` (Bloque B4); (c) `action_type` nuevos
  en `operation.log`.

**FRENO. Espero tu OK (bloque por bloque) para empezar por el Bloque A.**
