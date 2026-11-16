# PCM de EMERGENCIA en la Mac (plan de contingencia)

Desde la mudanza (2026-11-16), **el PCM productivo es
`https://primatecloudmanager.com`** (servidor `pcm`, cuenta 512060111009).
La Mac conserva una copia COMPLETA pero **desactualizada desde ese día**:
base `pcm_produccion`, filestore, `fernet.key` local y el script de
arranque renombrado a `levantar_emergencia.sh` (con aviso y confirmación).
Las **keys IAM viejas** quedaron **desactivadas (nunca borradas)** en IAM:
son la llave de esta contingencia.

> La emergencia es para cuando el PCM nuevo NO responde y hay que operar
> la infraestructura YA (p. ej. levantar/apagar servidores de clientes).
> No es para consultas: para mirar datos alcanza esperar a que el nuevo
> vuelva.

## 1 · Confirmar que el nuevo está realmente caído

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 https://primatecloudmanager.com/web/login
```

- `200` → NO hay emergencia: el PCM nuevo responde.
- `000`/`5xx` sostenido → seguí. Antes de asumir lo peor, mirá la consola
  AWS (EC2 → instancia `pcm`): si la máquina está `running`, puede ser
  solo el servicio Odoo — un reinicio del servicio o de la instancia
  desde la consola suele alcanzar **sin activar la emergencia**.

## 2 · Reactivar las keys viejas (consola IAM)

IAM → Users → `pcm-prod-prov` y `pcm-prod-ro` → Security credentials →
la access key **desactivada** de cada uno → **Activate**.

Con eso la Mac recupera su acceso a AWS (sus keys cifradas locales siguen
en la base, con la `fernet.key` local intacta).

## 3 · Traer el estado actual (la base local está VIEJA)

Todo lo que pasó después de la mudanza vive en los backups diarios del
nuevo. Restaurá el último sobre la base local ANTES de operar:

```bash
# Ver el último backup (perfil con las keys ya reactivadas):
aws s3 ls s3://pcm-backups-512060111009/ --recursive --profile pcm-prod-provision | sort | tail -5

# Bajarlo y restaurarlo SOBRE la base local (pisa pcm_produccion de la Mac):
aws s3 cp s3://pcm-backups-512060111009/<KEY_DEL_ULTIMO>.zip /tmp/pcm_emergencia.zip --profile pcm-prod-provision
cd /tmp && rm -rf pcm_emergencia && unzip -q pcm_emergencia.zip -d pcm_emergencia
dropdb -U odoo pcm_produccion && createdb -U odoo pcm_produccion
psql -U odoo -d pcm_produccion -q -f pcm_emergencia/dump.sql
rm -rf ~/Odoo/clients/19.0/pcm_produccion/data/filestore/pcm_produccion
cp -R pcm_emergencia/filestore ~/Odoo/clients/19.0/pcm_produccion/data/filestore/pcm_produccion
```

Si el backup del día no existe o S3 no responde, la base local vieja
sirve igual para operar servidores (el inventario cambia poco); solo
tenelo presente al mirar bitácoras/backups recientes.

## 4 · Levantar y ajustar

```bash
~/Odoo/clients/19.0/pcm_produccion/levantar_emergencia.sh   # pide confirmación «SI»
```

En `http://127.0.0.1:19190`, después de loguear:

- **Ajustes → Parámetros del sistema**: `web.base.url` →
  `http://127.0.0.1:19190` (el restore del backup del nuevo lo deja
  apuntando al dominio).
- Si la emergencia va para largo y necesitás crons/jobs: revisá que los
  crons de PCM estén activos (el backup del nuevo los trae activos).
- La instancia `pcm` del entorno `pcm-self` va a figurar con `is_self`:
  **dejalo así** (la Mac no hospeda a ese PCM, pero la guarda solo
  bloquea destruirlo — que es exactamente lo que no hay que hacer).

## 5 · Volver al nuevo cuando se recupere

1. Apagá el Odoo de la Mac (Ctrl+C en la terminal de
   `levantar_emergencia.sh`).
2. Consola IAM: **Deactivate** de nuevo las keys viejas (los dos users).
3. Verificá el nuevo: `curl` del paso 1 → `200`, login y una mirada a la
   bitácora.
4. Si operaste cambios DESDE la Mac durante la emergencia (montajes,
   restores, DNS), el nuevo no los tiene en su base: corré
   «Sincronizar servidores desde AWS» en el nuevo y revisá a mano lo que
   no sea inventario (bitácora, backups). La base de la Mac vuelve a
   quedar como copia vieja — no hace falta tocarla.

---
*Creado el día de la mudanza (2026-11-16). Si rotás el bucket de backups,
el dominio o los usuarios IAM, actualizá esta guía.*
