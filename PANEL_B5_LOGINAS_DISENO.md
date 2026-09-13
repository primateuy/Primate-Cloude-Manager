# Bloque B5 — "Login as…" (impersonación) — Diseño final para revisión

> **FRENO FUERTE. Cero código.** Es la feature más potente y más delicada del módulo entero:
> **abrir una sesión como otro usuario en el Odoo de un cliente, producción incluida.** Se revisa
> con el mismo (o más) cuidado que el restore, el borrado de DNS y el rollback de config. Nada se
> codea hasta aprobar este diseño **decisión por decisión**.

---

## 0. La realidad que manda todo el diseño: cross-origin ⇒ hace falta código remoto

La app PCM corre en el Odoo de **gestión** (un dominio). La instancia del cliente es **otro
servidor, otro dominio**. **Una cookie de sesión solo la puede setear el servidor de ese dominio.**
PCM **no puede** crear la sesión desde su lado. Da igual si generamos la sesión por SSM/odoo shell:
para que el navegador quede logueado en la instancia, **la instancia tiene que emitir la cookie**.

**Conclusión ineludible:** "Login as" necesita un **componente en el Odoo del cliente** que reciba
una autorización de PCM, cree la sesión y setee **su propia** cookie (same-origin) antes de
redirigir a `/web`. No hay forma de hacerlo 100% del lado de PCM. Todo el peso de seguridad recae
en ese componente y en cómo se lo autoriza.

---

## 1. Arquitectura: addon companion `pcm_impersonate` + lado PCM

### En la instancia (código remoto, mínimo y auditado): addon `pcm_impersonate`
- Un addon Odoo chico, desplegado por PCM (vía el clone de B4) e **instalado**, que expone **un
  solo** controlador `/pcm/impersonate` (`auth="none"`).
- Corre DENTRO del Odoo del cliente ⇒ tiene ORM para construir una sesión válida para el `uid`
  destino (incluido el `session_token` que Odoo deriva del usuario, sin tocar la contraseña).
- **Deshabilitado por default**: solo actúa si el sys-param `pcm.impersonate.enabled` está en
  `True` en esa instancia (habilitarlo es un acto deliberado, ver §6). Si está off → `403`.
- Guarda **solo la CLAVE PÚBLICA** de PCM (ver §2). Comprometer la instancia **no** permite forjar
  tokens (la privada vive en PCM).

### En PCM
- Genera el **token firmado** (§2), aplica la fricción de prod (§5), audita en `operation.log`
  (§5), y le da al navegador del admin la URL `https://<dominio-instancia>/pcm/impersonate?token=…`
  para abrir en pestaña nueva.

---

## 2. El token: firma ASIMÉTRICA, corto y de un solo uso

**Asimétrico a propósito** (no HMAC compartido): PCM firma con una **clave privada** (guardada en
PCM, cifrada con la Fernet de la cuenta); la instancia verifica con la **pública**. Así, aunque un
atacante lea todo el disco de la instancia, **no puede forjar un token** (no tiene la privada).

- **Payload**: `{db, uid, login, env_id, iss (cuenta/instancia), exp = now+60s, nonce (uuid4)}`.
- **Firma**: p. ej. Ed25519 (cryptography) sobre el payload canónico.
- **Expiración corta**: 60 s (el redirect es inmediato).
- **Un solo uso**: el controlador guarda los `nonce` consumidos (en un sys-param/tabla con TTL) y
  **rechaza el replay**. Un token usado no vale una segunda vez.
- El controlador valida, EN ORDEN: enabled → firma → no-expirado → nonce-no-usado → db/uid existen
  y el usuario está activo e interno. Cualquier fallo → `403`, sin crear sesión, con log.

---

## 3. Listar `res_users` (multi-BD) — por SSM, sin credenciales

- **Multi-BD**: una instancia puede tener >1 base (el panel ya muestra "Database"). Flujo:
  **elegir BD → listar usuarios de esa BD → elegir usuario**. Si hay varias, el selector de BD es
  obligatorio (no adivinar).
- **Listado por SSM** (mismo patrón que la detección de módulos de Fase 5; read-only, síncrono por
  la política), **no XML-RPC** (XML-RPC exigiría credenciales/apikey; SSM peer las evita):
  `psql -d <db> -tAF'|' -c "SELECT id, login, name FROM res_users
      WHERE active AND NOT share ORDER BY login"`.
- **Filtros**: `active = True` y `NOT share` (solo **internos**; nunca portal/público). Se excluye
  el usuario técnico (`__system__`) y, opcionalmente, se marca `admin` para que impersonarlo sea un
  paso extra-consciente.

---

## 4. Crear la sesión sin tocar contraseñas

- El controlador crea una **sesión nueva en el session store** de Odoo para `(db, uid)`:
  setea `uid`, `login`, `db`, `context` del usuario, y el **`session_token`** que Odoo deriva del
  usuario (para que la sesión sea válida y se invalide sola si al usuario le cambian la contraseña).
- **Nunca** se resetea la contraseña, **nunca** se crea una apikey, **nunca** se lee ni se mueve el
  hash fuera del proceso. La sesión es equivalente a un login normal de ese usuario.
- **Fin de la impersonación**: es una sesión normal → se cierra con Logout. PCM **no** puede matarla
  remotamente (es del Odoo del cliente); se documenta. (Opcional v2: endpoint `/pcm/impersonate/end`.)

---

## 5. Salvaguardas de seguridad (tratado como restore / DNS-delete / rollback)

- **Solo `group_cloud_admin`** dispara Login-as en PCM.
- **Endpoint OFF por default** (`pcm.impersonate.enabled`): habilitarlo es un acto explícito,
  auditado y **revocable** por instancia (§6). Off ⇒ la capacidad no existe.
- **Clave asimétrica** (instancia solo con la pública) + **token 60 s + un solo uso** (anti-replay).
- **Producción = fricción fuerte**: **tipear el nombre exacto del entorno** (validado server-side,
  patrón restore/config) + confirmación explícita. Impersonar en la producción de un cliente es lo
  más sensible del módulo.
- **AUDITORÍA en `operation.log` (inmutable) — el corazón del pedido**: **cada emisión** de token se
  registra con **quién** (usuario PCM), **a quién** (login/uid destino), **qué BD**, **qué entorno**,
  **flag de producción**, y **cuándo**. Se loguea **aunque el redirect no se complete** (alguien
  pidió impersonar = queda trazado). `action_type = "impersonate"`.
- **Traza del lado del cliente también**: el controlador deja un rastro en el Odoo del cliente
  (log/mensaje "sesión creada por impersonación PCM para <admin> como <usuario>") — el cliente ve
  en SU auditoría que no fue un login normal. Respeta al cliente.
- **Hardening de red (recomendado)**: nginx restringe `/pcm/impersonate` a la **IP de egreso de
  PCM** (allow-list), para que el endpoint no sea alcanzable desde toda internet aunque esté enabled.
- **Sin contraseñas** en ningún lado (§4).

---

## 6. Ciclo de vida del endpoint (habilitar/deshabilitar/desplegar)

- **Desplegar**: PCM instala el addon `pcm_impersonate` en la instancia (clone por B4 → `-i` por
  SSM) y escribe la **clave pública** de PCM en un sys-param. Acción de admin, explícita.
- **Habilitar**: setear `pcm.impersonate.enabled = True` (por instancia). Botón "Habilitar Login-as"
  con confirmación; queda en `operation.log`.
- **Deshabilitar / revocar**: `pcm.impersonate.enabled = False` (o desinstalar el addon) → la
  capacidad desaparece al instante. Botón "Deshabilitar Login-as". Es la palanca de kill-switch.
- **Rotación de clave**: si la privada de PCM se compromete, rotar el par y re-desplegar la pública.

---

## 7. IAM / seguridad AWS

- **Sin permisos IAM nuevos**: instalar el addon, escribir el sys-param, listar `res_users` y
  crear la sesión son todo `SendCommand` SSM ya concedido + tráfico HTTP normal a la instancia.
- La **clave privada** de PCM se guarda cifrada con la Fernet de la cuenta (como el resto de
  secretos); nunca sale de PCM. La instancia solo tiene la **pública**.

---

## 8. Flujo completo

```
Panel instancia → "Login as…"
  └─ (una vez) endpoint desplegado + habilitado en la instancia
  └─ elegir BD (si >1) → listar res_users (SSM psql, internos activos) → elegir usuario
  └─ prod? tipear el nombre del entorno (server-side)
  └─ PCM: firma token {db,uid,login,exp+60s,nonce} con la privada
  └─ operation.log: impersonate — quién→quién, BD, entorno, prod, cuándo  (SIEMPRE)
  └─ abre https://<instancia>/pcm/impersonate?token=…  (pestaña nueva)
       Endpoint (addon en el Odoo del cliente):
         enabled? → firma OK (pública)? → no-expirado? → nonce nuevo? → user activo/interno?
         → crea sesión para (db,uid) sin tocar password → set-cookie → redirect /web
         → deja traza en el log del cliente
  └─ el admin queda operando como <usuario> en el Odoo del cliente (logout termina)
```

---

## 9. Contradicciones / riesgos (explícitos, no tapados)

1. **Es, por definición, un backdoor potente en el Odoo del cliente.** Un endpoint que crea sesión
   como cualquier usuario. Mitigaciones: **off por default**, **clave asimétrica** (instancia sin
   la privada), **token 60 s + un solo uso**, **auditoría doble**, **allow-list de IP**, **kill-
   switch**. Aun así, habilitarlo es entregar una capacidad peligrosa: **debe ser un acto
   consciente, auditado y revocable**, no un default.
2. **Sesión completa vs. read-only**: v1 propone **sesión completa** (paridad con CloudPepper:
   reproducir/depurar como el usuario). Es lo más potente y lo más riesgoso. **Decisión del
   usuario** (§10). Un modo read-only sería más seguro pero limita el caso de uso.
3. **PCM no puede terminar la sesión remota** (es del Odoo del cliente); logout la cierra. Endpoint
   de corte remoto = v2.
4. **Shipping de código a cada instancia gestionada** (el addon) + su mantenimiento/actualización.
   Es el precio del cross-origin. Reusa el pipeline de B4 para desplegarlo.
5. **`operation.log` inmutable** es el registro de auditoría — encaja; se suma `action_type`
   `impersonate`. La traza del lado cliente es un plus de transparencia.

---

## 10. Decisiones que necesito de vos (antes de codear)

1. **¿Sesión completa o read-only en v1?** (Recomiendo completa por paridad CloudPepper, con toda
   la fricción/auditoría; pero es la decisión más cargada.)
2. **¿Endpoint como addon companion instalado** (mi propuesta) **o** preferís evaluar una variante
   sin instalar addon? (No hay forma sin código remoto; el addon es la más limpia.)
3. **¿Allow-list de IP en nginx** para `/pcm/impersonate` (hardening extra)? ¿Cuál es la IP de
   egreso de PCM?
4. **¿Impersonar `admin`/usuarios con rol de administración** se permite, se bloquea, o requiere un
   paso extra? (Es el caso más peligroso.)
5. **Firma**: ¿Ed25519 (asimétrica, mi recomendación) te cierra, o preferís otro esquema?

---

## 11. Plan por bloques (para estimar; NO se codea aún)

- **B5.1 — Addon companion `pcm_impersonate`** (remoto): controlador `/pcm/impersonate` con toda la
  validación (enabled/firma/exp/nonce/user), creación de sesión sin password, traza cliente, sys-
  params (pública + enabled + nonces usados). **El más delicado.**
- **B5.2 — Lado PCM**: par de claves por cuenta (privada cifrada), firma del token, `list_db_users`
  (SSM), `action_login_as` (admin-only, prod type-name, audita, devuelve la URL), deploy/enable/
  disable del endpoint. `action_type impersonate`.
- **B5.3 — UI**: en el panel, "Login as…" (selector BD → usuario), confirmación prod, apertura en
  pestaña nueva; botones Habilitar/Deshabilitar Login-as.
- **B5.4 — Tests**: firma/verificación (válida, expirada, nonce-reuso, firma-mala → rechazo), off-
  by-default 403, list_db_users filtra internos/activos, prod exige nombre, auditoría se escribe
  siempre. **E2E en vivo** (t3.micro descartable) del camino completo, como B3. **Peso: ALTO.**

**FRENO FUERTE. Espero tu revisión decisión-por-decisión (§10) para arrancar B5.** Es la última
feature del panel y la más delicada — mejor un ida y vuelta de diseño ahora que un backdoor mal
pensado después.

## Reglas permanentes halladas en el E2E real (2026-07-06)

1. **`odoo.registry` no existe en Odoo 19** — para abrir un cursor/env sobre una BD arbitraria en
   un controlador: `from odoo.modules.registry import Registry; Registry(db).cursor()`. (El
   controlador 403eaba todo hasta detectarlo en vivo.)
2. **Un flag de seguridad tocado desde OTRO proceso debe leerse FRESCO, no cacheado.** `get_param`
   está ormcacheado; PCM setea `enabled`/`epoch` por SSM (proceso aparte) y la cache del server
   corriendo no se invalida → el kill-switch no cortaba y tokens nuevos pasaban tras deshabilitar.
   Fix: el companion lee esos params con `search` sobre `ir.config_parameter`, no `get_param`.
   Regla general (hermana de "estado terminal no basta"): **si un control de seguridad puede
   cambiar desde afuera del proceso, leelo fresco en cada chequeo.**
