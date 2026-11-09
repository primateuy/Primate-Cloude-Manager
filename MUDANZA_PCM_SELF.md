# Mudanza de PCM Producción a su propio servidor (PCM self-hosted)

**Objetivo:** mudar la base `pcm_produccion` (hoy en la Mac, :19190) a un servidor
montado POR PCM en la cuenta real, con **Odoo 19 Community** + addons de PCM. Ese
servidor pasa a ser el PCM productivo y se gestiona a sí mismo.

**Estado:** análisis y preparación (2026-11-09). Nada montado en AWS. Pendiente de OK
para el ensayo en sandbox (punto 9) y para el día del corte.

---

## 0. VEREDICTO: VIABLE en Community, sin pérdida funcional para PCM

### 0.a — El código de PCM no depende de Enterprise

- `__manifest__.py`: `depends = [base, mail, queue_job]` (OCA). Deps Python puras
  (boto3, botocore, paramiko, cryptography, PyGithub).
- **Cero referencias** a módulos Enterprise en Python, XML, JS y SCSS: ningún
  `web_enterprise`/`web_studio`; ninguna vista `<cohort>`, `<gantt>`, `<map>`, `<grid>`;
  imports JS solo de `@odoo`, `@web` y el propio módulo. La palabra «enterprise»
  aparece solo como **valor de dominio** (edición de las instancias cliente que PCM
  gestiona), no como dependencia.
- `queue_job` (OCA 19.0) es LGPL y corre igual en Community.

### 0.b — Enterprise instalado hoy en `pcm_produccion` (43 módulos totales)

13 módulos del repo enterprise, **todos auto-instalados**, ninguno elegido:

| Módulos | Para qué estaban | Qué queda huérfano |
|---|---|---|
| `web_enterprise`, `web_mobile`, `mail_mobile`, `mail_enterprise` | shell backend Enterprise + app móvil | nada: solo vistas/campos |
| `web_cohort`, `web_gantt`, `web_grid`, `web_map` | tipos de vista | nada: PCM no los usa |
| `ai`, `ai_auto_install`, `ai_fields`, `ai_server_actions` | IA de Odoo | 2 crons y botones de prompts (sin uso) |
| `iap_extract` | OCR/digitalización | nada (sin uso) |

**Datos huérfanos: ninguno de negocio.** Todo lo que poseen es metadato (ir.model.fields,
vistas, 2 crons de `ai`). Verificado por `ir_model_data` sobre una copia.

### 0.c — Probado en local (2026-11-09)

1. **Base Community limpia** (addons_path sin enterprise): `-i primate_cloud_manager`
   + suite completa → **912 tests, 0 fallos, 0 errores**.
2. **Copia de `pcm_produccion` sobre Community puro, directo:** el registro carga y
   PCM actualiza, pero los 13 módulos quedan «not installable, skipped» y la base
   los sigue creyendo instalados → assets de `web_enterprise` rotos al abrir la web.
   **No es el camino.**
3. **Camino correcto, verificado:** desinstalar los 13 módulos **antes del dump**, en
   la Mac (con el addons_path actual que aún tiene enterprise). Dos pasadas:
   los 10 de la tabla (cascadea `ai_fields`/`ai_server_actions`) y después
   `ai_auto_install`. Quedan 30 módulos, 0 OEEL. Con ese estado, sobre Community puro:
   carga sin errores, **login + app PCM montada + pantallas Entornos/Proyectos/Costos
   con 0 errores de consola** (smoke Playwright, con filestore copiado).

### 0.d — Qué se pierde y cómo se reemplaza

- **Shell Enterprise del backend** → PCM usa su propio shell OWL fullscreen: la app
  PCM se ve idéntica. El backend «crudo» (Ajustes, Usuarios) queda con el look
  Community: cosmético.
- **App móvil de Odoo** (web_mobile) → navegador del teléfono.
- **IA de Odoo / OCR** → no se usaban.
- **Bonus**: el runtime que el montaje de PCM instala en los servidores **ya es
  Community** (clona `odoo/odoo` rama 19.0): el PCM productivo pasa a correr sobre
  exactamente lo que su propio montaje sabe construir y mantener. Y sin módulos
  OEEL-1 no hay dependencia de una suscripción Enterprise para el propio PCM.

---

## 1. INVENTARIO: lo que el servidor necesita vs lo que el montaje ya hace

### Ya lo hace el montaje (bootstrap_server.sh + install_instance.sh)

- Paquetes: git, python3-venv, build-essential, libxml2/xslt/ldap/sasl/pq/jpeg,
  **nginx**, **certbot** (+ timer), **wkhtmltopdf**, unzip, rsync, dnsutils,
  **aws-cli** (snap), **PostgreSQL** (16 en Ubuntu 24.04), swap si RAM < 4 GB.
- Runtime Odoo 19 **Community**: clone `odoo/odoo` 19.0 + venv (python3 = 3.12 en
  24.04, igual que la Mac) + requirements.
- `odoo.conf` con candado multi-tenant (`dbfilter = ^db$`, `list_db = False`),
  unit systemd, nginx con `/web/database` → 404, logrotate, salud + vecinos.

### Lo que falta para el perfil «PCM self» (gaps reales)

| Qué | Hoy | Necesita PCM | Cómo |
|---|---|---|---|
| pip extra en el venv del runtime | solo requirements de Odoo | `boto3 botocore paramiko cryptography PyGithub` | token `%%EXTRA_PIP%%` en el script o «Ejecutar comando» post-montaje |
| `server_wide_modules` | no figura en el conf (y es READONLY en el editor) | `base,web,queue_job` | token `%%EXTRA_CONF%%` |
| `[queue_job] channels` | — | `root:2` | ídem |
| `limit_time_real` | default 120 s | **14400** (lección bj-uat: jobs largos) | ídem |
| `workers` | 0 (multi-Odoo default) | 2 (en Linux el jobrunner convive bien con workers>0) | ya es token `%%WORKERS%%` |
| `max_cron_threads` | default | 2 | `%%EXTRA_CONF%%` |
| Clave Fernet por entorno | la unit no soporta env vars | `EnvironmentFile=/etc/odoo/<slug>.env` con `PRIMATE_CLOUD_FERNET_KEY` | agregar a la unit del script |
| Repos addons | — | OCA `queue_job` (público) + `primate_cloud_manager` (privado) | como `primate.cloud.repository` de la instancia (flujo existente) |

**Recomendación:** cambio chico en `install_instance.sh` (tokens opcionales
`%%EXTRA_PIP%%`, `%%EXTRA_CONF%%`, `EnvironmentFile` condicional) + checkbox
«Perfil PCM» en el wizard, con goldens actualizados. Alternativa sin código: 4
comandos post-montaje por «Ejecutar comando». **A decidir antes del ensayo.**

### Comparación con `pcm_produccion.conf` de la Mac

Coinciden: `list_db=False`, `dbfilter`, `server_wide_modules=base,web,queue_job`,
`limit_time_real=14400`, `channels=root:2`, `max_cron_threads=2`, master password
solo hash. Cambian: `workers 0→2`, `proxy_mode False→True` (nginx delante, el
montaje ya lo pone), rutas `/opt/pcm/instances/<slug>/...`, logfile propio.

---

## 2. REPO PRIVADO: ya soportado, con un caveat conocido

- `primate.cloud.repository` guarda `github_token_encrypted` (Fernet, enmascarado en
  UI, nunca en claro) y el clone viaja por SSM con el token por **GIT_ASKPASS
  temporal** (mktemp + trap que lo borra pase lo que pase); nunca en la URL, argv ni
  git-config. **Test existente:** `test_addon_clone_script_token_seguro`
  (tests/test_phase6.py) fija exactamente eso.
- **Caveat:** el script SSM lleva el token en **base64 inline** → legible
  (decodificable) en el historial de comandos SSM de la cuenta ~30 días. Mismo
  pendiente conocido del `admin_passwd` de bj-uat (BACKLOG «secretos por comando SSM»).
- **Recomendación:** fine-grained PAT de GitHub, **solo lectura**, limitado al repo
  `primate_cloud_manager`, con vencimiento (90 días) y renovación anotada. Riesgo
  residual bajo: quien lee el historial SSM ya es admin de la cuenta. La deploy key
  SSH es una alternativa válida pero exige reescribir el clone a `ssh://` +
  known_hosts; el camino PAT ya está construido y testeado.
- **Hardening futuro (opcional, mismo backlog):** pasar secretos por S3
  presigned / SSM Parameter Store en lugar de inline (cubriría también `admin_passwd`).

---

## 3. RESTORE SIN NEUTRALIZAR: ya resuelto por diseño + checklist post-restore

- La neutralización corre **solo si la instancia destino NO es `production`**
  (R4-B5: «sobre producción JAMÁS se neutraliza, pida lo que pida»). El PCM self se
  monta con instancia **tipo Production** → el restore de `pcm_produccion` deja
  crons, SMTP y `web.base.url` **intactos**. No hace falta un modo nuevo.
- Estado real de `pcm_produccion` hoy: 26/27 crons activos (8 de PCM), **0 servidores
  SMTP** configurados y 2 mails en `exception` → no hay cola peligrosa que soltar
  (igual se verifica el día D: `select state, count(*) from mail_mail group by state`).
- **Ajustes post-restore** (por «Ejecutar comando» o botón futuro «Checklist PCM self»):
  1. `web.base.url = https://pcm.erp.primateuy.com` + `web.base.url.freeze = True`.
  2. `report.url = http://127.0.0.1:<http_port>` (lección wkhtmltopdf/localhost).
  3. Verificar: crons PCM activos, jobrunner de queue_job vivo (un job de prueba),
     `ir.config_parameter` sin `primate_cloud_manager.fernet_key` (la clave va por
     env var; si apareciera, borrarla), 0 SMTP hasta configurar el real.
  4. Desinstalar Enterprise ya viene hecho de la Mac (punto 0.c).

---

## 4. CREDENCIALES CIFRADAS: opción (b) — clave nueva en el servidor

Coincide con tu preferencia. Procedimiento exacto:

1. **Antes del primer arranque post-restore**: generar clave en el server
   (`.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
   escribirla en `/etc/odoo/pcm-self.env` → `PRIMATE_CLOUD_FERNET_KEY=...`
   (root:odoo, 640), `EnvironmentFile=` en la unit, `daemon-reload`. Así el fallback
   de `_get_encryption_key()` **nunca** genera una clave dentro de la base.
2. Arrancar: los blobs viejos no descifran → `decrypt()` devuelve `False` (no
   revienta): cuentas sin credenciales, token de repo vacío.
3. **Recargás a mano por UI** (vos, Claude no las toca): secret de `pcm-prod-ro` y
   `pcm-prod-prov`, PAT del repo privado. Impersonación: el par es por instancia;
   se regenera al primer uso.
4. **Rotar IAM**: crear access keys NUEVAS para ambos usuarios, cargarlas en el PCM
   nuevo, y **desactivar las viejas** apenas verificado el corte. Esto además hace
   de *fencing*: aunque el PCM de la Mac arrancara por error, sus keys ya no sirven.
5. La clave de la Mac (`~/.pcm_produccion/fernet.key`) se archiva offline hasta
   confirmar el corte; después se destruye.

---

## 5. AUTOGESTIÓN: IMPLEMENTADO (commit local, 14 tests)

- `primate.cloud.instance.is_self` («Hospeda este PCM»): máximo una (constraint),
  marcar/desmarcar solo Cloud Admin (chequeo server-side en `write`), tracking en
  chatter.
- **Bloqueado para TODOS los grupos** (también `group_cloud_destructive`), en los
  **puntos de ejecución** (botón + ORM + job, como la regla de importados):
  - máquina: detener, reiniciar, terminar (`_enqueue_lifecycle` **y** `job_lifecycle`,
    más la entrada al wizard de destrucción). **Iniciar queda permitido** (camino de
    recuperación).
  - instancia: desmontar (acción, encolado y `job_destroy`), `unlink`, reiniciar su
    Odoo (`restart_odoo`), cambiar dominio (`job_create_dns_and_switch` y
    `job_attach_domain`), restaurar encima (`job_restore_backup` sobre destino self),
    ser origen de staging (`job_create_staging`, `job_create_staging_instance`,
    `job_refresh_staging`).
  - **El backup de sí mismo queda PERMITIDO** (lo exige el punto 6).
- Todo intento queda en bitácora (`action_type = self_block`, log durable).
- UI: botones ocultos en `servidor_detalle`, `instancia_detalle` y `entorno_detalle`
  (staging/restore) + aviso visible «hospeda a ESTE PCM».
- Consecuencia asumida: **actualizar el código de PCM en su propio server** (git pull
  + restart) será operación manual por SSM con runbook (el botón de restart está
  bloqueado a propósito); una futura acción controlada «Actualizar PCM» puede venir
  después.
- Tests: `tests/test_autogestion_self.py` (14, todos verdes; suite completa corrida).

---

## 6. SEGURIDAD

- **Ya hay**: 2FA (auth_totp) en admin, `list_db = False`, dbfilter, master password
  solo-hash, nginx 404 en `/web/database`, HTTPS Let's Encrypt + renovación.
  Sumar: `auth_passkey` (ya instalado en la base) como segundo camino fuerte.
- **Allowlist de IP en `/web/login` (propuesta, feature chica):** campo en la
  instancia (una IP/CIDR por línea) + job que escribe
  `/etc/nginx/pcm-login-allow-<slug>.inc` (`allow ...; deny all;`), lo incluye en un
  `location = /web/login` del site, `nginx -t` + reload. Con test golden. **Riesgo a
  decidir:** IP de oficina dinámica = lockeo; el rescate es editar por SSM. Mi
  sugerencia: día 1 sin allowlist (2FA + passkey + list_db=False ya es fuerte) y la
  feature entra después del corte, con calma.
- **Backups diarios de la base de PCM:** la política gestionada existente sirve tal
  cual (cron «ejecutar backups gestionados» + «marcar backups expirados»). Bucket
  `pcm-backups-512060111009` se crea A MANO según RUNBOOK_ONBOARDING (PCM no crea
  buckets, decisión fija) con lifecycle de retención. **Permisos: alcanzan** — la
  política del rol del servidor ya da `s3:PutObject/GetObject` sobre `pcm-*/*` y el
  usuario de PCM tiene `Get/PutLifecycleConfiguration` sobre `pcm-*` (runbook §S3).
  Nota fina: PCM-self se respalda a sí mismo (permitido); solo el RESTORE sobre sí
  está bloqueado.

---

## 7. CORTE: ver runbook abajo (RUNBOOK_MUDANZA_PCM_SELF al final de este doc)

Idea central contra el doble-PCM: el orden es **congelar crons Mac → dump → restore
disparado DESDE la Mac → verificar el nuevo → rotar keys IAM (fencing real) → apagar
la Mac con doble cerrojo**. Aunque la Mac reviviera por error, sus keys desactivadas
la dejan inofensiva.

---

## 8. CONFIGURACIÓN PROPUESTA DEL MONTAJE

| Parámetro | Valor | Nota |
|---|---|---|
| Cuenta / Región | cuenta real · us-east-1 | la misma de bj-uat |
| Tipo | **t3.medium** | default del wizard ✓ |
| Disco | **gp3 50 GB** | subir del default 30 |
| SO | **Ubuntu 24.04** | default; el wizard también ofrece 22.04 y Amazon Linux/Otro (AMI a mano). Recomendado 24.04: python 3.12 + PG 16 |
| IP fija | ✓ Elastic IP | default ✓ |
| Dominio | sslip.io automático al montar → después «Crear DNS ahora» → **pcm.erp.primateuy.com** | encaja en el sufijo default `erp.primateuy.com` del wizard |
| Base | PostgreSQL 16 local, `pcm_produccion` | PG 14.22 (Mac) → 16: dump compatible ✓ |
| Tipo de entorno/instancia | **Production** | clave: así el restore NO neutraliza (punto 3) |
| Odoo | **19 Community** | defaults del modelo ✓; el runtime del montaje ya es Community |
| workers / conf | perfil PCM (punto 1) | `workers=2`, queue_job, `limit_time_real=14400` |

Costo estimado: EC2 t3.medium on-demand ~US$30 + EBS 50 GB gp3 ~US$4 + IPv4 ~US$3,65
≈ **~US$38/mes**.

---

## 9. ENSAYO EN SANDBOX (pendiente de tu OK — no se tocó AWS)

Con `pcm_demo` (cuenta sandbox «Personal Prueba Real»):

1. Montar un servidor igual al propuesto (puede ser t3.small para abaratar) con el
   perfil PCM (pip extra + conf + EnvironmentFile).
2. Repos: `queue_job` público + `primate_cloud_manager` con el PAT read-only real.
3. Restaurar ahí una **copia de `pcm_demo`** (instancia destino Production → verifica
   el restore sin neutralizar) + checklist post-restore.
4. Marcar `is_self` y probar los bloqueos EN VIVO (botones ocultos + jobs cortados +
   bitácora).
5. Ensayo del corte y del rollback (apagado doble cerrojo + fencing de keys sandbox).
6. **Video completo estilo demo POS Backend**, revisado entero; terminar los
   recursos al final (triple confirmación).

---

## Decisiones que te pido antes de seguir

1. **Perfil de montaje**: ¿tokens nuevos en `install_instance.sh` (recomendado, con
   goldens) o comandos post-montaje sin tocar el script?
2. **Allowlist nginx**: ¿día 1 o después del corte? (recomiendo después)
3. **OK al ensayo en sandbox** (punto 9) con video.

---

# RUNBOOK_MUDANZA_PCM_SELF — día del corte

> Regla fija: Claude no ejecuta nada directo contra la cuenta real ni el servidor
> (ni aws cli, ni ssh): todo por PCM o por tus manos. Credenciales las cargás vos.

## F0 — Prerrequisitos (días antes)

- [ ] Ensayo sandbox (punto 9) completado con video revisado.
- [ ] Servidor `pcm-self` montado en la cuenta real (config del punto 8), con perfil
      PCM aplicado y `https://pcm.erp.primateuy.com` activo (sslip → DNS → cert).
- [ ] Repos clonados en la instancia (queue_job + primate_cloud_manager @ 19.0).
- [ ] Bucket `pcm-backups-512060111009` creado con lifecycle (runbook onboarding).
- [ ] Clave Fernet NUEVA instalada (`/etc/odoo/pcm-self.env` + EnvironmentFile, F4.1).
- [ ] Access keys IAM nuevas creadas para `pcm-prod-ro` y `pcm-prod-prov`
      (sin cargar en ningún lado todavía).
- [ ] En la Mac: desinstalados los 13 módulos Enterprise (punto 0.c) con `-u` y
      smoke de pantallas OK. **Backup previo de la base.**

## F1 — Congelar la Mac (ventana de corte abierta)

- [ ] Avisar/cerrar sesiones. No se toca más PCM Mac salvo lo que sigue.
- [ ] Desactivar los 8 crons «Primate Cloud:» en la Mac (quedan registrados cuáles).
- [ ] Vaciar/esperar la cola queue_job (0 jobs pending/started).
- [ ] `select state, count(*) from mail_mail group by state;` → anotar (hoy: 2
      exception, 0 SMTP: nada que soltar).

## F2 — Dump y subida

- [ ] `pg_dump -Fc pcm_produccion` + tar del filestore (~11 MB).
- [ ] `subir_dump.sh` → `s3://pcm-dumps-<cuenta>/incoming/` (imprime el ARN esperado).

## F3 — Restore (disparado desde el PCM de la Mac, que sigue vivo sin crons)

- [ ] PCM Mac → entorno pcm-self → «Restaurar desde un dump…» → key de S3, base
      `pcm_produccion`, instancia destino la de pcm-self (**Production → sin
      neutralizar**), pre-backup no (base recién nacida).
- [ ] Ver el job en verde + bitácora. El script arranca la unit al final.

## F4 — Despertar y verificar el PCM nuevo (checklist en verde ANTES de apagar nada)

- [ ] (hecho en F0) clave Fernet nueva por env var; confirmar que la base NO tiene
      `primate_cloud_manager.fernet_key` en parámetros.
- [ ] Post-restore: `web.base.url = https://pcm.erp.primateuy.com` +
      `web.base.url.freeze`, `report.url` local.
- [ ] Login admin (2FA) por el dominio nuevo.
- [ ] Cargar credenciales: keys IAM **NUEVAS** en pcm-prod-ro / pcm-prod-prov +
      «Validar conexión» (ARN esperado en verde); PAT del repo privado.
- [ ] Marcar `is_self` en la instancia pcm-self → botones destructivos desaparecen;
      probar un stop → bloqueado y en bitácora.
- [ ] Sync AWS manual OK (servidores listados, bj-uat visible).
- [ ] Backup manual de `pcm_produccion` al bucket nuevo OK (objeto en S3).
- [ ] Crons PCM reactivados/activos EN EL NUEVO; un job de prueba procesado
      (jobrunner vivo).

## F5 — Fencing y apagado de la Mac

- [ ] **Desactivar en IAM las access keys viejas** (las que conocía la Mac). Desde acá
      la Mac es inofensiva aunque arranque.
- [ ] Apagar Odoo de la Mac (`pgrep` antes y después — regla no-relanzar-sin-pgrep).
- [ ] Doble cerrojo anti-arranque: `mv levantar.sh levantar.sh.MUDADO-<fecha>` que
      solo imprime «MUDADO a pcm.erp.primateuy.com» y sale; `mv pcm_produccion.conf
      pcm_produccion.conf.MUDADO`. La base local queda como respaldo frío 30 días.
- [ ] Archivar `~/.pcm_produccion/fernet.key` offline (destruir al cerrar los 30 días).

## F6 — Día después

- [ ] Verificar que el backup diario automático llegó al bucket.
- [ ] Verificar renovación/vigencia del cert y `web.base.url` estable.
- [ ] Borrar el dump de `pcm-dumps-*` (como bj-uat).
- [ ] Actualizar KB (`_index.md`, `decisiones.md`) y `docs/CHECKLIST_MUDANZA_PROD.md`.

## Rollback (si F4 no está en verde)

1. NO apagar la Mac (F5 no se ejecutó): reactivar sus 8 crons → PCM Mac sigue siendo
   el productivo. Las keys viejas siguen activas (solo se desactivan en F5).
2. Congelar el server nuevo para diagnóstico: detener su unit **por SSM/consola**
   (el botón de PCM está bloqueado por autogestión, y es correcto que lo esté).
3. El dump en S3 y la base de la Mac quedan intactos: se reintenta otro día.
   Nada destructivo ocurrió antes de F5.
