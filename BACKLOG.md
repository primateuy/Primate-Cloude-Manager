# BACKLOG — primate_cloud_manager

> Local, git-ignored (como el resto de los .md). Pendientes acumulados post v0.1.0.
> Los de **Backend/staging** NO se pagan como deuda suelta: se resuelven al construir la
> Fase 8 (ver propuesta de diseño de la fase).

## ✅ RESUELTO

- **F5 — Integridad del validador de respaldos: falso "No cumple" por BD huérfana.**
  **ARREGLADO (commit `0c6fd06`, 2026-07-02)** con los dos frentes: (1) causa raíz —
  `_upsert_provisioned_database` hace que el re-provision reuse el registro por (entorno,
  nombre) en vez de duplicar; (2) red de seguridad — `_database_on_terminated_instance`:
  el validador (`_collect_backup_evidence`/`_evaluate_backup_compliance`) omite del
  cumplimiento las BD sobre instancia `terminated` (las lista como "se omite", no como
  incumplimiento) y el ejecutor (`job_run_backup`) no las respalda. Tests: caso mixto
  (viva+huérfana → "Cumple"), regla pura, ejecutor, y upsert; +verificado en `pcm_demo`
  con el escenario real. Descripción original abajo (histórico).

  <details><summary>Descripción original (histórico)</summary>
  **No es higiene, es correctitud del validador.** Un sistema de respaldos que reporta
  incumplimientos FALSOS es tan peligroso como uno que oculta los reales: erosiona la
  confianza y entrena al operador a ignorar el badge. El re-provision de un entorno crea un
  registro `primate.cloud.database` nuevo (apuntando a la instancia nueva) sin reusar ni
  archivar el viejo; el viejo queda apuntando a una instancia `terminated` y, como nunca
  tendrá backups, arrastra el entorno a "No cumple" **para siempre**. Confirmado en la prueba
  real (env 5: 2 registros `odoo19_demo`, id 6→instancia terminada, id 7→viva; el validador
  daba "No cumple" hasta borrar el huérfano). **Arreglo (elegir/combinar):**
  1. `_provision_database` reusa el registro existente por (entorno, nombre) en vez de crear
     uno nuevo (ataca la causa raíz: no dejar huérfanos).
  2. El validador (`_collect_backup_evidence`) y el ejecutor de backups **saltan o marcan
     aparte** las BD cuya `ec2_instance_id.instance_state == 'terminated'` (defensa: una BD
     sin infra viva no es "incumplimiento", es "no aplica"). ← recomendado como red de
     seguridad independiente de la causa.
  Cerrar con test: entorno con una BD sobre instancia terminada + una viva con backup fresco
  → debe dar "Cumple", no "No cumple".
  </details>

## Backend

- **Staging resuelve servidor/BD por `[:1]`** — `environment` toma el primer servidor y la
  primera BD del entorno; asume 1 servidor/1 BD y rompe/elige mal en multi-instancia. Se
  rediseña en Fase 8 usando `database.ec2_instance_id` (el hub ya expone la relación).
- **Constraint de coherencia de entorno en `database.ec2_instance_id`** — hoy se puede
  vincular una BD a una EC2 de OTRO entorno sin que nada lo impida; falta constraint
  `database.environment_id == ec2_instance_id.environment_id`.
- **Asociar BD suelta desde la UI (write)** — en el hub las bases sin servidor (RDS/sueltas)
  se listan pero no hay acción para vincularlas a una instancia (`write` de
  `ec2_instance_id` con selector).
- **No existe sync a nivel entorno** — el botón del hub hace loop de
  `ec2.action_sync_from_aws` sobre los servidores del entorno; no sincroniza BDs/DNS ni
  detecta recursos nuevos del entorno. Falta un `environment.action_sync` real.

## IAM

- **El user `pcm` no tiene `iam:GetInstanceProfile`** — no bloquea el provisioning (PassRole
  alcanza), pero impide verificar desde la app que el instance profile existe/está bien
  armado antes de lanzar (hoy da AccessDenied en la validación read-only previa).

## Harness de tests

- **Automatizar el click-through de la doble confirmación destructiva en Playwright** — en
  la prueba real el 2º diálogo de la terminación falló por timing y se terminó server-side;
  la terminación es justamente la acción que más conviene tener automatizada en smoke_ui.

## UI

- **Label honesto del botón "Sincronizar" del entorno** — sigue diciendo "Sincronizar"
  (`entorno_detalle.xml:65`); debe decir "Sincronizar servidores desde AWS" hasta que exista
  el sync real a nivel entorno (ver ítem de Backend).

## Tests

- **Test de tags con strings literales** — `test_aws_base.py` compara las claves contra las
  constantes de `aws_base` (constante vs constante): un typo en la constante pasaría el
  test. Agregar asserts con los literales `primate:client` / `primate:environment` /
  `primate:managed_by`. (Verificado 2026-07-01: no hay typo en el código ni recursos AWS
  con clave mal escrita en us-east-1/2; el `primaate:` fue typo del reporte.)

## IAM / AWS (de la prueba real de Fase 8, 2026-07-02)

- **Política IAM del user sin permisos S3** — documentar en la guía de la política mínima
  el bloque S3 que la Fase 8 necesita (`Get/HeadObject`, `ListBucket`,
  `PutLifecycleConfiguration`, `DeleteObject` sobre `arn:aws:s3:::pcm-*`).
  **DECISIÓN FIJA (2026-07-02): PCM NO crea buckets** — el bucket de una política lo crea
  un admin a mano; el error accionable de `head_bucket` es el comportamiento correcto
  (documentado también en FASE8_PROPUESTA.md §1.2).
- **Config guardada del wizard es cruda** — si un flujo futuro reutiliza
  `_load_provision_config()` fuera del wizard, debe aplicar la transformación de
  `_prepare_params` (p. ej. `security_group_ids` string→lista). Considerar guardar la
  versión transformada.

## Re-aprovisionamiento (prueba real Fase 8, 2026-07-02)

- **F5 — duplicación de registro de BD en el re-provision.** Ver **PRIORIDAD ALTA** arriba
  (es la causa raíz del falso "No cumple" del validador).

- **Barrido de limpieza S3 de una sola pasada** (fricción F6, prueba real Fase 8) — un
  `list_objects` + delete único dejó objetos sin borrar (posible consistencia list-after-write
  cross-region). No afecta al módulo (la retención es por lifecycle de S3, no borrado masivo);
  pero si algún día se agrega una acción "vaciar backups" app-side, debe iterar hasta que el
  listado dé vacío.

- **Re-`-u` de bases vivas tras agregar campos** (prueba real DNS, 2026-07-03) — se agregó el
  campo `is_alias` y se corrió la suite en `pcm_test`, pero `pcm_demo` no se re-actualizó; la
  primera corrida de la prueba real falló con `column is_alias does not exist`. Recordatorio
  operativo (no bug del módulo): tras agregar/cambiar campos, `-u` todas las bases donde se
  vaya a probar en vivo, no solo la de tests.

## Verificación real diferida (prueba real Fase 9, 2026-07-03)

- **Cuadre de costos (`_reconcile_costs`) NO verificado contra montos reales.** El mecanismo
  está implementado y cubierto por tests con mock (suma=total, detecta descuadre, entorno
  borrado cuadra, saltar ceros no rompe). Pero la pasada real fue contra la cuenta free-tier
  `603011031378` (~USD 0.0), así que el cuadre real solo se ejercitó en el **caso cero**.
  **PENDIENTE: validar el cuadre contra una cuenta con gasto real** (montos no-cero,
  multi-servicio) apenas se conecte la primera. No es deuda de código — es verificación real
  diferida por falta de datos. **Que no se registre como "ya validado".**
- **Atribución fina por cost allocation tag — segunda pasada.** En la prueba real el valor tras
  `primate:environment_id$` vino vacío (tags aún no propagados; activación hasta 24h, no
  retroactiva, paso de admin del runbook). Pendiente confirmar en una **segunda pasada** que los
  `pcm_ref` aparecen en el desglose de CE una vez propagados. (Ojo: falta confirmar con el
  usuario si llegó a activarlos en Billing.)

## HARDENING DE SEGURIDAD — secretos por comando SSM (Bloque B4, 2026-07-06)

- **PRIORIDAD (hardening de seguridad, NO nice-to-have): cualquier secreto que hoy viaje por el
  COMANDO SSM queda en el historial de SSM ~30 días, legible por otros roles de la cuenta.** Hoy
  aplica al **token de GitHub de un repo privado de cliente** (Bloque B4, clone) inyectado en
  base64 en el comando. El askpass temporal cubre el **disco** de la instancia (no persiste, no en
  URL/git-config/argv), **pero NO cubre el registro de comandos de SSM** (SendCommand queda en la
  consola/historial). **Fix:** pasar el secreto por **SSM Parameter Store SecureString** (PCM lo
  escribe cifrado, el instance profile lo lee con `ssm:GetParameter --with-decryption`, PCM lo
  borra) → el comando SSM solo lleva el NOMBRE del parámetro, nunca el secreto. Agrega IAM
  (`ssm:PutParameter/DeleteParameter` a pcm-operator, `ssm:GetParameter` + kms decrypt al instance
  profile). **Se paga cuando haya clientes con repos privados.** Revisar si algún otro flujo
  inyecta secretos por comando y migrarlo igual.
- **Inventario de secretos en tránsito por comando SSM (actualizado R3-B3, 2026-07-14):**
  además del token GitHub, hoy viajan inline en el script de `install_instance.sh` (y en el
  legacy `install_odoo.sh`): la **contraseña PG transitoria** de la instancia (`CREATE USER`
  + conf) y el **admin_password** del `odoo.conf` (heredoc del conf). Ninguno se persiste en
  registros PCM ni se loguea (golden lo fija), pero ambos quedan en el historial SSM ~30 días
  y en los `args` del `queue.job` hasta su limpieza (patrón aceptado en R2 para la password
  transitoria; el requeue los necesita). La migración a SecureString debe cubrir LOS TRES.

## Verificación real diferida (Bloque B4, 2026-07-06)

- **E2E real de B4 (agregar addon) — NO validado en vivo, diferido al primer uso real.** El clone
  + addons_path retroactivo + detección de módulos está cubierto por unit tests (mocks) y el clone
  script probado estructuralmente, pero NO se corrió contra una instancia real (el peor caso de B4
  es un addon "no verificado", reversible y sin daño, así que no se justificó un t3.micro
  dedicado). Queda como **verificación diferida** al primer uso real: confirmar en vivo que el
  clone entra, el `addons_path` retroactivo se activa vía B3 y Odoo carga el módulo. NO registrar
  como "validado". (El rollback del que B4 depende — B3 config — SÍ se validó en vivo.)
