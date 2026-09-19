# BACKLOG — primate_cloud_manager

> Local, git-ignored (como el resto de los .md). Pendientes acumulados post v0.1.0.
> Los de **Backend/staging** NO se pagan como deuda suelta: se resuelven al construir la
> Fase 8 (ver propuesta de diseño de la fase).

## 🚦 ORDEN DE PUESTA EN PRODUCCIÓN (gate explícito, acordado 2026-09-19)

**features → ensayo → HTTPS → producción.**

1. **Features**: restauración desde dump externo, importación masiva de servidores y
   backups programados.
2. **Ensayo** con Barraca Julia (Odoo 19 Community, base migrada v13→v19). Corre en la
   **cuenta AWS personal de Daryl**, no en una cuenta de cliente.
3. **HTTPS del propio manager** — sigue siendo **bloqueante**, pero de *producción con
   cliente real*, NO del desarrollo ni del ensayo. Antes se leía como bloqueante de todo
   lo que viniera después del primer cliente; queda acotado acá para que el gate no
   frene el ensayo ni se pierda de vista.
4. **Producción** con cliente real: recién acá se exige el HTTPS resuelto.

## 🚦 GATE — Restore a tamaño real: pendiente para la cuenta de la EMPRESA

**Requisito antes de producción**, junto con el HTTPS del manager y el arreglo del `NEED_MB`.

El ensayo de Barraca Julia se hace **100 % dentro del plan gratuito** de la cuenta personal
(`603011031378`), que no admite tipos grandes (`m6i.xlarge` lo rechazó con
`InvalidParameterCombination`). Por eso valida **el flujo completo** —aprovisionamiento, restore
por F1 desde una key de S3 y arranque de Odoo 19— sobre una base **reducida**
(`bj_v19_micro_smoke.zip`), pero **no** el tamaño real. Queda explícitamente pendiente, para la
cuenta de la empresa:

- el restore de la base completa (zip de 11,2 GB, 22,3 GB descomprimido);
- el **pico de disco** real (estimado en 60-70 GB, sin medir);
- los **tiempos** del restore grande;
- el tipo de instancia objetivo (`m6i.xlarge` o equivalente).

## 🚦 GATE — El chequeo de espacio del restore no cuenta todo el pico (F1)

**Resolver antes de habilitar el restore a operadores en producción.**

El `NEED_MB` del restore desde dump externo valida `/var/tmp` en dos etapas (el zip antes de
bajarlo, y el total de `unzip -l` con el zip ya en disco), pero **no cuenta**:

- **la base restaurada en PostgreSQL**: ocupa espacio en el volumen de PG, que en PCM suele ser
  el mismo disco raíz;
- **la copia del filestore a `data_dir`**: el `rsync` corre mientras el directorio de extracción
  todavía existe, así que el filestore ocupa espacio dos veces en ese momento.

El chequeo puede pasar y el disco llenarse a mitad del restore. Estimación con Barraca Julia
(zip 11,2 GB, 22,3 GB descomprimido, `dump.sql` de 12,8 GB): pico de **~60-70 GB**. Es una
estimación, no una medición; el ensayo va a dar el número real. Para el ensayo se aprovisiona con
100 GB de disco.

Arreglo propuesto: sumar al `NEED2` el tamaño descomprimido del filestore más un estimado de la
base (p. ej. 1,5 × `dump.sql`), y chequear `df` del volumen de PG, no sólo el de `/var/tmp`.

## 📋 Para el reporte del ensayo de Barraca Julia

- **El pre-backup va a ser trivial en esta corrida**: el destino es un servidor recién
  aprovisionado, sin base previa, así que no hay nada que respaldar antes del drop. El peso real
  del pre-backup (que va a S3 con retención de 7 días, `pcm.pre_restore_retention_days`) **se
  mide recién en el primer restore sobre una base existente**. No registrarlo como medido.

## ⏸ POSPUESTO — Backups programados con catálogo (F3)

**Diseño aprobado en sep-2026, NO implementado.** Está completo en
`F3_BACKUPS_PROGRAMADOS_DISENO.md` para retomarlo sin rediseñar.

- **Motivo de la postergación:** espacio en disco del servidor, sobre todo en producción
  (el zip formato Odoo no se puede streamear: obliga a bajar a disco, ≈3× el tamaño), y no
  hace falta para el ensayo de Barraca Julia.
- 🔴 **Requerido antes de pasar un cliente a producción**, junto con el **HTTPS del manager**
  (ver el gate de arriba).
- **Ojo:** el catálogo (`primate.cloud.backup`), la política, `job_run_backup` y "Respaldar
  ahora" **ya existen desde la Fase 8**. Posponer F3 no los elimina: lo pospuesto es el
  formato zip, la retención por N copias, el flag de verificación, que el cron honre la
  frecuencia, el catálogo en la ficha de base/instancia y cuenta+instancia en la key.

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

- **El re-etiquetado falló al archivar el entorno 80 (2026-09-19) por IAM, no por el recurso terminado**:
  `pcm-operator` no tiene `ec2:DeleteTags` (el runbook sólo lista `ec2:CreateTags`). Dos arreglos: sumar
  `ec2:DeleteTags` a la policy/runbook, y que el job saltee con gracia los recursos inexistentes/terminados
  y registre «omitido», no «Fallido».

- **«Agregar instancia»/staging fallidos no tienen reintento y el slug queda bloqueado incluso archivando**
  (octavo hallazgo, grabación del 2026-09-19: la instancia de staging fallida por la región retuvo
  `barraca-julia-staging` —`UNIQUE(environment_id, slug)` cuenta las archivadas— y hubo que borrar el
  registro). **Solución principal:** un reintento que REUSA el mismo registro, simétrico al «Reintentar
  instalación» del cuarto hallazgo (mismo patrón). **Complemento:** al archivar, una instancia que NUNCA
  tocó el servidor libera su slug; una que sí lo tocó lo retiene (es dueña de carpetas y unit). El dato a
  persistir para decidirlo es «tocó el servidor / nunca llegó». Es la recuperación sin trucos del operador.

- 🔴 **La copia de staging deja las tablas a nombre del rol de PRODUCCIÓN: el Odoo del staging no arranca**
  (noveno hallazgo, 2026-09-19). El camino del CATÁLOGO restaura con `pg_restore -d` sin `--no-owner --role`, así que
  las 950 tablas quedan del rol del origen (`odoo_barraca_julia`) y el usuario del staging
  (`odoo_barraca_julia_staging`) **no ve ninguna tabla**: `information_schema` sólo lista lo que el usuario puede ver,
  Odoo cree que falta `orm_signaling_registry`, intenta crearla y PostgreSQL responde «already exists» → *Failed to
  load registry* y toda URL redirige a sí misma. Es la MISMA clase del fix de F1 (`7193740`), en el camino que se dejó
  byte-idéntico. Sólo se nota cuando origen y destino tienen usuarios PG distintos: **el caso multi-Odoo** (producción
  y su staging en el mismo servidor), que es el default de «Crear staging». **Arreglo:** `--no-owner --role=<pg_user>`
  (o `REASSIGN OWNED` post-restore) en el camino del catálogo, con test de dueños. **Complemento:** el chequeo de
  sanidad post-restore (`SELECT count(*) FROM ir_module_module`) corre como `postgres` y por eso pasó: debería correr
  **como el usuario de la instancia destino**, que es quien después tiene que leer la base.
- **No hay forma documentada de entrar por navegador al staging** (noveno hallazgo, parte 2): nginx lo sirve en el 80
  por `server_name` (`staging.<dominio>`) y su puerto propio (8079) está cerrado en `pcm-managed` (sólo 80/443). Sin
  DNS, el staging es inalcanzable salvo forzando el `Host`. Opciones en la KB; la decisión depende del gate de
  dominio/HTTPS.

## IAM

- **El user `pcm` no tiene `iam:GetInstanceProfile`** — no bloquea el provisioning (PassRole
  alcanza), pero impide verificar desde la app que el instance profile existe/está bien
  armado antes de lanzar (hoy da AccessDenied en la validación read-only previa).

## Harness de tests

- **Automatizar el click-through de la doble confirmación destructiva en Playwright** — en
  la prueba real el 2º diálogo de la terminación falló por timing y se terminó server-side;
  la terminación es justamente la acción que más conviene tener automatizada en smoke_ui.

## UI

- **La salida de los comandos SSM se ve como HTML escapado en el chatter** (visto 2026-09-19 en el
  ensayo de Barraca Julia: `&lt;b&gt;Comando SSM&lt;/b&gt;…` en el mensaje de `bj-ensayo`).
  `job_execute_command` (`primate_cloud_ec2_instance.py`) arma el cuerpo como `str` con `%` y se lo
  pasa a `message_post`, que desde Odoo 17 escapa todo `str`. El arreglo NO es envolver el
  resultado en `Markup` a secas: el comando, el stdout y el stderr vienen del servidor y tienen que
  ir **escapados** dentro de un `Markup` (`Markup("…<pre>%s</pre>") % stdout`), o se abre un
  camino para inyectar HTML en el chatter. Buscar el mismo patrón en los demás `message_post` que
  interpolan salida de SSM. Con test: el cuerpo trae `<pre>` real y un `<script>` en el stdout
  sale escapado.
- **Label honesto del botón "Sincronizar" del entorno** — sigue diciendo "Sincronizar"
  (`entorno_detalle.xml:65`); debe decir "Sincronizar servidores desde AWS" hasta que exista
  el sync real a nivel entorno (ver ítem de Backend).

## Tests

- **`TestPcmApp.test_accent_default_teal` falla: el test quedó viejo, no el código.**
  Espera `pcm_accent == "teal"` y el default del campo es **`violet`**
  (`models/res_users.py:33`). El test es del commit fundacional `2cddcfc`; el
  default se cambió después en `7ddf047` («reconciliar tokens al handoff de
  diseño»), y el test no se actualizó. **Verificado preexistente** el 2026-09-19:
  corrido con el árbol sin los cambios de F1 (stash) falla igual
  (`'violet' != 'teal'`, 1 failed of 1). Arreglo: cambiar el esperado a `violet`
  y, de paso, renombrar el test (`test_accent_default_teal` miente). No se tocó
  en F1 por estar fuera de su alcance.


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
