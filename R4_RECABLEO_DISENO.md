# R4 — Recablear lo por-instancia: diseño fino

> **DISEÑO. Cero código. FRENO al final.** R3 cerró el multi-Odoo físico (N unit/conf/nginx
> por servidor, probado real). R4 hace que TODO lo que opera "el Odoo del servidor" opere
> **una instancia**: panel, logs, config (B3), addons (B4), impersonación (B5), backups,
> restore y staging. Diseñado sobre un inventario exhaustivo del código real (file:line
> auditados), no de memoria.

---

## 0. El hallazgo que ordena la fase

R1 creó los campos por-instancia (`conf_path`, `service_name`, `data_dir`, `addons_dir`,
`http_port`, `pg_user`, `database_id`) y R3 los materializa al instalar — pero **ningún flujo
operativo los lee todavía**. Todos los caminos de B2–B5, backups, restore, staging y deploy
usan constantes legacy (`/etc/odoo/odoo.conf`, unit `odoo`, `/opt/odoo/custom-addons`,
`/opt/odoo/venv`, puerto 8069) o el par `environment_id`/`ec2_instance_id` como si el
servidor tuviera UN Odoo.

**La clave del recableo barato:** las instancias legacy ya llevan los valores legacy en sus
campos (defaults de R1) y las multi-Odoo llevan los del slug (materialize de R3). Recablear
= leer los campos de la instancia. **Un solo código sirve para ambos layouts**; no hay
migración de datos (solo 3 campos nuevos, §2).

## 1. Decisión de modelado: el panel opera INSTANCIAS

Hoy los métodos de B2–B5 viven en `primate.cloud.ec2.instance` (la máquina) sin selector de
instancia (`fetch_logs`, `fetch_config/action_save_config/job_save_config`,
`_ensure_custom_addons_path`/clone, `deploy_impersonate`/`action_login_as`/
`set_impersonate_enabled`, `restart_odoo`, `_odoo_shell_heredoc`, `_RUNTIME_PROBE`).

**D-R4.1 — Se MUEVEN a `primate.cloud.instance`.** La instancia resuelve su máquina
(`environment_id.ec2_instance_id`) para el SSM y lee SUS campos para todo lo demás. La EC2
queda como máquina pura: ciclo de vida, métricas, comandos SSM crudos, logs de SO. La
bitácora de config/addons/impersonación/logs pasa a `record=instance` (hoy se atribuye a la
máquina). Los wrappers del dashboard reciben el id de `primate.cloud.instance` REAL (hoy
`get_instance_config(instance_id)` recibe el id de la EC2 — el nombre ya mentía).

## 2. Campos nuevos en la instancia (los 3 que faltan)

| Campo | Default legacy | Materialize multi-Odoo (R3) |
|---|---|---|
| `python_bin` | `/opt/odoo/venv/bin/python3` | `/opt/pcm/runtime/odoo-<ver>/venv/bin/python3` |
| `odoo_bin` | `/opt/odoo/odoo/odoo-bin` | `/opt/pcm/runtime/odoo-<ver>/src/odoo-bin` |
| `log_path` | `/var/log/odoo/odoo.log` | `/opt/pcm/instances/<slug>/log/odoo.log` |

Consumidores: odoo-shell heredoc (B5 set_param), `module_update` del deploy, `-i` del
companion, runtime probe, logs (§3). `_materialize_multiodoo_layout` los escribe; los
legacy quedan por default. Usuario unix sigue compartido (`odoo`, D-R4 v1 — sin cambios).

## 3. Bloques

### R4-B1 — Modelo y cimientos + GUARDS runtime (agregado de la aprobación)
Campos §2 + helpers en instancia (`_machine()`, `_ssm()`, `_log(record=self)`) + el deploy
recableado (`primate_cloud_deployment.py:248-260`: restart por `service_name`,
`module_update` por `python_bin`/`odoo_bin`/`conf_path` y la BD de `instance.database_id`).
`add_addon` fija `local_path` desde `instance.addons_dir`.

**Clasificación (a)/(b) de los usos legacy y guards (regla permanente nueva: lo destructivo
no-recableado se BLOQUEA en runtime, no se anota):**

| Flujo | Clase | Por qué / peor caso | Tratamiento |
|---|---|---|---|
| **Restore** (`job_restore_backup`: `systemctl stop odoo` + `dropdb`/`createdb` por NOMBRE + filestore legacy) | **(b) — el peor** | En un server multi, `stop odoo` no para nada (unit inexistente) y `dropdb <nombre>` **destruye la BD VIVA de otro cliente** | **GUARD** en job + wizard |
| **Backup gestionado** (`job_run_backup`: filestore base legacy, creds RDS de conf legacy) | (b) | Backup sin filestore / con datos mal atribuidos = respaldo silenciosamente inválido o exposición cruzada | **GUARD** en job + botón + cron (falla honesta, no skip) |
| **Staging create/refresh** | (b) | Arrastra backup+restore + restart legacy sobre el ORIGEN | **GUARD** en ambos jobs (sobre el origen) |
| **Addons B4** (`add_addon`/`job_add_addon`: clone + edición de conf legacy + restart) | (b) | Escribe dirs/conf en un server ajeno al flujo; `mkdir -p` CREA rutas legacy en un server multi | **GUARD** |
| **Config B3 write/rollback** (`job_save_config`, `_config_rollback` → SIEMPRE `/etc/odoo/odoo.conf`) | (b) | El rollback restauraría el conf equivocado | **GUARD** en action + job |
| **Impersonación deploy/enable** (untar a dir legacy, `-i` con runtime legacy, restart unit legacy) | (b) | Escribe en el server + es la pieza de seguridad | **GUARD** |
| **Deploy** (checkout/pull/module_update/restart) | (b) → **se RECABLEA en B1** | En vez de guard, B1 lo arregla directo (tiene `instance_id` del mixin R1) | fix, no guard |
| Logs fetch/stream, config READ, runtime probe, `list_db_users`, paths informativos del JS | (a) | Leen la ruta equivocada → dato vacío/erróneo, cero daño | Esperan a su bloque (B2/B6) |

**El guard**: `environment._ensure_legacy_flow_allowed(flow)` — bloquea con UserError si el
servidor tiene alguna instancia no archivada materializada al layout multi
(`service_name != 'odoo'`) **o más de una instancia no archivada** (destino ambiguo).
Mensaje accionable: qué flujo, por qué, y que se libera en su bloque de R4. Cada bloque
LEVANTA su guard al recablear. Tests: cada entrada guardada rechaza sobre multi y pasa
sobre legacy puro.

### R4-B2 — Panel-ops por instancia: logs + config + addons
- **Logs split (plan §6):** fuente `odoo` = `tail` de `instance.log_path` (ambos layouts
  loguean a archivo; en multi el journal de la unit queda casi vacío porque el conf define
  `logfile`) con `journalctl -u <service_name>` como fallback; `os`/`nginx`/`postgres`
  QUEDAN en el servidor (pantalla máquina). `_LOG_SOURCES`/`_JOURNAL_STREAM` dejan de
  hardcodear la unit `odoo` (`ec2_instance.py:261-268,313`).
- **Config B3:** `config_read.py:20` y `config_apply.py:39` ganan token `%%CONF_PATH%%`;
  restart/health/rollback por `service_name` + `http_port`
  (`ec2_instance.py:594,602-604,639-641,665-668` — el rollback hoy restaura SIEMPRE a
  `/etc/odoo/odoo.conf`: en una instancia multi pisaría el conf equivocado). Probe de
  runtime por `python_bin`/`odoo_bin`/`conf_path` (`:1129-1135`).
- **Addons B4:** clone a `instance.addons_dir` (`:690-746`); en multi el `addons_path` del
  conf YA incluye `instances/<slug>/addons` (R3) → `_ensure_custom_addons_path` solo hace
  falta en legacy (se detecta por instancia, mismo código).
- Salud tras cada mutación: el patrón health-check antes/después de B3 se mantiene, ahora
  contra el `http_port` y la unit de LA instancia (los vecinos no se verifican acá: editar
  el conf de B no toca a A — eso lo garantiza la propiedad por slug de R3 y lo fija la
  re-prueba real §5).

### R4-B3 — Impersonación multi-instancia (la delicada; pedido explícito #1)
Hoy: par Ed25519 **por cuenta** (`account.py:176-210`, sys-params
`pcm.impersonate.*.<account_id>`), base URL del **entorno**, allowlist nginx que reescribe
**todos** los vhosts con `proxy_pass 127.0.0.1:8069` (`ec2_instance.py:963-977`), deploy
por rutas legacy. En un servidor compartido eso significa: misma clave para A y B (un token
de A verificaría en B), y el allowlist de un cliente tocando el sitio de otro. Rediseño:

1. **Clave de firma POR INSTANCIA (D-R4.3):** par Ed25519 generado por instancia; privada
   cifrada con la Fernet de la cuenta en campos de `primate.cloud.instance`
   (`impersonate_privkey_encrypted`, `impersonate_pubkey`; groups admin, `copy=False` —
   un duplicado/staging JAMÁS hereda la clave). La pública que se despliega a la instancia
   es LA SUYA. **El caso cruzado queda estructuralmente imposible: un token firmado con la
   privada de A no verifica contra la pública de B.**
2. **Claim `instance_ref` en el payload** (el `pcm_ref` inmutable) + sys-param
   `pcm.impersonate.instance_ref` escrito al desplegar: el companion valida
   `payload.instance_ref == param local` ADEMÁS de la firma. Defensa en profundidad: si
   algún día dos instancias comparten clave por error operativo, el claim corta igual.
3. **Epoch y enabled por instancia:** ya viven en la BD de cada instancia (sys-params del
   companion, leídos frescos por la regla permanente); lo que se recablea es el ESCRITOR:
   `set_impersonate_enabled`/`_impersonate_set_param` pasan al odoo-shell de LA instancia
   (`python_bin`/`odoo_bin`/`conf_path`, `-d instance.database_id`). El kill-switch de B no
   toca las sesiones de A.
4. **Allowlist nginx POR SITIO (D-R4.4):** `_nginx_allowlist` edita SOLO el vhost propio
   (`pcm-<slug>.conf` en multi; el site del dominio en legacy) y hace `proxy_pass` al
   `http_port` de la instancia. Nunca más un barrido de `sites-enabled/*`.
5. **Base URL por instancia** (`instance.main_url`), deploy del companion a
   `instance.addons_dir` + `-i` con su runtime + restart de su unit.
6. **Companion v2:** único cambio de código remoto = validar el claim `instance_ref`
   (main.py, en el orden: enabled → firma → exp → nonce → instance_ref → user). El resto
   (nonce, epoch fresco, log local, banner) queda igual. Redeploy versionado.
7. **Rotación por instancia:** regenerar el par + re-desplegar la pública, sin tocar a las
   vecinas.
8. **D-R4.9 — Nacimiento de instancias y claves (respuesta a la pregunta de la aprobación):**
   una instancia NUEVA (staging incluido) **nace SIN par de claves** y por lo tanto sin
   capacidad de impersonación. El par se genera **automáticamente en el primer
   deploy/enable de Login-as de ESA instancia** (el acto explícito y auditado que ya existe
   en el ciclo de vida B5) — nunca como efecto colateral del `copy=False`, nunca en el
   create. Razones: la impersonación es opt-in por diseño (endpoint off por default;
   habilitarla ES el acto consciente); generar N pares que nadie usa multiplica secretos sin
   beneficio; y el flujo del operador no gana fricción (el deploy genera el par solo, sin
   paso manual de claves). El `copy=False` queda como red de atrás, no como mecanismo.

### R4-B4 — Backups + restore por instancia
- Backup: filestore desde `instance.data_dir` (no `BACKUP_FILESTORE_BASE`,
  `environment.py:2485`), credenciales RDS leídas del `conf_path` de la instancia
  (`:2461-2464`), y `job_run_backup` **itera instancias** (política/compliance por
  instancia desde R1) en vez de BDs del entorno.
- Restore: stop/start por `service_name` (`:2736,2740` — hoy pararía el Odoo EQUIVOCADO en
  un server multi), `createdb -O <pg_user>` (`:2747`), filestore destino por `data_dir`
  (`:2756`), y el wizard de restore elige INSTANCIA destino (la BD y las rutas salen de
  ella). El validador de compliance lee la instancia directo (sin pasar por la delegación).

### R4-B5 — Staging = instancia (plan §6)
El wizard de staging gana **servidor destino**: (a) un servidor multi-Odoo EXISTENTE
(default: el mismo del origen — el caso barato que R3 habilitó) o (b) "crear servidor
nuevo" (reusa la cadena R2, comportamiento actual). El staging se monta como
`job_add_instance` de una instancia `env_type=staging` + copia BD por el pipeline
backup/restore de B4 (ya por-instancia) + neutralización + repos al `addons_dir` del slug.
`origin_instance_id` ya existe (R1). Refresh apunta a la instancia staging.

### R4-B6 — Hub: pantalla SERVIDOR vs pantalla INSTANCIA + DNS best-effort (pedido #2)
- `get_server_detail` se parte: **servidor** (máquina: estado/métricas/acciones EC2/logs
  os-nginx-pg/instancias hospedadas) e **instancia** (nuevo serializer
  `get_odoo_instance_detail`: config/logs odoo/addons/login-as/backups/deploys/DNS/staging
  de ESA instancia). `PCM_PATHS` hardcodeado del JS (`servidor_detalle.js:11-19`) muere:
  las rutas/comandos copy-paste vienen del backend con los campos de la instancia.
- La delegación compat del entorno (related a `primary_instance_id`) se retira de los
  SERIALIZERS acá; los campos related del modelo quedan hasta el final de R4 (D-R4.7).
- **DNS best-effort §8.1 — las 3 condiciones viven en este bloque:**
  1. *Advertencia visible:* campo `dns_pending` en la instancia (razón del fallo, seteado
     por `job_add_instance` cuando Route53 falla) → badge/alerta en la pantalla instancia,
     no solo chatter.
  2. *Accesible mientras tanto:* la pantalla instancia muestra el acceso por IP + header
     Host (hint curl copy-paste) mientras `dns_pending` esté activo.
  3. *Reintento aislado:* botón "Crear DNS ahora" → `instance.action_retry_dns` → reusa
     `_provision_dns(..., instance=)` con hosted_zone/ttl persistidos en `dns_pending`
     (JSON), SIN re-correr ningún install (cero riesgo de `DIRTY_SLUG`). Éxito → limpia el
     flag + registro DNS real.

### R4-B7 — Pruebas reales de cierre (plan §6, no negociable)
Una sesión AWS (t3.micro, cero-config, barrido final) con servidor + instancias A y B de
clientes distintos:
1. **B3 real:** editar el conf de B por el panel → B reinicia y sana, **A ni se reinicia**
   (uptime de unit A idéntico) — y el rollback de config de B restaura EL conf de B.
2. **B5 real con CASO CRUZADO (precisión de la aprobación del plan):** impersonar en A OK
   (auditado ambos lados); **token emitido para A pegado en B → 403** (firma + claim);
   kill-switch de B no corta la sesión viva de A; epoch-bump de A la corta y B sigue.
3. **Staging en el MISMO servidor:** staging de A como 3ª instancia del server; A y B
   intactas (triple check R3-B4); neutralización verificada.
4. **Restore por instancia:** restaurar un backup de A sobre la BD de A; B ni se entera
   (uptime + HTTP).

## 4. Decisiones a aprobar (D-R4.1 … D-R4.8)

| # | Decisión | Propuesta |
|---|---|---|
| D-R4.1 | Panel-ops se MUEVEN a `primate.cloud.instance`; EC2 queda máquina pura; bitácora `record=instance` | **Sí** |
| D-R4.2 | 3 campos nuevos por instancia: `python_bin`, `odoo_bin`, `log_path` (defaults legacy; materialize multi) | **Sí** |
| D-R4.3 | Par Ed25519 **por instancia** (privada cifrada en campos de la instancia, `copy=False`) + claim `instance_ref` validado por el companion (v2) | **Sí** |
| D-R4.4 | Allowlist nginx SOLO en el vhost propio, `proxy_pass` al puerto de la instancia | **Sí** |
| D-R4.5 | Logs odoo = `tail` de `log_path` (fallback journal por unit); os/nginx/pg quedan en el servidor | **Sí** |
| D-R4.6 | Staging default = instancia en el MISMO servidor del origen; "servidor nuevo" queda como opción | **Sí** |
| D-R4.7 | La delegación compat del entorno (related a primary) se retira al FINAL de R4 (sub-bloque propio), no antes | **Sí** |
| D-R4.8 | `dns_pending` (JSON razón+params) en la instancia + `action_retry_dns` aislado | **Sí** |

## 5. Riesgos explícitos
1. **B3-rollback hoy es peligroso en multi** (restaura al conf legacy): B2 lo arregla; hasta
   entonces NO editar config de instancias multi por el panel (no hay producción, pero se
   anota).
2. **Companion v2 = redeploy** en instancias con Login-as habilitado (hoy: solo las de
   prueba; cero costo real).
3. **Mover métodos de modelo rompe la API JS del panel** → B6 y B1/B2 se sincronizan;
   smoke_ui cubre la regresión.
4. La clave por instancia multiplica secretos (N pares): mitigado por campos cifrados +
   `copy=False` + rotación por instancia.

**FRENO. Espero revisión de D-R4.1…D-R4.8 antes de codear.** Orden propuesto de ejecución:
B1 → B2 → B3 (freno intermedio: es la pieza de seguridad) → B4 → B5 → B6 → B7 (prueba real).
