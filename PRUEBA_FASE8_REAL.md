# Prueba real controlada — Fase 8 (respaldos + staging)

> Estado: **PAUSADA en el paso 1, bloqueada por una acción de admin AWS** (crear el bucket).
> Infra en pausa segura: EC2 `stopped` (no factura cómputo), lista para retomar sin
> re-aprovisionar. Fecha: 2026-07-02.

## Evidencia hasta ahora

| Ítem | Valor / Resultado |
|---|---|
| Barrido inicial EC2 (us-east-1/2) | Sin instancias (la de la prueba anterior, terminada) ✔ |
| Aprovisionamiento entorno demo (id 5) | `success` — 2º intento (ver fricción F1) |
| EC2 nueva | `i-0cac04f905afc3310`, t3.micro, us-east-2, IP 3.137.148.236 |
| Instalación Odoo vía SSM | `Success` (~4 min) |
| HTTP | **200** en `http://3.137.148.236/web/login` ✔ |
| Política de respaldo | "Fase 8 prueba real" (daily/7, gestionada, bucket `pcm-fase8-test-603011031378`) asignada al entorno |
| AWS CLI en la instancia | instalado vía `snap install aws-cli --classic` (v2.35.14) — ver fricción F2 |
| PutObject del instance profile | probado: responde `NoSuchBucket` (NO AccessDenied) ⇒ el rol puede subir; solo falta el bucket |
| Estado de pausa | EC2 `stopped` verificado en AWS ✔ |

## BLOQUEO: crear el bucket S3 (acción de Daryl)

Ninguna identidad de PCM puede crear buckets (`s3:CreateBucket` denegado tanto para el
user `pcm-operator` como para el rol `pcm-ssm-role`). Es la única pieza que falta:
el rol ya puede subir objetos y el user ya puede verificar (`head_object`/`head_bucket`).

**Opción A (recomendada, 1 min):** crear a mano el bucket
`pcm-fase8-test-603011031378` en **us-east-2** (consola S3 → Create bucket, defaults).

**Opción B:** agregar `s3:CreateBucket` + `s3:PutLifecycleConfiguration` a la política
del user `pcm-operator` (alcance sugerido: `arn:aws:s3:::pcm-*`).

Al estar el bucket: retomo con start de la EC2 y sigo los pasos 1-5 del plan.

## Fricciones para el backlog (las dos primeras YA corregidas en código)

- **F1 (corregido en el driver de la prueba):** la config guardada del wizard es CRUDA
  (`security_group_ids` como string); reutilizarla sin pasar por `_prepare_params` rompe
  `run_instances`. Anotar: si algún día un flujo re-usa la config guardada fuera del
  wizard, necesita la misma transformación.
- **F2 (FIX commiteado `25dac1b`):** la AMI Ubuntu 24.04 NO trae AWS CLI y `awscli` (apt)
  no tiene candidato ⇒ TODO el pipeline de backups moría en el primer `aws s3 cp`.
  `install_odoo.sh` ahora instala `aws-cli` vía snap, y los scripts de backup/restore
  tienen guard con error accionable.
- **F3 (FIX commiteado `185dad0`):** `ensure_bucket` usaba `list_buckets`
  (`s3:ListAllMyBuckets`, denegado en cuentas mínimas) ⇒ ahora `head_bucket` + errores
  accionables (bucket ajeno / sin permiso de creación).
- **F4 (pendiente, doc):** la política IAM mínima documentada en `PRUEBA_AWS_REAL.md` no
  tiene NINGÚN permiso S3 para el user; la Fase 8 necesita al menos:
  `s3:GetObject`/`HeadObject`, `s3:ListBucket`, `s3:PutLifecycleConfiguration`,
  `s3:DeleteObject` (limpieza) sobre `arn:aws:s3:::pcm-*` — y decidir quién crea buckets.
  El instance profile necesita `s3:PutObject`/`GetObject` sobre el mismo alcance (ya los
  tiene en esta cuenta).

## 2º BLOQUEO (2026-07-02): política IAM del user sin bloque S3

Con el bucket ya creado, `pcm-operator` no puede hacer NINGUNA operación S3 (head_bucket
403, list_objects/put_lifecycle AccessDenied, head_object 403): la política del user no
tiene bloque S3 y la Fase 8 lo requiere por diseño (verificación `head_object` del
restore, lifecycle de retención, limpieza). Fricción F4 confirmada como bloqueante.
EC2 devuelta a `stopped` (pausa segura). FIX adicional commiteado (`15743a7`): el 403 de
head_bucket es ambiguo (bucket ajeno o falta s3:ListBucket) y el mensaje ahora lo dice.

**Acción de Daryl:** agregar este statement a la política del user `pcm-operator`:

```json
{
  "Sid": "PcmS3Backups",
  "Effect": "Allow",
  "Action": [
    "s3:ListBucket",
    "s3:GetObject",
    "s3:DeleteObject",
    "s3:GetLifecycleConfiguration",
    "s3:PutLifecycleConfiguration"
  ],
  "Resource": [
    "arn:aws:s3:::pcm-*",
    "arn:aws:s3:::pcm-*/*"
  ]
}
```

(`s3:GetLifecycleConfiguration` es necesario porque `put_lifecycle_rule` lee la config
existente para mergear sin pisar reglas ajenas.)

## 3er hallazgo (2026-07-02): scope de escritura del instance profile

Con user y bash resueltos, el backup avanzó hasta el `aws s3 cp` y falló: el rol
`pcm-ssm-role` (instance profile) NO puede `PutObject` en `pcm-fase8-test-603011031378`.
Mapeo del scope del rol (vía SSM, AccessDenied vs NoSuchBucket):

| Bucket | PutObject del rol |
|---|---|
| `pcm-fase8-test-603011031378` | **AccessDenied** (fuera de scope) |
| `pcm-backups-603011031378` | NoSuchBucket (permitido, falta crear) |
| `pcm-staging-603011031378` | NoSuchBucket (permitido) |
| `pcm-603011031378` | NoSuchBucket (permitido) |

El rol está scopeado a nombres específicos, NO a `pcm-*`. El user (`pcm-operator`) sí quedó
en `pcm-*` (lee/lista/lifecycle OK sobre pcm-fase8-test).

**Acción de Daryl (mínima, sin tocar IAM):** crear el bucket **`pcm-backups-603011031378`**
en us-east-2 (nombre que el rol YA puede escribir y el user YA puede leer). Yo repunto la
política a ese bucket. El `pcm-fase8-test-603011031378` queda vacío para borrar.
*(Alternativa: ampliar el rol `pcm-ssm-role` con `s3:PutObject`/`GetObject` sobre
`arn:aws:s3:::pcm-*/*` — pero la opción del bucket no otorga permisos nuevos.)*

### FIX de código en el camino (commiteado)
- `162cbb5`: los scripts de backup/restore ahora corren bajo **bash** (heredoc). SSM ejecuta
  con `/bin/sh` = dash, que no soporta `set -o pipefail`; sin él, un `pg_dump` fallido en el
  pipe subiría un dump truncado. Verificado en la instancia real.

## 4to hallazgo / corrección (2026-07-02): el rol NO tiene escritura S3

**Corrección de un error mío:** el mapeo anterior (bucket rename) fue un MALENTENDIDO de la
semántica de S3. `NoSuchBucket` en un PutObject a un bucket inexistente NO prueba permiso de
escritura — S3 devuelve el error de existencia antes que el de auth. Con el bucket
`pcm-backups-603011031378` YA creado, el rol da `AccessDenied` igual que antes.

Realidad: el instance profile **`pcm-ssm-role` no tiene NINGÚN `s3:PutObject`** (se creó para
provisioning/SSM, no para backups). No hay truco de nombre de bucket que lo evite.

**Acción de Daryl (última IAM, al rol `pcm-ssm-role`):**

```json
{
  "Sid": "PcmS3BackupObjects",
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject"],
  "Resource": "arn:aws:s3:::pcm-*/*"
}
```

(`PutObject` para subir dump+filestore; `GetObject` para el marcador `head-object` del backup
y para bajar el dump en el restore.) El bucket `pcm-backups-603011031378` ya es el destino de
la política. Con esto la cadena queda completa: user lee/lista/lifecycle/borra (`pcm-*`), rol
escribe/lee objetos (`pcm-*/*`).

## Evidencia de la corrida completa (2026-07-02, tras las 3 IAM + fixes)

| Paso | Resultado |
|---|---|
| Start EC2 `i-0cac04f905afc3310` | running, IP 18.218.218.89 (IP cambia en cada start), HTTP **200** |
| **Paso 1 — Backup real** | registro id 12 `completed`, purpose `manual`, dump 1.18 MB (1188462 B) + filestore 63.7 KB en S3 (`head_object` OK) ✔ |
| Validador tras backup | **Cumple** (tras limpiar BD huérfana — ver fricción abajo) ✔ |

**Fricción F5 (backlog):** re-provision del entorno duplicó el registro `primate.cloud.database`
(id 6→instancia terminada, id 7→viva); el registro viejo sin backups dejaba el entorno en "No
cumple". Limpieza del huérfano → "Cumple". Detalle en BACKLOG.md.

## RESULTADO FINAL — prueba completa, 5/5 pasos ✔ (2026-07-02)

| # | Paso | Evidencia |
|---|---|---|
| 0 | Provisión origen | EC2 `i-0cac04f905afc3310` t3.micro us-east-2, install SSM Success, HTTP 200 |
| 1 | **Backup real** | registro 12 `completed` purpose `manual`; S3: dump 1.18 MB (1188462 B) + filestore 63.7 KB (63752 B); `head_object` OK |
| 1b | **Validador** | → **Cumple** (tras limpiar BD huérfana, fricción F5) |
| 3 | **Staging desde instancia** | env 17, EC2 `i-0d7e0d8130b2ae95b` (única extra); wizard preseleccionó origen instancia 8/BD 7; fuente id 13 purpose **`staging`**; HTTP 200 |
| 3b | Verif. staging | neutralización: mail=0, crons=0, apikeys=0, `web.base.url`=staging; datos: 4 usuarios; **filestore íntegro `ATTACH_MISSING=0`** (12 adjuntos) |
| 2+4 | **Restore sobre staging + pre-backup** | pre-backup id 14 `pre_restore` en S3 (1.18 MB) fechado ANTES del restore; restore `success`; bitácora "Restaurar Backup … → Fase8 Staging"; HTTP 200; re-neutralizado; `ATTACH_MISSING=0` |
| 5 | **Limpieza total** | ambas EC2 `terminated`; bucket `pcm-backups-603011031378` **vacío** (6 objetos borrados); lifecycle de prueba eliminado; barrido final: 0 EC2 facturando, 0 objetos S3 |

**Guardarraíles respetados:** máx. 1 EC2 extra (el staging); nada contra entornos de clientes;
al primer fallo se cortó facturación antes de investigar; sin reinicios con jobs corriendo.

### Prerequisitos IAM confirmados (para la doc de la política mínima)
- **User `pcm-operator`** (statement `PcmS3Backups`): `s3:ListBucket`, `s3:GetObject`,
  `s3:DeleteObject`, `s3:GetLifecycleConfiguration`, `s3:PutLifecycleConfiguration` sobre
  `arn:aws:s3:::pcm-*` y `pcm-*/*`.
- **Rol `pcm-ssm-role`** (statement `PcmS3BackupObjects`): `s3:PutObject`, `s3:GetObject`
  sobre `arn:aws:s3:::pcm-*/*`.
- **PCM no crea buckets** (decisión fija): el bucket de la política lo crea un admin; su
  nombre debe caer en el scope de escritura del rol (`pcm-backups-603011031378` ✔).

### Fricciones para el backlog (código ya corregido salvo F5/F6)
- **F1** (driver de prueba): config guardada del wizard es cruda (`security_group_ids`
  string→lista). → BACKLOG.
- **F2** (FIX `25dac1b`): AMI Ubuntu 24 sin aws-cli → `install_odoo.sh` lo instala por snap.
- **F3** (FIX `185dad0`): `ensure_bucket` usaba `list_buckets` → ahora `head_bucket`.
- **FIX `162cbb5`**: scripts de backup/restore bajo bash (SSM usa dash, sin pipefail).
- **FIX `15743a7`**: mensaje del 403 de head_bucket desambiguado.
- **F5** (BACKLOG): re-provision duplica el registro `primate.cloud.database`; el viejo
  (instancia terminada, sin backups) deja el entorno en "No cumple".
- **F6** (nuevo, doc): el barrido de limpieza de una sola pasada dejó 4 objetos S3 (posible
  consistencia list-after-write); un vaciado robusto debe iterar hasta que el listado dé
  vacío. Solo afecta scripts de limpieza; el módulo usa lifecycle de S3 para retención, no
  borrado masivo app-side.



---

# Prueba real controlada — Fase 8.5 (DNS CRUD) — completa ✔ (2026-07-03)

Zona de test **`pcm-test.internal`** (`Z0724138235G01YP4KPO8`), NO delegada (.internal):
toda verificación por **API de Route 53** (`list_records`/`get_change`), nunca por dig
(no resolvería, y es lo esperado). Registro `test.pcm-test.internal` (A).

| Paso | Resultado (verificado por API) |
|---|---|
| 1. Crear desde PCM | `sync=synced`, ChangeId `/change/C07554193QS2P3NKRZSCM`, presente en la zona con `203.0.113.10` |
| 2. Modificar desde PCM | `sync=synced`, valor nuevo `203.0.113.20` confirmado en la zona |
| 3. **Divergencia forzada por fuera** | cambio directo por boto3 a `198.51.100.99` (simula consola) → sync de PCM → **`sync=divergent`**, `record_value_aws=198.51.100.99` (real), PCM conserva `203.0.113.20` sin pisar |
| 4. Resolver reaplicando desde PCM | `sync=synced`, la zona vuelve a `203.0.113.20`, `record_value_aws` limpio |
| 5. Borrar por el job | `state=deleted`, `sync=synced`, **ausente en la zona** (API=None) |
| 6. Limpieza + barrido | sin residuos; la zona queda solo con **NS/SOA** |

**El punto que moto no cubre (divergencia real) quedó validado:** con un cambio hecho FUERA
de PCM, el sync lo detecta como divergente mostrando el valor real de AWS, y reaplicar desde
PCM lo resuelve. La propagación real (`get_change` PENDING→INSYNC) también se ejercitó (moto
siempre da INSYNC).

**Guardarraíles:** nada que facture por hora (Route 53 solo cobra la zona ~USD 0.50/mes, que
Daryl borra); la zona quedó limpia. **Fricción para el backlog:** al agregar el campo
`is_alias` (fix del alias) NO se re-actualizó `pcm_demo`, y la primera corrida falló con
`column is_alias does not exist` — recordatorio operativo: re-`-u` de las bases vivas tras
agregar campos, no solo correr la suite en `pcm_test`.
