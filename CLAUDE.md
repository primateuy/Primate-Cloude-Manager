# CLAUDE.md

## Knowledge base (leer PRIMERO, ahorra tokens)
- Proyecto en la KB: **primate-cloud-manager** → `/Users/darylyturraldelopez/Desktop/Odoo/Sagui-Vault/Primate/primate-cloud-manager/`
- Antes de explorar código: leé `/Users/darylyturraldelopez/Desktop/Odoo/Sagui-Vault/Primate/primate-cloud-manager/_index.md`
- Antes de grepear: consultá `mapa-archivos.md` de esa carpeta
- Dudas de diseño/flujo: `arquitectura.md` y `decisiones.md`
- Al terminar una tarea significativa: actualizá "Estado actual" en `_index.md` (con fecha) y registrá decisiones de diseño en `decisiones.md`
- No dupliques en la conversación lo que ya está en la KB: referenciala

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> _Convenciones del proyecto. El resto del documento está en español, igual que el código._

Módulo **`primate_cloud_manager`** para Odoo 19 Enterprise: gobierno de infraestructura AWS
para entornos Odoo (tipo CloudPepper, pero dentro de Odoo).

## Estado actual del repo

**Fases 1–9 de `ARCHITECTURE.md` construidas y testeadas** (inventario, acciones EC2,
aprovisionamiento, trazabilidad, deployments, staging, respaldos+restore, DNS CRUD,
observabilidad/costos, panel de instancia, auto-discovery de red — con pruebas reales en AWS).

✅ **RECABLEO CERRADO (R6, 18-jul-2026).** El recableo del modelo (servidor ≠ instancia,
decisión **D6** de `ARCHITECTURE.md`; plan por fases R0–R6 en `RECABLEO_PLAN.md`) terminó con
R6-B3 el 2026-07-18, y después se completó el rediseño de UI (21-jul). El congelamiento de
features que regía durante el recableo ya no aplica.

### Vocabulario del dominio (D6 — usarlo en código, UI y docs)

- **Proyecto = un cliente** (partner). Reúne sus instancias estén en el servidor que estén.
- **Entorno = el SERVIDOR AWS** (la EC2, la máquina). Infraestructura; puede ser COMPARTIDO
  entre clientes. Lo "por servidor": IP, tipo EC2, estado de la máquina, SG/subnet, costo AWS.
- **Instancia = un Odoo montado DENTRO de un servidor** (`primate.cloud.instance`). Dos
  vínculos obligatorios: `project_id` (cliente) y `environment_id` (servidor). Lo "por
  instancia": versión Odoo, BD, dominio, puerto, systemd, odoo.conf, repos, deploys, logs,
  backups, staging, impersonación.
- **Máquina AWS (EC2)** = `primate.cloud.ec2.instance`, el recurso AWS de inventario (1:1 con
  el entorno cuando PCM lo gestiona; también existe suelto para máquinas importadas).
- Crear entorno = aprovisionar EC2 **y** montar su primera instancia. Crear instancia = montar
  OTRO Odoo en un servidor EXISTENTE (nunca lanza EC2). Staging = una instancia.

## Jerarquía de documentos (cuál gana)

1. **`primate_cloud_manager_spec_funcional.docx` / `_spec_tecnica.docx`** — fuente de verdad
   del *alcance*: modelos, campos, flujos, seguridad. (No versionados en git; pedirlos si no
   están disponibles.)
2. **`ARCHITECTURE.md`** — *decisiones* que reconcilian ambas specs, *deltas* a aplicarles y el
   *orden de construcción* (Fases 1–10). **Si la spec y `ARCHITECTURE.md` se contradicen, gana
   `ARCHITECTURE.md`** (p. ej. la spec dice manifest 17.0; la decisión D1 lo corrige a 19.0).
   Para el recableo servidor≠instancia (D6), el detalle operativo vive en **`RECABLEO_PLAN.md`**
   (mismo rango que `ARCHITECTURE.md` para ese alcance; gana a la spec).
3. **`CLAUDE.md`** (este archivo) — *convenciones de código* y reglas de Odoo 19.
4. **`RESUMEN_PROYECTO.md`** — contexto narrativo del *porqué* del proyecto y de las fases.

## Stack y entorno

- **Odoo 19.0 Enterprise.** Python **3.10+** (recomendado **3.12** por rendimiento del ORM).
- `__manifest__.py`: `"version": "19.0.1.0.0"` (NO 17.0/18.0), `"license": "AGPL-3"`.
- `depends`: `['base', 'mail', 'queue_job']`. **`queue_job` (OCA rama 19.0) es obligatorio.**
- `external_dependencies.python`: `['boto3', 'botocore', 'paramiko', 'cryptography', 'PyGithub']`.

## Reglas de Odoo 19 — NO usar patrones viejos

Estas son las trampas al venir de v17/v18. Odoo 19 mantiene los cambios de v18 y suma varios más.

- **Vistas de lista: `<list>`, NUNCA `<tree>`.** En las acciones, `view_mode="list,form"`.
- **No usar `attrs` ni `states`.** Atributos directos con expresión Python:
  `invisible="state == 'done'"` (igual `readonly`/`required`/`column_invisible`).
- **No usar `record._cr`, `record._context`, `record._uid`** (deprecados en 19). Usar
  `self.env.cr`, `self.env.context`, `self.env.uid`.
- **No importar de `odoo.osv`** (deprecado). Importar de `odoo` (`from odoo import api, fields, models, Command`).
- **Escrituras x2many con objetos `Command`**, no tuplas `(0, 0, {...})`:
  `Command.create({...})`, `Command.link(id)`, etc.
- **`read_group` está deprecado** → usar `_read_group` (backend) o `formatted_read_group` (API pública).
- **Restricciones e índices con la API nueva:** `models.Constraint()` y `models.Index()`.
- **`name_get` ya no existe** → definir `_compute_display_name`.
- **QWeb: `t-out`, no `t-esc`** (relevante en reports/portal).
- Crons sin `numbercall`/`doall`; para tareas largas en cron, considerar la nueva API de cron
  con commits por lotes y notificación de progreso.
- `create` recibe lista de vals → `@api.model_create_multi`.
- La demo data ya no se carga por defecto: no depender de ella en los tests.

> El ORM se reorganizó internamente bajo `odoo/orm/`, pero los imports de alto nivel
> (`from odoo import fields, models, api, Command`) siguen funcionando sin cambios.

## Nombres reales (respetar exactamente)

Modelos: `primate.cloud.account`, `.project`, `.environment` (= SERVIDOR, ver D6),
`.instance` (= un Odoo, nuevo en R1), `.ec2.instance` (= máquina AWS de inventario),
`.database`, `.repository`, `.module`, `.commit`, `.deployment`, `.dns.record`,
`.backup.policy`, `.backup`, `.cost.entry`, `.cost.share` (nuevo en R5, derivado e
inmutable), `.monitor.snapshot`, `.operation.log`, `.region.setup`.
Archivos: `models/primate_cloud_<modelo>.py`. Clases: `PrimateCloud<Modelo>`.

## Capa de servicios (`services/`)

- Adaptadores Python **puros** por servicio AWS (`aws_base`, `aws_ec2`, `aws_rds`,
  `aws_route53`, `aws_cloudwatch`, `aws_ssm`, `aws_cost_explorer`, `aws_s3`).
- **Sin lógica de negocio, sin ORM, no escriben en BD.** Reciben credenciales ya
  descifradas, devuelven datos normalizados. Los modelos los consumen por inyección.
- Esto habilita testeo unitario con mocks y la extensión futura a Azure/GCP.

## Concurrencia y tareas largas (crítico)

- **Toda operación AWS/SSM/SSH mutante o larga va en `queue_job`** (aprovisionamiento,
  staging, sync, deploy, backup, restore, escritura de config + restart, clone de addons).
  Nunca en un worker web ni en `create`/`write`.
- Patrón: el método de acción (botón) solo valida, cambia estado a `provisioning`/etc. y
  encola con `.with_delay()`. El job hace el trabajo, actualiza estado y escribe en
  `primate.cloud.operation.log`.
- En el job: capturar excepciones, guardarlas en el log/`error_message`, dejar estado
  `error`. Nunca tragarse errores en silencio.

### Excepción oficial: lecturas interactivas síncronas (política)

Las **lecturas read-only, cortas y acotadas** que alimentan la UI en vivo **pueden correr
síncronas en el worker web** (no por `queue_job`), porque la latencia de un job las haría
inusables. Aplica **solo** a: leer logs (`fetch_logs`), leer/parsear el `odoo.conf`, y listar
usuarios de la BD remota (`res_users`) para el *Login as*. Reglas para que sea seguro:

- **Read-only**: no muta nada en AWS ni en la instancia (ni escribe en BD Odoo).
- **Timeout corto y cota de salida** (no el `timeout=120` de los jobs): no bloquear el worker.
- **Todo lo que muta sigue por `queue_job`**: escribir el `odoo.conf` + restart, respaldar,
  restaurar, clonar addons, ciclo de vida de EC2, generar/consumir el token de impersonation.

Esto está escrito como **política, no como excepción silenciosa**: si dudás si algo califica,
mutante ⇒ `queue_job`; read-only interactivo corto ⇒ síncrono.

## Ejecución remota

- **Preferir SSM** (`aws_ssm.send_command` / `run_script`) sobre SSH para correr comandos
  en las EC2. `paramiko` solo como fallback.
- Requisito a asumir: agente SSM presente en la AMI + instance profile con permisos SSM.
- Lectura de módulos instalados en una instancia: query a `ir_module_module` de la BD
  remota vía SSM (no asumir; ejecutarlo y parsear).

## Etiquetado obligatorio de recursos

Todo recurso AWS creado por el módulo se etiqueta, como mínimo, con:
`primate:client`, `primate:environment`, `primate:managed_by=pcm`. Centralizar la
construcción de tags en un helper de `aws_base`. Sostiene la atribución de costos.

## Seguridad

- Credenciales IAM cifradas con `cryptography`; campos solo visibles para
  `group_cloud_admin`; el secret con `password=True`. Nunca en exports ni logs.
- Credenciales root **prohibidas**. Solo IAM con permisos mínimos.
- La sesión boto3 se crea en memoria dentro del método y no se persiste.
- `primate.cloud.operation.log` es **inmutable**: bloquear `write`/`unlink` desde la UI.
- Operaciones destructivas (`terminate`, restore, checkout en prod): confirmación explícita
  + registro en log.

## Tests

- `tests/` heredando de `odoo.tests.common.TransactionCase`.
- **Mockear siempre AWS/SSM/SSH** (`unittest.mock`). Los tests no tocan infra real.
- Además hay **tests de integración con `moto`** (`test_integration_moto.py`): ejercitan boto3
  de verdad contra un AWS simulado (sin red ni infra real), incluyendo el flujo end-to-end de
  sincronización. Se saltan solos si `moto` no está instalado (`skipUnless`).
- Cada fase se cierra con sus tests pasando antes de avanzar.

## Entorno de ejecución (rutas reales en esta máquina)

- **venv del proyecto:** **`.venv/`** (en la raíz del repo, **Python 3.12**). Tiene los
  requirements de Odoo + las deps del módulo (boto3, paramiko, cryptography, PyGithub) + `moto`
  (para tests de integración). Se usa Python 3.12 a propósito: en 3.11 Odoo fija
  `cryptography==3.4.8` (incompatible con moto); en 3.12 fija `cryptography==42.0.8` +
  `urllib3==2.0.7`, stack moderno que convive con moto.
- **`odoo-bin`:** `../forum/_shared/community/odoo-bin` (fuente Odoo 19 Community, solo lectura
  como referencia). Se corre con el python del venv: `\.venv/bin/python <odoo-bin> ...`.
- **Config del proyecto:** **`PrimateCloudeManager.conf`** (raíz del repo). HTTP `:19088`,
  gevent `:19093`, PostgreSQL local (`db_user=odoo`), `workers=0`. (`forum.conf` es de otro
  proyecto; no usarla.)
- **`addons_path` (ya configurado** en `PrimateCloudeManager.conf`): `community/addons` +
  `shared/oca/queue` (queue_job 19.0) + el repo del módulo. ⚠️ Enterprise 19.0 **no está en
  disco** (solo 17.0/18.0); no es dependencia del módulo, así que no bloquea instalar/testear.
- ⚠️ Los puertos (19088/19093) coinciden con `forum.conf`: **no levantar ambas instancias a la
  vez** o chocan.

## Comandos útiles

Desde la raíz del repo. Reemplazar `<db>` por la base de datos de trabajo.

```bash
PY=.venv/bin/python
ODOO_BIN=../forum/_shared/community/odoo-bin
CONF=PrimateCloudeManager.conf

# Levantar con recarga de assets/qweb/py para desarrollo
$PY $ODOO_BIN -c $CONF --dev=all

# Instalar el módulo por primera vez (desde cero, sin demo)
$PY $ODOO_BIN -c $CONF -d <db> -i primate_cloud_manager --without-demo=all --stop-after-init

# Actualizar el módulo tras cambios en Python/XML
$PY $ODOO_BIN -c $CONF -d <db> -u primate_cloud_manager --stop-after-init

# Correr toda la suite de tests del módulo (incluye integración moto)
$PY $ODOO_BIN -c $CONF -d <db> -u primate_cloud_manager --test-enable \
  --test-tags /primate_cloud_manager --stop-after-init

# Correr un subconjunto por clase/método
$PY $ODOO_BIN -c $CONF -d <db> -u primate_cloud_manager \
  --test-enable --test-tags /primate_cloud_manager:NombreClaseTest.test_metodo --stop-after-init
```

## Cómo trabajar en este repo

- Implementar **una fase de `ARCHITECTURE.md` a la vez**, no todo de golpe.
- Antes de escribir, revisar los modelos/servicios ya existentes para no duplicar.
- Si una decisión de arquitectura no está en la spec ni en `ARCHITECTURE.md`, **parar y
  preguntar** en vez de inventar.
