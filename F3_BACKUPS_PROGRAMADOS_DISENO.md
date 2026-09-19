# F3 — Backups programados con catálogo (DISEÑO APROBADO, POSPUESTO)

> **Estado: pospuesto (2026-09-19).** Diseño aprobado en sep-2026 y **no implementado**.
> Motivo de la postergación: **espacio en disco del servidor, sobre todo en producción**, y
> no hace falta para el ensayo de Barraca Julia.
> **Requerido antes de pasar un cliente a producción**, junto con el HTTPS del manager
> (ver el gate en `BACKLOG.md`).
> Se guarda completo para retomarlo **sin rediseñar**.

## Ojo: el catálogo NO es parte de este feature — ya existe

`primate.cloud.backup` (catálogo), `primate.cloud.backup.policy`, `job_run_backup`,
`_build_backup_script`, los tres crons y el botón "Respaldar ahora"
(`views/primate_cloud_environment_views.xml:142`) **están implementados desde la Fase 8**.
El wizard de restauración se construyó sobre el catálogo (`backup_id` es `required=True`) y
**los tres llamadores del staging le pasan `backup_id`**. Posponer F3 NO elimina el catálogo
ni el origen "copia del catálogo": sólo posterga lo que falta, listado abajo.

## Lo que falta (esto es F3)

1. **Formato zip de Odoo** (`dump.sql` + `filestore/`).
2. **Retención por N copias** (hoy sólo por días, vía lifecycle de S3).
3. **Flag de verificación** de cada copia.
4. **Que el ejecutor honre la frecuencia** (hoy sólo mira `execution_hour`).
5. **Catálogo visible en la ficha** de base y de instancia.
6. **Cuenta e instancia en la key** de S3.

## 🔴 La disyuntiva central: el zip rompe el streaming

Hoy el dump **nunca toca el disco**: `pg_dump -Fc | aws s3 cp -`. Decisión consciente de la
Fase 8 — evita llenar el disco de producción y el multipart se aborta si el pipe falla.

Un zip de Odoo es **un archivo con dos miembros**, y no se puede streamear desde dos
productores: obliga a bajar a disco, y verificarlo con `unzip -t` exige el zip completo en
disco. Costo **≈3×** (dump + copia del filestore + zip). El SQL plano además es más grande y
restaura más lento que `-Fc`. **Esta es la razón por la que F3 se pospuso.**

| Opción | Pros | Contras |
|---|---|---|
| (a) Seguir con `-Fc` + tar | Cero riesgo de disco | El backup no lo restaura un Odoo cualquiera |
| (b) Zip siempre | Portable | Pierde el streaming en TODAS las bases |
| **(c) `archive_format` por política** ← recomendada | Default = comportamiento actual; quien quiere portabilidad opta y paga el disco | Dos caminos que mantener |

## Cambios concretos

**`backup.policy`**: `archive_format` (`pcm_stream` default / `odoo_zip`), `retention_mode`
(`days` / `copies`), `retention_copies`.

**`backup`**: `archive_format`, `verified`, `verified_at`, `verify_detail`.
⚠️ **Hay que sumarlos a `UPDATABLE_FIELDS`** (`models/primate_cloud_backup.py:29`) o el
`write` los rechaza: el modelo es inmutable salvo esa allowlist.

**Rama zip del script** (`--no-owner --no-privileges` para que el SQL plano entre bajo otro
`pg_user`, porque el restore hace `createdb -O`):

```sh
WORK=/var/tmp/pcm/backup/$$; mkdir -p "$WORK/filestore"
# guarda previa: 3x (dump + filestore + zip) contra df -Pm /var/tmp
sudo -u postgres pg_dump --no-owner --no-privileges -d <db> -f "$WORK/dump.sql"
[ -d "$FS_DIR" ] && cp -a "$FS_DIR/." "$WORK/filestore/"
( cd "$WORK" && zip -q -r bk.zip dump.sql filestore )
unzip -t "$WORK/bk.zip" >/dev/null && echo "PCM_ZIP_OK"
aws s3 cp "$WORK/bk.zip" s3://<bkt>/<key>.zip --only-show-errors
# y limpieza garantizada del WORK en trap EXIT
```

**Verificación mínima**: `pcm_stream` → `PCM_DUMP_SIZE_BYTES > 0`;
`odoo_zip` → `PCM_ZIP_OK` + contiene `dump.sql` + tamaño > 0.

**Retención por N copias**: el lifecycle de S3 no puede hacerlo. Nuevo
`_cron_enforce_retention()`: por (entorno, base) deja las N más recientes; para el resto borra
**las keys exactas del catálogo** (no por listado, así que no aplica el hallazgo del barrido de
una sola pasada) y marca el registro `expired`.
**Nunca borra el registro**: el modelo prohíbe `unlink` y el admin no lo tiene en la ACL.
**IAM nuevo: NINGUNO** — `s3:DeleteObject` sobre `pcm-*/*` y `s3:ListBucket` sobre `pcm-*` ya
están en `pcm-operator`.

**Frecuencia y alcance**: resolver la política por cadena **base → instancia → entorno**
(nuevo `database.backup_policy_id`) y honrar la frecuencia comparando contra el último backup
completado, manteniendo el tope de 1 intento/hora.

**Catálogo en la ficha**: `backup_ids` en `database` y en `instance`, con fecha, tamaño, key,
estado, verificada y propósito, reusando `action_restore`.

**Key nueva**: `<prefijo>/<cuenta>/<proyecto|sin-clasificar>/<entorno>/<instancia>/<base>/<ts>`.
⚠️ Las reglas de lifecycle vigentes apuntan al prefijo viejo (hay que asegurar la regla en el
nuevo), y **hay un defecto latente hoy**: `_run_database_backup` usa `self.project_id.name`,
que es **nullable**, así que un servidor sin proyecto genera keys bajo `"sin-nombre"` — el caso
de los servidores importados por F2.

**Bitácora**: `backup_run` ya existe; hay que agregar **`backup_purge`** a `ACTION_TYPES`
(es una lista cerrada, `models/primate_cloud_operation_log.py:11`).

## Decisiones que quedaron abiertas al posponer

1. ¿`archive_format` por política (c) o zip siempre (b)?
2. ¿Se cambia el layout de la key? Toca las reglas de lifecycle vigentes.
3. ¿Política por base (campo nuevo) o alcanza instancia → entorno?
4. ¿"Respaldar ahora" también por base y por instancia? (a nivel entorno ya existe)

## Estimación

**3–4 días.** Riesgo medio: el zip toca disco en producción y el cambio de key interactúa con
el lifecycle existente.
