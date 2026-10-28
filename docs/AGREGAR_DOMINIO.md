# Agregar el dominio definitivo a una instancia que está en sslip.io

Decisión 2026-10-27: todo montaje sin dominio (bj-uat incluido) queda
funcionando con **HTTPS por sslip.io** (`<slug>.<ip-con-guiones>.sslip.io`) y
**sin registro DNS propio**. El dominio se suma después, por uno de dos
caminos. Los dos son **no destructivos**: el dominio nuevo se monta en
paralelo y recién con el certificado nuevo verificado se retira sslip.io.
Si algo falla, la instancia sigue exactamente como estaba (sslip.io con
HTTPS) y la ficha dice qué pasó.

Ninguno reinstala Odoo ni toca la base, salvo `web.base.url`.

## Antes de empezar (los dos caminos)

1. La instancia tiene **IP fija** (Elastic IP). Todo montaje nuevo la
   reserva por defecto; la ves en la ficha del servidor ("IP del servidor") y
   en la consola de EC2 → Elastic IPs (con la etiqueta
   `primate:managed_by = pcm`). Sin IP fija, un stop/start cambia la IP y el
   registro A queda apuntando a otro lado.
2. Anotá la IP fija: es el valor del registro A.
3. Si el dominio tiene un registro **AAAA** (IPv6) que no apunta a esta
   máquina, borralo: Let's Encrypt validaría contra esa IPv6 y fallaría.

## Camino 1 — por PCM: «Crear DNS ahora» (el dominio está en Route 53)

Requiere que el stack `pcm-prerequisitos` tenga los parámetros **ZonaDns**
(Hosted Zone ID) y **NombresDns** (ej. `*.erp.cliente.com`). Vacíos, PCM no
puede escribir en Route 53 y este botón falla con AccessDenied: usá el
camino 2.

En PCM:

1. Entornos → el entorno → la instancia (ficha de la instancia).
2. **Crear DNS ahora**.
3. Completá **Dominio nuevo** (ej. `erp.cliente.com`), **Hosted Zone ID** y
   **TTL** (300). Confirmá con **Crear DNS y cambiar**.

   Con los parámetros `pcm.route53_default_zone_id` (la zona) y
   `pcm.route53_default_domain_suffix` (el subdominio del permiso, p. ej.
   `erp.primateuy.com`), el formulario viene con la zona cargada y propone
   `<slug>.<subdominio>`; un nombre fuera de ese subdominio se rechaza antes de
   tocar nada (el permiso de Route 53 no lo alcanzaría). Caso real: bj-uat →
   `barracajuliav19.erp.primateuy.com` (28-oct, 70 s).

PCM hace, en un job:

| Paso | Qué hace | Si falla |
|---|---|---|
| 1 | Crea (UPSERT) el registro A `dominio → IP fija` en la zona | No se tocó nada; el motivo queda en la ficha |
| 2 | Espera INSYNC de Route 53 | — |
| 3 | Consulta el A contra los NS de la zona (hasta 5 minutos) | No se tocó el servidor |
| 4 | Monta un vhost de nginx en paralelo para el dominio nuevo | Se borra el vhost nuevo |
| 5 | Pide el certificado (certbot) y hace un GET https real | Se borran el vhost nuevo y su certificado; sigue sslip.io |
| 6 | El vhost nuevo pasa a ser el de la instancia; se borra el de sslip.io y su certificado | Se restaura el vhost anterior |
| 7 | `web.base.url` → `https://dominio`; la URL principal y los links de PCM pasan al dominio nuevo | — |

Qué verificás:

- En la ficha: el link de la cabecera muestra el candado y el dominio nuevo.
- `https://dominio/web/login` abre con certificado válido; `http://` redirige.
- Bitácora: `Crear DNS` y `Cambiar dominio (DNS + HTTPS)` en éxito, y
  `Borrar certificado: <sslip>`.
- Registros DNS: el registro A nuevo, atado a la instancia.

## Camino 2 — a mano: «Asociar dominio existente»

Sirve para Route 53 cargado a mano y para cualquier DNS fuera de AWS. PCM
**no toca el DNS**: solo verifica que el dominio apunte a la IP fija.

### En la consola de AWS (si el dominio está en Route 53)

1. Route 53 → Hosted zones → la zona del dominio.
2. **Create record**: nombre (ej. `erp`), tipo **A**, valor = **la IP fija**,
   TTL 300, routing simple. **Create records**.
3. Esperá a que el cambio pase a INSYNC (un minuto, normalmente).
4. Comprobá desde tu máquina: `dig +short erp.cliente.com` tiene que
   devolver la IP fija.

(Con otro proveedor de DNS: lo mismo, un registro A a la IP fija.)

### En PCM

1. Ficha de la instancia → **Asociar dominio existente**.
2. **Dominio nuevo** → **Verificar y asociar**.
   - Si el dominio **no resuelve** o **apunta a otra IP**, PCM lo dice en el
     momento ("apunta a 5.6.7.8" / "todavía no resuelve") y **no cambia
     nada**. Corregí el registro y volvé a intentar.
3. Si resuelve a la IP fija, PCM sigue desde el paso 4 de la tabla de
   arriba (vhost en paralelo → certificado → retiro de sslip.io →
   `web.base.url`). En el job vuelve a verificar la resolución desde el
   servidor (resolver del sistema, 1.1.1.1 y 8.8.8.8).

Qué verificás: lo mismo que en el camino 1 (salvo el registro en Registros
DNS, que PCM no crea en este camino).

## Si algo falla

- La ficha muestra **"No se pudo pasar a <dominio>"** con el motivo y dos
  botones: **Reintentar** (no vuelve a crear el registro) y **Descartar**.
- La instancia sigue en sslip.io con HTTPS: nada para restaurar.
- Rate limits de Let's Encrypt: cada intento fallido consume uno. Antes de
  reintentar, confirmá con `dig` que el dominio resuelve a la IP fija.

## Pendiente: bj-uat

bj-uat se monta en sslip.io (con IP fija). Cuando el cliente confirme el
dominio: camino 1 si el dominio queda en la zona de Route 53 de la cuenta
(con ZonaDns/NombresDns cargados en el stack), camino 2 si no.
