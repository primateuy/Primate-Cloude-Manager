# Videos de ensayo de PCM (fuera de git)

## `demo_monitoreo_cloudwatch.mp4` — 3:36, 1080p

Demo real del **Monitoreo CloudWatch** (pedido INCO 2026-11-12) en la sandbox
(`pcm_demo`, cuenta «Personal Prueba Real», us-east-1, t3.small efímero,
Ubuntu 24.04). Config de INCO **embebida** (archivo local, sin Parameter
Store). Driver `grabar_demo_cw.py`, toma 9 con **0 incidentes** (las tomas
1–8 cazaron bugs del driver y un hallazgo real: el stack `-cfn` de la sandbox
actualiza `pcm-ssm-role-cfn` pero los montajes usan `pcm-ssm-role` → la
policy se adjuntó a mano; en la cuenta real el Sufijo vacío pega en el rol
correcto).

| Minuto | Tramo |
|---|---|
| 0:00 | Portada |
| 0:12 | Montaje nuevo con la opción **activada por default** en el wizard (espera ×16) |
| 1:26 | Servidor activo; agente **Activo** en la ficha + bitácora («Monitoreo CloudWatch: activar») |
| 1:58 | Espera de las primeras métricas (×16) → tarjeta: **mem 22.0 % · disk 27.0 %** en CWAgent, leídos de CloudWatch de verdad |
| 2:33 | Desactivar (el servicio se DETIENE, sin desinstalar) y reactivar: Activo de nuevo |
| 3:00 | Teardown con triple confirmación → EC2 terminated, IP liberada |
| 3:32 | Resultado: montaje+agente OK, métricas OK, incidentes ninguno |

Revisado entero (fotogramas cada 9 s): sin credenciales visibles; la
contraseña del paso 3/3 aparece como puntos. Costo AWS de la demo completa
(9 tomas, 2 días, incluido el 3066 vivo una noche para confirmar métricas):
≈ US$ 0,80.

## `ensayo_pcm_self.mp4` — 10:11, 1080p

Ensayo COMPLETO de la mudanza de PCM a su propio servidor (punto 9 del plan,
`MUDANZA_PCM_SELF.md`), en la sandbox (`pcm_demo`, cuenta «Personal Prueba
Real», us-east-1, t3.small, Odoo 19 **Community**). Todo ocurre de verdad:
driver `grabar_ensayo_pcm_self.py`, toma 12 (las tomas 7–11 cazaron bugs del
driver: banner de expiración, slug de instancia ≠ nombre de máquina, confirm
nativo del restore, poll stale, is_self en instancia duplicada de la copia,
guarda del ticket antes que la self en `action_terminate` crudo).

| Minuto | Tramo |
|---|---|
| 0:00 | Portada + montaje: entorno Producción Community con perfil PCM (espera ×16) |
| 1:33 | Servidor activo; perfil verificado por SSM: conf jobrunner, queue_job clonado, clave Fernet nacida EN el servidor, pip extra |
| 3:20 | Dump espejo del runbook (freeze crons → copia → desinstalar 29 Enterprise + bundle → dump); subida a S3 por Daryl, verificada desde el servidor (×32) |
| 4:12 | Módulo PCM por paquete local al addons dir (PAT real queda para el día D) |
| 4:59 | Restore SIN neutralizar (destino Producción): confirm + triple confirmación + job en verde (×16) |
| 5:33 | El PCM nuevo despierta: web.base.url/report.url, crons reactivados, SMTP y cola INTACTOS |
| 7:16 | Credenciales recargadas por Daryl en la UI (opción b: la clave vieja no descifra, las keys nunca pasan por Claude) y verificadas con sync real |
| 8:26 | Autogestión: is_self → botones fuera + `action_stop`, wizard de terminar y `job_lifecycle` rechazados («hospeda a ESTE PCM») |
| 9:00 | Simulacro del corte: viejo apagado, el nuevo solo, ROLLBACK (viejo vuelve con crons) |
| 9:54 | Teardown real con triple confirmación → EC2 terminated, IP liberada |

Revisado entero (fotogramas cada 25 s): contraseñas solo como puntos, las keys
de AWS jamás aparecen (las cargó Daryl en su navegador, fuera de la grabación).
Costo AWS del ensayo completo (12 tomas, 2 días): ≈ US$ 0,40.

## `dominio_bj_uat_2026-10-28.mp4` — 1:45, 1080p

Dominio definitivo de bj-uat en la cuenta de Primate: «Crear DNS ahora» con
`barracajuliav19.erp.primateuy.com` (primera prueba real de punta a punta de
Route 53 + certificado). PCM propone `<slug>.erp.primateuy.com` y la zona por
defecto; se cambia al nombre definitivo; registro A → INSYNC → propagación →
certificado → https verificado → retiro de sslip.io (espera ×8). Después:
http → https, candado, `/web/database/manager` 404 y el registro en «Registros
DNS». Revisado entero: sin credenciales ni datos del cliente.

## `montaje_bj_uat_2026-10-28.mp4` — 7:55, 1080p

Montaje REAL de bj-uat (UAT de Barraca Julia, no productivo) en la cuenta de
Primate, us-east-1, desde `pcm_produccion`. Uso interno.

| Minuto | Tramo |
|---|---|
| 0:00 | Portada |
| 0:06 | La configuración precargada, para el OK (se cierra sin aprovisionar) |
| 0:50 | Aprovisionamiento: formulario verificado, región en verde, contraseña maestra desde archivo (espera ×8) |
| 2:29 | Restore — 1er intento (incidente: PCM se reiniciaba por `limit_time_real`, queue_job relanzó; este intento frenó en el chequeo de espacio sin tocar la base) (espera ×32) |
| 3:56 | Restore — corrida limpia: neutralización antes de arrancar Odoo y checklist en verde (espera de 28 min ×32) |
| 5:54 | Checks: `/web/login` sin selector, `/web/database/manager` 404, login administrator, 115 módulos, 0 crons, 0 SMTP, `web.base.url`, adjuntos, candado, IP fija en PCM |
| 7:48 | Resultado |

Revisado entero (hojas de contacto cada 2 s): contraseñas solo como puntos, sin
credenciales de AWS; contenido del Odoo del cliente blureado. Quedan a la vista
el nombre del cliente en la página pública de login y el número de cuenta AWS en
la ruta S3 del restore (uso interno). Scripts: scratchpad de la sesión (`f9/`).

## `sslip_automatico_sandbox_2026-10-27.mp4` — 3:46, 1080p

Prueba real del «Dominio sslip.io automático» en la sandbox (`pcm_demo`, cuenta
«Personal Prueba Real», us-east-1, t3.small, disco 20 GB, sin restore). Segunda
corrida: la primera encontró un bug (ver abajo), se corrigió con su test y se
repitió entera.

| Minuto | Tramo |
|---|---|
| 0:00 | Portada |
| 0:06 | Montaje: nuevo entorno Staging + «Aprovisionar» con IP fija, sslip.io automático y HTTPS (espera ×8) |
| 2:03 | Verificación: los 4 mensajes del chatter en orden, http → https, certificado de Let's Encrypt (YE1, sin STAGING) |
| 2:48 | Terminación con triple confirmación + IP fija liberada (espera ×8) |
| 3:39 | Resultado |

Hallazgos de la corrida (corregidos, con test):
- Con sslip.io marcado, «Crear registro DNS» quedaba marcado y oculto: el
  botón «Aprovisionar» no hacía nada (pedía una zona invisible). Ahora sslip.io
  apaga Route 53 en la pantalla y en el servidor.
- El chatter del entorno no contaba el montaje entero (la IP fija y el HTTPS
  quedaban en la máquina y en la instancia). Ahora tiene los 4 pasos en orden.
- Terminar: si el job choca con una escritura concurrente sobre la máquina,
  queue_job lo reintenta; el reintento encontraba la IP fija ya liberada en AWS
  y no la limpiaba en PCM. Ahora la limpia y lo deja en chatter y bitácora.

Revisado entero (hojas de contacto cada 2 s): contraseñas solo como puntos,
sin credenciales de la cuenta AWS. Scripts: scratchpad de la sesión (`f8/`).

## `ensayo_permisos_sandbox_2026-10-27.mp4` — 14:16, 1080p

Ensayo de los permisos de la plantilla `pcm-prerequisitos` en la sandbox
(usuarios `pcm-prod-prov-cfn` y `pcm-prod-ro-cfn`), con la versión final de
la plantilla. Uso interno: se ven datos de la base de prueba del cliente.

| Minuto | Tramo |
|---|---|
| 0:00 | Portada |
| 0:06 | Montaje con la cuenta de aprovisionamiento (IP fija marcada) |
| 1:06 | Reintento sin duplicado (demora de EC2; mismo token, misma máquina) |
| 1:49 | HTTPS por sslip.io |
| 3:18 | Restore del dump reducido + neutralización (espera ×8) |
| 4:47 | Chequeos después del restore |
| 6:37 | Importación de solo lectura + gemelo (documento en 3 pasos y ficha) |
| 8:31 | «Crear DNS ahora» en la zona sin delegar: certbot falla, sigue en sslip.io |
| 10:26 | «Asociar dominio existente»: no resuelve, no cambia nada |
| 11:11 | «Asociar dominio existente»: dominio que sí apunta, cambio sin corte |
| 12:52 | Terminación con triple confirmación + liberación de la IP fija (espera ×8) |
| 14:09 | Resultado |

Notas:
- El aviso de 10:26 muestra el texto anterior («todavía no resuelve, no a la
  IP del servidor»); quedó corregido después de grabar.
- Revisado entero (hojas de contacto cuadro a cuadro): sin keys ni
  contraseñas visibles; el bloque de credenciales de la cuenta AWS se oculta
  en la grabación.
- Scripts de grabación y armado: scratchpad de la sesión (`f7/`).

## mudanza_pcm_self.mp4 — EL DÍA REAL (2026-11-16, 10:36)

La mudanza REAL de PCM a su propio servidor: P1 montaje (wizard verificado
campo a campo, maestra de Daryl a puntos) → P2 dominio APEX
`primatecloudmanager.com` + repo clonado con PAT (revocado en cámara) → F1
congelamiento → F2 dump espejo sin Enterprise (sha256 en tarjeta) → F3
restore con triple confirmación destructiva → F4 despertar (2FA, keys
nuevas, is_self, backup real, bj-uat) → F5 fencing + Mac apagada como
respaldo de emergencia. Ensamblado de 6 tomas (las credenciales se tipearon
en campos password o fuera de cámara; un fotograma muestra un código TOTP
parcial ya vencido). Driver: `grabar_dia_d_pcm_self.py` (modos de
reanudación desde_p2/repo/dump/f4/f5).
