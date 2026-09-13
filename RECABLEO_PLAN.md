# Plan de recableo del modelo — servidor ≠ instancia, instancia de dos padres

> **PLAN. Cero código. FRENO al final de este documento y entre cada fase.**
> Momento elegido a propósito: **sin datos de producción** — cada instancia real creada con el
> modelo viejo encarece este recableo (interés compuesto). Se recablea la raíz, no se parchea.

---

## 0. El desalineamiento confirmado (por qué esto es la raíz)

Hoy el módulo tiene **una sola entidad** (`primate.cloud.environment`) que mezcla dos cosas:

- **La máquina** (el flujo de aprovisionar lanza la EC2 desde el entorno; el costo AWS se
  atribuye por el tag `primate:environment_id` de la EC2).
- **El Odoo** (versión/edición, dominio, BD, repos, deploys, backups, staging — todo cuelga de
  `environment_id`, verificado en el código: `repository/deployment/dns_record/database/backup/
  module.environment_id`).

Consecuencias: "crear instancia" == "lanzar EC2" (imposible montar 2 Odoo en una máquina),
un servidor no puede hospedar clientes distintos, y el costo por cliente solo funciona con
servidores dedicados. La auditoría lo confirmó; esto lo corrige de raíz.

---

## 1. Modelo destino (confirmado con Daryl — exacto)

```
ANTES (hoy)                              DESPUÉS (destino)
===========                              =================

project (cliente-ish)                    PROYECTO = cliente (partner)
  └── environment (máquina+Odoo mezclados)  └── sus INSTANCIAS (facturación/gestión)
        ├── ec2.instance(s) (la EC2)
        ├── database(s)                  ENTORNO = SERVIDOR (la EC2, la máquina)
        ├── repository(s) → commits/módulos     └── INSTANCIAS que hospeda (infra)
        ├── deployment(s)
        ├── dns.record(s)                INSTANCIA = un Odoo dentro de un servidor
        └── backup(s) / policy             ├── project_id  (cliente)   ← obligatorio
                                           ├── environment_id (servidor) ← obligatorio
cost.entry → environment_ref (tag EC2)     ├── database, repos, deploys, dns,
                                           │   backups, staging, panel, login-as
                                           └── cost.share (su pedazo del costo del server)

                                         cost.entry (crudo AWS) → SERVIDOR
                                         cost.share (calculado) → INSTANCIA/PROYECTO
```

Reglas de comportamiento destino (el corazón del recableo):

1. **Crear entorno** = aprovisionar el servidor EC2 **y** montarle su **primera instancia**.
2. **Crear instancia** = montar OTRO Odoo dentro de un servidor **existente**. NO lanza EC2.
   Otro puerto, otra BD, otro dominio, otro systemd, otra entrada nginx — sin tocar los Odoo
   que ya corren.
3. **Crear staging** = montar una instancia dentro de un servidor (el mismo u otro), no una
   máquina nueva.
4. Todo lo "por instancia" cuelga de la instancia; solo lo de la máquina cuelga del entorno.

---

## 2. Delta de modelo exacto

### 2.1 Decisión estructural D-R1 — ¿qué modelo es "el servidor"? (recomendación: Opción A)

- **Opción A (recomendada): `primate.cloud.environment` se ADELGAZA a "servidor gestionado" y
  conserva un 1:1 con `primate.cloud.ec2.instance` (la máquina como recurso AWS).**
  - `environment.ec2_instance_id` (M2O, required una vez aprovisionado; el inverso reemplaza al
    actual `ec2_instance.environment_id`).
  - `ec2.instance` sigue existiendo como **recurso AWS de inventario** (el sync de cuentas
    importa máquinas que PCM no gestiona; el historial de terminadas se conserva). Etiqueta UI:
    **"Máquina AWS (EC2)"** para matar la ambigüedad con la nueva Instancia.
  - Por qué A: el sync/inventario/moto-tests no se tocan; toda la plomería SSM/lifecycle/metrics
    del panel queda alcanzable vía `server.ec2_instance_id`; churn mínimo en código validado.
- **Opción B (descartar salvo preferencia fuerte): fusionar `ec2.instance` dentro de
  `environment`.** Más pura (una entidad = un servidor) pero re-toca TODO lo validado (sync,
  panel, moto, smoke) y rompe el inventario de máquinas no gestionadas. Cara y riesgosa.

### 2.2 Entidad NUEVA: `primate.cloud.instance` (la Instancia = un Odoo)

Archivo `models/primate_cloud_instance.py`, clase `PrimateCloudInstance`, hereda `mail.thread`.

| Campo | Tipo | Notas |
|---|---|---|
| `name` | Char required | nombre humano |
| `project_id` | M2O project **required** ondelete=restrict | eje CLIENTE |
| `environment_id` | M2O environment **required** ondelete=restrict | eje INFRA (servidor) |
| `slug` | Char required | identificador técnico (dirs/unit/BD); `UNIQUE(environment_id, slug)` |
| `state` | Selection | draft / installing / active / error / archived |
| `env_type` | Selection | production/staging/testing/development (**se muda desde environment**: el "propósito" es del Odoo, no de la máquina) |
| `odoo_version` / `odoo_edition` | Selection | **se mudan desde environment** |
| `main_url` | Char | dominio (se muda desde environment) |
| `http_port` / `gevent_port` | Integer | asignados por el servidor; `UNIQUE(environment_id, http_port)` |
| `service_name` | Char | unit systemd (`odoo-<slug>`; legacy: `odoo`) |
| `conf_path` / `data_dir` / `addons_dir` | Char | rutas REALES por instancia (permiten convivir layout legacy y multi-Odoo sin migrar in-place) |
| `pg_user` | Char | usuario PostgreSQL propio de la instancia |
| `database_id` | M2O database | BD principal |
| `pcm_ref` | Char (`pcm_inst_<uuid>`) | ref estable para cost.share |
| `cost_weight` | Float default 1.0 | método de reparto "por peso" |
| `origin_instance_id` | M2O instance | staging: instancia origen |
| `provision_config_encrypted` | Char cifrado | config del wizard de instalación (se muda) |
| campos runtime B1 | | `runtime_python_version/odoo_version/workers/last_runtime_probe` **se mudan desde ec2.instance** (son del Odoo) |

### 2.3 `primate.cloud.environment` DESPUÉS (servidor)

**Queda:** `name`, `account_id`, `ec2_instance_id` (1:1 máquina), `instance_ids` (One2many),
`state` (draft/provisioning/active/error/archived — de la MÁQUINA), `pcm_ref` (sigue siendo el
valor del tag `primate:environment_id` de la EC2 → atribución de costo POR SERVIDOR),
`cost_split_method` (nuevo, ver §4), campos de descubrimiento (región vía cuenta/máquina).

**Se va a Instancia:** `odoo_version`, `odoo_edition`, `main_url`, `env_type`,
`backup_policy_id` + compliance (`backup_compliance/detail/last_backup_check`), TODOS los campos
de staging (`origin_environment_id`→`instance.origin_instance_id`, `staging_ids`,
`staging_origin_instance_id/database_id`, `staging_config_encrypted`, fechas/logs),
`provision_config_encrypted` (se parte: parámetros de máquina quedan en servidor, los de Odoo
van a instancia).

**Se elimina:** `project_id` como required. Un servidor COMPARTIDO no tiene UN cliente. Queda
`project_ids` computed (proyectos hospedados) + opcional `owner_project_id` (dueño para
dedicados; informativo). **El proyecto deja de ser padre del servidor: es padre de instancias.**

### 2.4 Recableo de relaciones (tabla exacta, campo por campo)

| Modelo | Hoy apunta a | Destino | Notas |
|---|---|---|---|
| `repository.environment_id` | environment | `instance_id` | repos son del Odoo |
| `module.environment_id` (related store) | environment | `instance_id` (related vía repo) | |
| `deployment.environment_id` | environment | `instance_id` | deploy actúa sobre UN Odoo |
| `dns_record.environment_id` | environment | `instance_id` | el dominio es de la instancia |
| `database.environment_id` | environment | `instance_id` | + conserva `account_id` required (inventario importa BDs sueltas); `ec2_instance_id` se mantiene (en qué máquina vive; para RDS queda vacío) |
| `backup.environment_id` | environment | `instance_id` | política/cumplimiento/ejecución por instancia |
| `backup_policy` (asignación) | environment.backup_policy_id | `instance.backup_policy_id` | el modelo policy no cambia |
| `monitor.snapshot.ec2_instance_id` | ec2 | **queda** (CPU/status/red son de la máquina) | |
| `monitor.snapshot.database_id` | database | queda; hereda instancia vía database | |
| `cost.entry.environment_id/_ref` | environment | **queda = SERVIDOR** (el crudo AWS es indivisible) | el reparto vive en `cost.share` (§4) |
| `operation.log` | refs genéricos | + columna `instance_id` opcional | auditoría por Odoo |
| `ec2_instance.environment_id` | environment (viejo) | se INVIERTE: `environment.ec2_instance_id` | máquinas importadas sin servidor: válido |

### 2.5 Migración de datos (solo BDs de prueba — no hay producción)

Script ORM one-shot (NO módulo de migración formal): por cada environment viejo → crea servidor
(mismo name+account, engancha su ec2) + 1 instancia (hereda version/url/env_type/repos/BDs/
backups/DNS/deploys). `pcm_demo` se re-siembra con el seed adaptado; `pcm_test` se recrea.
Los smoke fixtures (`tools/smoke_ui/setup_fixtures.py`) se adaptan en la misma fase.

---

## 3. Impacto por feature YA construida y validada

| Feature (validación previa) | Qué cambia | Qué se conserva | ¿Re-prueba REAL contra AWS? |
|---|---|---|---|
| **Aprovisionamiento F4** (2 pruebas reales + B5) | El flujo se parte en `job_provision_server` (EC2+bootstrap) → `job_install_instance` (Odoo). Wizard reorganizado en 2 bloques (máquina / Odoo) | client_token, resume, config guardada cifrada, bus overlay, servicios AWS puros | **SÍ** (R2: servidor+1ª instancia end-to-end) |
| **Auto-discovery B1–B5** (probado real 2026-07-09) | Solo se re-engancha al wizard nuevo de "crear entorno". El descubrimiento/SG/caché son POR REGIÓN — no saben de instancias | `aws_discovery`, `region.setup`, `ensure_security_group`, lock, semáforo | NO (smoke del wizard alcanza; la idempotencia del SG no se rehace) |
| **Panel de instancia A+B1–B5** | SE PARTE: Dashboard máquina (IP/tipo/lifecycle/métricas/logs os·nginx·postgres) → vista SERVIDOR; runtime B1, logs odoo B2, Config B3, Addons B4, Login-as B5 → vista INSTANCIA. B3/B4 hoy hardcodean `/etc/odoo/odoo.conf` y `/opt/odoo` → parametrizar por `instance.conf_path/addons_dir/service_name` | Toda la plomería SSM; config_read/apply (reciben path por token); health-check+rollback de B3 | **SÍ para B3** (conf por instancia) y **SÍ para B5** (endpoint/allowlist por sitio nginx + BD por instancia). B1/B2: smoke |
| **Backups F8** (prueba real hecha) | policy/compliance/ejecución cuelgan de instancia; scripts ya reciben db/filestore por parámetro → apuntan a `instance.data_dir` | streaming a S3, lifecycle, validador puro, expiración, pre-backup | **SÍ el RESTORE** (destino = instancia, rutas por instancia) |
| **Staging F7+8.B5** (real) | Cambio conceptual: staging = INSTANCIA nueva (servidor destino elegible, default: el del origen). Reusa install-instance + copia/neutraliza | pipeline backup→restore, neutralization.sql, refresh wizard | **SÍ** (staging en el MISMO servidor del origen: el caso nuevo) |
| **DNS** | `dns_record.instance_id`; el A-record apunta a la IP del SERVIDOR de la instancia | Route53 service, UPSERT | NO (mock; se ejercita dentro del E2E de R3) |
| **Costos F9** (parsing real validado) | El pull/cuadre NO cambia (crudo por servidor). Se AGREGA la capa de reparto (§4). `primate:client_id` en EC2 solo para dedicados | CE service, `_persist_cost_result`, cuadre, "datos al" | La validación contra gasto real sigue DIFERIDA (igual que hoy); el reparto se testea con mocks |
| **Cost-attribution / pcm_ref** | `environment.pcm_ref` = ref del SERVIDOR (tag EC2, no cambia). Nuevo `instance.pcm_ref` para las líneas de reparto | `_ensure_pcm_ref` del partner, degradación limpia | NO |
| **Repos/Deploys F5–F6** | apuntan a instancia; scripts de deploy usan `service_name`/rutas de la instancia | GitHub service, marcadores PCM, revert | NO (smoke) |
| **Impersonación B5** (real) | companion se despliega POR INSTANCIA (su BD, su sitio nginx); kill-switch/epoch por instancia (regla permanente: flag leído fresco) | protocolo Ed25519, nonce, audit doble | **SÍ** (multi-instancia: token de A no debe servir en B) |
| **Wizard recuerda config / overlay bus / dashboard OWL** | se re-mapea a las 2 pantallas (servidor/instancia) | mecánica | NO (smoke) |

---

## 4. Costos con servidores compartidos (nace con el modelo)

**Principio: AWS factura la EC2 indivisible → el crudo se guarda POR SERVIDOR; el costo por
instancia/cliente es CALCULADO por PCM.**

- `cost.entry` queda como está (crudo, `environment_ref` = servidor). El cuadre contra CE no
  cambia (sigue siendo la validación de que no perdemos plata en el camino).
- **Modelo NUEVO `primate.cloud.cost.share`** (línea de reparto): `date/period`,
  `cost_entry_id` (o servidor+período+servicio), `environment_id` (servidor), `instance_id`,
  `project_id` (denormalizado para reportes por cliente), `method`, `weight_applied`, `amount`,
  `currency`. `UNIQUE(cost_entry_id, instance_id)`. Se genera por cron/job **después** del pull
  (re-pull → regenera las shares de ese período; nunca a mano).
  - **SIEMPRE derivada y regenerable desde el crudo, NUNCA editable a mano** — mismo patrón que
    los backups inmutables: `write`/`unlink` bloqueados desde la UI, solo el job las crea/borra.
  - **El cuadre sigue siendo contra el crudo de CE por servidor** (no contra la suma de shares).
    Invariante con test propio: **Σ shares de un servidor/período == costo crudo de ese
    servidor/período, al centavo** — sin centavos perdidos ni inventados (el residuo de la
    división en `equal`/`weight` se asigna determinísticamente, p. ej. a la instancia de mayor
    peso/primera por id, y el test lo fija).
- **Tres métodos, configurables — global con override por servidor**
  (`cost_split_method` en settings + campo en environment):
  1. **`equal` (v1 disponible)** — monto / nº de instancias activas del servidor en el período.
  2. **`weight` (v1 disponible)** — proporcional a `instance.cost_weight` (default 1.0).
  3. **`usage` (definido, NO disponible en v1)** — proporcional a consumo medido POR instancia
     (CPU por unit systemd, RAM por unit, tamaño de BD). Selección visible pero deshabilitada
     con nota honesta *"requiere medición por instancia (agente); hoy solo hay métricas por
     servidor"*. Interfaz definida desde ya: `_usage_ratio(instance, period)` para enchufar
     cuando el agente mida por unit. (Nota: tamaño de BD ya sería medible por psql — se anota
     como posible `usage` parcial, decisión para ESA fase, no v1.)
- **Tags en la EC2**: `primate:environment_id` (servidor) queda igual. `primate:client_id`
  SOLO si el servidor es **dedicado** (todas sus instancias del mismo proyecto); al volverse
  compartido, un job de re-tag lo retira (una EC2 no tiene dos clientes — para compartidos
  manda `cost.share`). Al volver a dedicado, se re-emite.
- **Prerequisito de datos**: `project.partner_id` pasa a **required** (sin partner no hay
  client_id ni destino de reparto — hoy es opcional y por eso el demo no emite el tag).

---

## 5. `install_odoo.sh` multi-Odoo (la pieza técnica más difícil)

### 5.1 Partir el script actual en dos

- **`bootstrap_server.sh`** (corre UNA vez por servidor; idempotente, re-ejecutable):
  paquetes de sistema, PostgreSQL (un cluster compartido), nginx base, certbot, **swapfile**
  (obligatorio para multi-Odoo en máquinas chicas), layout `/opt/pcm/`, y el runtime
  **compartido por versión**: `/opt/pcm/runtime/odoo-<ver>/{src,venv}` (un solo checkout+venv
  por versión de Odoo por servidor — dos instancias 19 comparten binarios; disco y RAM de
  t3.micro no bancan venvs duplicados).
- **`install_instance.sh`** (corre por CADA instancia; tokens `%%SLUG%%`, `%%HTTP_PORT%%`,
  `%%GEVENT_PORT%%`, `%%DB_NAME%%`, `%%PG_USER%%`, `%%PG_PASSWORD%%`, `%%DOMAIN%%`,
  `%%ODOO_VERSION%%`, `%%ADMIN_PASSWORD%%`, `%%IS_FIRST%%`):
  usuario PG propio + BD propia; `data_dir` propio (`/opt/pcm/<slug>/data`); `addons_dir`
  propio (`/opt/pcm/<slug>/addons`); conf propio (`/etc/odoo/<slug>.conf`) con
  **`db_filter = ^<db>$` y `list_db = False`** (aislamiento multi-tenant obligatorio),
  puertos propios; unit propio `odoo-<slug>.service`; sitio nginx propio por dominio
  (`default_server` solo si `IS_FIRST`); logrotate propio.

### 5.2 Garantía de no-romper-lo-existente (las dos reglas permanentes aplican acá)

- **Health check ANTES**: el job registra el estado HTTP de TODAS las instancias vivas del
  servidor antes de tocar nada (patrón `PCM_HTTP_WAS` de B3).
- El script **jamás** escribe units/sites/confs cuyo nombre no sea el de SU slug; nginx se
  recarga con `reload` (no restart) y solo tras `nginx -t`.
- **Health check DESPUÉS**: las vivas siguen respondiendo + la nueva responde. Si una viva
  dejó de responder → **rollback** de los artefactos nuevos (unit, site, conf, BD, dirs — solo
  los del slug) + reload + estado `error` con el motivo.
- Asignación de puertos: el SERVIDOR asigna el próximo slot libre (base 8069/8072, paso 10)
  desde el registro PCM; constraint de unicidad por servidor; el script valida además con
  `ss -ltn` que el puerto esté libre (belt & suspenders).
- Concurrencia: **advisory lock por servidor** (mismo patrón sesión+unlock del SG de B3) — dos
  "crear instancia" simultáneos sobre la misma máquina se serializan.

### 5.3 Convivencia con el layout legacy (sin migración in-place)

Las instancias YA aprovisionadas (layout `/opt/odoo` + unit `odoo` + puerto 8069) se registran
con sus rutas reales en los campos por instancia (`conf_path=/etc/odoo/odoo.conf`,
`service_name=odoo`, `http_port=8069`). Como B3/B4/backups/deploys pasan a leer las rutas DE LA
INSTANCIA, ambos layouts conviven sin tocar máquinas vivas. (Sin producción, además, lo normal
será recrear los servidores de prueba con el layout nuevo.)

### 5.4 Prueba real dedicada (cierre de R3 — no negociable)

En t3.micro real (barato a propósito, workers=0 + swap):
1. Crear entorno → servidor + instancia A → HTTP 200 en su dominio.
2. Crear instancia B en ESE servidor → HTTP 200 de B **y** A intacta (hash del conf de A sin
   cambios, uptime del unit de A sin reinicio, HTTP 200 de A re-verificado).
3. Instalación C con fallo inducido → rollback: A y B intactas, C limpia (sin unit/site/BD
   huérfanos).
4. Barrido completo (lección B5: pedir `ec2:DeleteSecurityGroup` para el runbook ANTES, o
   asumir el resto documentado).

---

## 6. Plan por FASES (freno entre cada una; suite verde + smoke como puerta)

| Fase | Contenido | Puerta de salida | FRENO |
|---|---|---|---|
| **R0** | Aprobar este plan; actualizar `ARCHITECTURE.md` + `CLAUDE.md` (vocabulario, nombres canónicos: se agrega `primate.cloud.instance`, `.cost.share`; etiqueta "Máquina AWS"); congelar features nuevas | docs actualizados | ✋ |
| **R1** | **Modelo + vocabulario + migración de datos de prueba**: crear `instance`, mover campos (§2.2–2.3), recablear relaciones (§2.4), adelgazar environment, script de migración ORM, re-seed pcm_demo, fixtures smoke, tests adaptados. Los FLUJOS no cambian aún (crear entorno sigue montando su única instancia por dentro) | suite verde + smoke verde sobre el modelo nuevo | ✋ |
| **R2** | **Separar flujos**: wizard crear-entorno en 2 bloques (máquina/Odoo) → `job_provision_server` + `job_install_instance` encadenados; auto-discovery re-enganchado; acción "Crear instancia" visible pero **gated** ("requiere R3"); resume/idempotencia por job | suite + smoke + **prueba real** servidor+1ª instancia (mini-B5) | ✋ |
| **R3** | **Multi-Odoo**: bootstrap/install split (§5), asignación de puertos, lock por servidor, health-check antes/después + rollback | suite + **prueba real 2 Odoo en un t3.micro + rollback inducido** (§5.4) | ✋ |
| **R4** | **Recablear lo por-instancia**: panel (pantalla servidor vs pantalla instancia), logs split (odoo→instancia; os/nginx/postgres→servidor), B3 config por `conf_path`, B4 addons por `addons_dir`, B5 impersonación por instancia (**la clave de firma Ed25519 y el epoch del kill-switch son POR INSTANCIA, no por servidor** — companion/allowlist/kill-switch por sitio), backups+restore por instancia, **staging = instancia** (wizard con servidor destino) | suite + smoke + **re-pruebas reales: B3, B5, staging-en-mismo-servidor, restore**. La prueba real de B5 incluye EXPLÍCITAMENTE el **caso cruzado**: un token/sesión emitido para la instancia A debe ser RECHAZADO por la instancia B del mismo servidor, y la del cliente X no debe servir para el cliente Y (equivalente multi-tenant del kill-switch) | ✋ |
| **R5** | **Costos con reparto**: `cost.share` + métodos `equal`/`weight` (v1) + `usage` definido-deshabilitado + re-tag dedicado/compartido + `partner_id` required en proyecto | suite (mocks CE); la validación contra gasto real sigue diferida (cuenta sin gasto — ya anotado) | ✋ |
| **R6** | **UI de los dos ejes**: pantalla Proyecto (las instancias del cliente, estén donde estén, con su costo repartido), pantalla Entorno/Servidor (las instancias hospedadas + costo crudo), app OWL re-mapeada, dashboard KPIs, smoke re-escrito completo | suite + smoke 100% | ✋ (cierre) |

Orden = el sugerido en el pedido. R1 y R2 podrían fusionarse si R1 sale limpio, pero se
proponen separados: R1 es puro modelo (riesgo mecánico), R2 toca el flujo validado por 3
pruebas reales — mejor frenar entre medio.

---

## 7. Riesgos (y por qué NO hacerlo ahora sería peor)

1. **Se re-abre código validado por pruebas reales** (provision, staging, restore, B3, B5).
   Mitigación: la tabla §3 marca exactamente qué re-prueba real corresponde a cada fase; nada
   se declara "validado" por herencia — regla del proyecto.
2. **Multi-tenant en una máquina**: un error de aislamiento (dbfilter, list_db, filestore,
   impersonación cruzada) expone datos entre clientes. Mitigación: `db_filter=^db$` +
   `list_db=False` obligatorios en el conf generado (test golden), filestore por `data_dir`
   propio, prueba real B5-impersonación cruzada (token de A rechazado en B). El usuario unix
   compartido en v1 se acepta y se anota como hardening v2 (usuarios por instancia).
3. **RAM en máquinas chicas**: 2 Odoo en t3.micro = swap sí o sí. Mitigación: swapfile en
   bootstrap + workers=0 por default en instancias adicionales + advertencia en el wizard
   (a futuro: chequeo de RAM libre antes de permitir "crear instancia").
4. **nginx compartido**: un site roto tumba el reload para todos. Mitigación: `nginx -t` antes
   de reload, reload (no restart), rollback del site propio.
5. **Puertos**: colisión = instancia que no levanta. Mitigación: registro en PCM (constraint) +
   verificación `ss -ltn` en el script.
6. **Costos**: el reparto introduce una segunda fuente de verdad. Mitigación: `cost.share`
   SIEMPRE derivada (regenerable desde el crudo), nunca editable; el cuadre sigue siendo contra
   CE crudo.
7. **Deriva de documentación**: CLAUDE.md hoy fija nombres que cambian. Mitigación: R0 los
   actualiza ANTES de tocar código.

**Por qué ahora**: no hay datos de producción. Cada instancia real creada con el modelo viejo
(a) nace con la semántica equivocada de staging/backup/costos, (b) exigirá migración de datos
REAL con ventana de mantenimiento, y (c) multiplica las re-pruebas reales (habría que validar
migración + recableo, no solo recableo). Hoy la "migración" es re-sembrar dos BDs de prueba.
Es el momento más barato que va a existir.

---

## 8. Decisiones a aprobar (además del plan en sí)

| # | Decisión | Recomendación |
|---|---|---|
| D-R1 | Servidor: adelgazar environment + 1:1 con ec2.instance (A) vs fusión (B) | **A** |
| D-R2 | Nombre del modelo nuevo | **`primate.cloud.instance`** + re-etiquetar ec2.instance como "Máquina AWS (EC2)" en UI |
| D-R3 | `project.partner_id` required | **Sí** (prerequisito de client_id y reparto) |
| D-R4 | Usuario unix por instancia vs compartido | **Compartido en v1**, hardening v2 anotado. ⚠️ **APROBADO CON PRECISIÓN (explícita, no default olvidado):** en v1 el aislamiento entre clientes es a nivel APLICACIÓN (db_filter + list_db + filestore por dir), NO a nivel sistema operativo. El aislamiento fuerte (usuarios unix por instancia) es v2 y **debe cerrarse ANTES de poner dos clientes con datos sensibles en un mismo servidor compartido**. Va también al RUNBOOK_ONBOARDING como advertencia operativa |
| D-R5 | Runtime Odoo compartido por versión por servidor (src+venv) | **Sí** (t3.micro no banca duplicados) |
| D-R6 | Staging: servidor destino | **Elegible, default = el servidor del origen** |
| D-R7 | Reparto: `equal` default global, override por servidor | **Sí**; `usage` visible-deshabilitado |
| D-R8 | R1 y R2 separados o fusionados | **Separados** (freno entre modelo y flujo) |

---

**FRENO.** Espero revisión/aprobación (en especial D-R1…D-R8 y el orden de fases) antes de
escribir una línea de código. Pendientes previos que NO son de este plan y siguen en cola:
commitear el fix del lock de sesión + el fix `fe_sendauth` (ya verdes), y el
`delete-security-group` de eu-west-1 con credenciales admin.
