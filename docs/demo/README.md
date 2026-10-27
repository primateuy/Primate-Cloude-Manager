# Videos de ensayo de PCM (fuera de git)

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
