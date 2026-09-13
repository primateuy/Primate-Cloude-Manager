# Smoke-test de UI — Primate Cloud Manager

Pruebas headless de la app OWL de PCM (montaje, sidebar, hub, drill-through,
breadcrumb y el drawer de wizard), pensadas para correr **antes de cada entrega
visual**. Manejan un Chromium headless con Playwright y capturan errores de
consola/JS + screenshots de evidencia.

## Requisitos (una vez)

Playwright ya está en el `.venv` del proyecto. Falta bajar el binario de Chromium:

```bash
.venv/bin/playwright install chromium
```

(Queda en la caché de Playwright, no toca `/Applications`.)

## Cómo correr

Con la base demo levantada en `:19088` (ver `PrimateCloudeManager.conf`):

```bash
PY=.venv/bin/python
ODOO_BIN=../forum/_shared/community/odoo-bin
CONF=PrimateCloudeManager.conf

# 1. Fixture: entorno DRAFT atado a una cuenta de PRUEBA (creds falsas), para
#    ejercitar el flujo (a) sin crear recursos reales (el job falla con AuthFailure).
$PY $ODOO_BIN shell -c $CONF -d pcm_demo --no-http < tools/smoke_ui/setup_fixtures.py

# 2. Escenarios de navegador (Playwright).
$PY tools/smoke_ui/run.py

# 3. Test unitario del interceptor de acciones (flujo b, sin navegador).
node tools/smoke_ui/interceptor_test.mjs

# 4. (Opcional) limpiar el fixture del demo.
$PY $ODOO_BIN shell -c $CONF -d pcm_demo --no-http < tools/smoke_ui/teardown_fixtures.py
```

Variables de entorno opcionales para `run.py`: `PCM_BASE` (default
`http://localhost:19088`), `PCM_LOGIN`, `PCM_PWD` (default `admin`/`admin`).

## Escenarios (`run.py`)

| Caso | Qué cubre |
|------|-----------|
| `s01_app_y_sidebar` | Montaje de la app + grupos colapsables del sidebar. |
| `s02_hub_y_drill` | Hub del entorno + drill-through a servidor, repositorio, base y cuenta (breadcrumb de 3 niveles). |
| `s03_drawer_cancelar` | Drawer de "Nueva instancia": abre el wizard real embebido y cierra con Cancelar. |
| `s04_drawer_error_correccion_exito` | Ciclo error→corrección→éxito en el drawer de Aprovisionar: confirmar con dato faltante deja el drawer abierto y conserva lo ingresado; corregir y confirmar cierra el drawer. Segundo uso del drawer sin estado colgado. |
| `s06_respaldos_y_wizards_fase8` | Fase 8: sección Respaldos del hub (chip de propósito del backup), wizard de restore en drawer (cancelar) y "Crear staging" desde el detalle del servidor. |
| `s07_dns_crud` | Fase 8.5: sección DNS del hub — crear registro (wizard en drawer) y la fricción alta al intentar borrar un registro de producción (alerta roja + acknowledge). |
| `s08_costos_y_logs` | Fase 9: pantalla de Costos (con "datos al") y el detalle de servidor con métricas (RAM/disco "requiere agente") y el visor de logs. |
| `s05_salir_y_volver` | Salir a Ajustes y volver a Cloud Manager 2 veces: remonta limpio (sin drawers fantasma, con acento y sidebar). |

`interceptor_test.mjs` (flujo b) ejercita `handlePcmActionResult` con cada forma
de acción devuelta por un wizard al confirmar: `act_window` de wizard→drawer,
`act_window` de registro→navegación por la pila, `display_notification`→toast,
otras→servicio de acción. Se simula porque ningún wizard del módulo encadena
acciones hoy.

## Notas

- Los screenshots quedan en `tools/smoke_ui/screenshots/` (ignorada por git).
- El flujo (a) encola un job de aprovisionamiento sobre la cuenta de prueba: falla
  con `AuthFailure` sin crear infraestructura ni facturar. El teardown limpia el
  fixture (que queda en estado `error`).
- No reiniciar el servidor del demo mientras corre el smoke-test.
