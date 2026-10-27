# Videos de ensayo de PCM (fuera de git)

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
