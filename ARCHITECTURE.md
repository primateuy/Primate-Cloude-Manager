# Primate Cloud Manager — Plan de construcción y decisiones

> Este documento **no** redefine el alcance: el *qué* (modelos, campos, flujos, seguridad)
> vive en `primate_cloud_manager_spec_funcional.docx` y `primate_cloud_manager_spec_tecnica.docx`,
> que son la fuente de verdad. Acá se registran las **decisiones** que reconcilian ambas specs,
> los **deltas** a aplicarles, y el **orden** en que Claude Code construye el módulo.
> Las convenciones de código están en `CLAUDE.md`.

---

## 1. Decisiones tomadas

- **D1 — El manager corre sobre Odoo 19 Enterprise.** El manifest de la spec técnica dice
  17.0; se corrige a `19.0.1.0.0`. Python 3.10+ (recomendado 3.12). Aplican las reglas de
  sintaxis y deprecaciones de Odoo 19 (`<list>`, sin `attrs`/`states`, sin `record._cr`/
  `_context`/`_uid`, sin `odoo.osv`, `Command` para x2many, `_read_group`, `models.Constraint`/
  `models.Index`, `t-out`, `_compute_display_name`). El atributo `odoo_version` de los
  *entornos gestionados* sigue pudiendo ser 17/18/19 — es independiente de la versión del módulo.

- **D2 — `queue_job` es obligatorio.** Todo flujo largo (aprovisionamiento, staging, sync,
  deploy) es asíncrono. Los flujos síncronos con polling/timeout que describe la spec se
  reimplementan como jobs. `queue_job` entra en `depends`.

- **D3 — Autenticación multi-cuenta con dos métodos.** `primate.cloud.account` gana un campo
  `auth_method`:
  - `access_key` (como la spec: claves IAM cifradas) — **default para v1**.
  - `assume_role` (rol IAM cruzado en la cuenta destino + `role_arn` + `external_id`) —
    **recomendado para cuentas de cliente**, evita guardar secretos de larga vida.
  Arrancamos con `access_key`; `assume_role` queda implementado como opción seleccionable.

- **D4 — Etiquetado obligatorio al crear recursos.** Convención mínima: `primate:client`,
  `primate:environment`, `primate:managed_by=pcm`. Es lo que sostiene la atribución de
  costos de la sección 13 de la spec funcional.

- **D5 — Ejecución remota vía SSM** (con `paramiko` como fallback). Requisito asumido:
  agente SSM en la AMI + instance profile con permisos SSM.

- **D6 — Recableo del modelo: servidor ≠ instancia (APROBADO 2026-07-10).** El modelo de las
  specs mezcla "servidor AWS" y "Odoo" en una sola entidad (`environment`); se recablea a dos
  ejes que se cruzan en la INSTANCIA:
  - **Proyecto = un cliente** (eje facturación/gestión; `partner_id` pasa a required).
  - **Entorno = el SERVIDOR AWS** (la EC2, la máquina; eje infraestructura). Puede ser
    **compartido** entre clientes. 1:1 con `ec2.instance` (que queda como recurso AWS de
    inventario, etiqueta UI "Máquina AWS (EC2)").
  - **Instancia (`primate.cloud.instance`, nueva) = un Odoo dentro de un servidor**, con DOS
    padres obligatorios: `project_id` (cliente) y `environment_id` (servidor). Todo lo
    "por Odoo" (versión, BD, dominio, repos, deploys, DNS, backups, staging, panel,
    impersonación) cuelga de la instancia; solo lo de la máquina cuelga del entorno.
  - Comportamiento: crear entorno = EC2 + primera instancia; crear instancia = OTRO Odoo en
    un servidor EXISTENTE (sin EC2 nueva); staging = instancia.
  - Costos: el crudo de CE queda POR SERVIDOR; el costo por instancia/cliente se CALCULA
    (`primate.cloud.cost.share`, derivada e inmutable; métodos equal/weight en v1, usage
    definido-deshabilitado). `primate:client_id` en la EC2 solo para servidores dedicados.
  - ⚠️ Aislamiento v1 entre clientes = a nivel APLICACIÓN (db_filter + list_db + filestore por
    dir), NO a nivel SO; el aislamiento fuerte (usuarios unix por instancia) es v2 y debe
    cerrarse ANTES de compartir un servidor entre clientes con datos sensibles.
  - **Donde este D6 contradiga a las specs o a fases previas de este documento, gana D6.**
    Plan detallado, delta de campos, impactos y fases: **`RECABLEO_PLAN.md`**.

---

## 2. Deltas a aplicar sobre las specs

Cambios concretos respecto de lo que dicen los documentos:

1. Manifest: `version` 17.0 → **19.0.1.0.0**; añadir **`queue_job`** (OCA rama 19.0) a
   `depends`; añadir **`PyGithub`** a `external_dependencies.python` (para
   `repository.action_sync_commits` vía GitHub API).
2. `primate.cloud.account`: añadir `auth_method`, `role_arn`, `external_id` (ver D3).
3. `aws_base`: añadir helper de construcción de tags (D4) y, si se usa `assume_role`,
   soporte `sts:AssumeRole` para credenciales temporales.
4. Trazabilidad: explicitar que `module_ids` (instalado vs disponible) se obtiene
   consultando `ir_module_module` de la BD remota **vía SSM**.
5. Neutralización: el SQL de la sección 7.3 (técnica) tiene sentencias frágiles
   (filtro de crons por nombre de modelo; `mail.catchall.alias` no "bloquea envío").
   Revisar y testear antes de darlo por bueno; versionarlo en `data/neutralization.sql`.

---

## 3. Orden de construcción

Una fase = un bloque de trabajo para Claude Code. Cada fase cierra con sus tests pasando.
Principio: **primero leer/inventariar lo que ya existe en AWS, después crear.**

> **Estado real:** Fases 1–9 construidas y testeadas (tag `v0.1.0` + fases 8/8.5/9, panel de
> instancia A+B1–B5, auto-discovery de red B1–B5 con prueba real en región virgen). La Fase 8
> se redefinió (ver abajo); diseño en `FASE8_PROPUESTA.md`.
>
> ⚠️ **RECABLEO EN CURSO (D6) — FEATURES CONGELADAS.** Desde 2026-07-10 no se agrega
> funcionalidad nueva fuera de las fases R0–R6 de `RECABLEO_PLAN.md` (cada fase con freno y
> aprobación). Las fases de abajo describen lo YA construido con el modelo viejo; su
> semántica de "entorno" se recablea según D6.

- **Fase 1 — Fundaciones.** Esqueleto del módulo, `queue_job`, cifrado de credenciales,
  `operation.log` (inmutable), `account` (con D3), `aws_base` + `test_connection`, helper de
  tags (D4), grupos de seguridad (viewer/operator/admin).
- **Fase 2 — Inventario y sincronización (solo lectura).** `project`, `environment`
  (esqueleto), `ec2.instance` + `aws_ec2` (list/get/sync), `database` (sync RDS), `dns.record`
  (sync). Cron de sincronización. Importa lo que ya corre antes de poder crear nada.
- **Fase 3 — Acciones sobre EC2.** start/stop/restart/terminate/execute_command vía SSM,
  todo async + log. Wizards de confirmación.
- **Fase 4 — Aprovisionamiento de entornos.** Wizard `ec2_create` + flujo `provision`
  (EC2 → DB → `install_odoo.sh` por SSM → nginx → SSL → DNS), async. `aws_rds`, `aws_route53`.
- **Fase 5 — Trazabilidad.** `repository` + `commit` + `module`: `sync_commits` (GitHub API),
  `detect_modules` (SSM), `check_sync_state`; cruce repo vs base, estado `divergente`.
- **Fase 6 — Deployments.** `deployment` + tipos (pull / checkout rama / checkout commit /
  module_update / service_restart), async, con commit origen/destino y log; opción de revertir.
- **Fase 7 — Staging.** Wizard `staging_create` + flujo de 12 pasos async + neutralización
  (SQL versionado y testeado) + `refresh_staging`.
- **Fase 8 — Respaldos + staging consciente de instancias.** `backup.policy` (+ campos de
  ejecución) y registro de backups efectivos (`primate.cloud.backup`); PCM ejecuta backups
  gestionados (pg_dump + filestore vía SSM → S3) además de validar (cron esperado vs
  detectado, estados Cumple/No cumple/No verificable/Sin política); restore con doble
  confirmación (+ tipear el nombre del entorno si el destino es producción). El staging
  reemplaza la convención `[:1]` por selección explícita de instancia/BD de origen
  (persistida en `staging_origin_*`), refresh con opciones (spec §14.5) y neutralización
  ampliada (cola de mail, API keys, dominio website). Un staging replica UNA instancia+BD.
- **Fase 8.5 — DNS CRUD (mini-fase, inmediatamente después de la 8).** CRUD de `dns.record`
  contra Route53 sobre lo existente: `update_record`/`delete_record` en `aws_route53` +
  acciones del modelo + UI.
- **Fase 9 — Observabilidad y costos.** `monitor.snapshot` (CloudWatch), visor de logs
  (SSM/CloudWatch), `cost.entry` (Cost Explorer) con atribución por tags y reparto de
  recursos compartidos.
- **Fase 10 — Multi-cloud y satélites (futuro).** Módulos `primate_cloud_azure`,
  `_gcp`, `_contracts`, `_portal`, `_ai`, sobre la abstracción de la capa de servicios.

> El v1 de las specs equivale a las Fases 1–9. El roadmap de módulos satélite de las specs
> corresponde a la Fase 10.

---

## 4. Riesgos a vigilar

- **Credenciales cross-account:** con `assume_role`, usar `external_id` y permisos mínimos;
  con `access_key`, cifrado + visibilidad solo admin + prohibición de root. Nunca secretos
  en logs.
- **Deriva de estado:** el inventario remoto se desfasa entre refrescos; mostrar siempre la
  fecha del último sync y permitir refresco manual.
- **Operaciones destructivas** (terminate, restore, checkout a prod): doble confirmación +
  log + idealmente doble aprobación en entornos `production`.
- **Cost Explorer:** datos con retardo de horas y API con costo; cachear y consultar por lotes.
- **Dependencia de SSM:** si el agente o el instance profile faltan, la ejecución remota y la
  detección de módulos no funcionan; validarlo en el flujo de aprovisionamiento.
