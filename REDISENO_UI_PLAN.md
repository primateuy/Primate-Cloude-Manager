# REDISEÑO UI — 3 temas configurables + unificación de pantallas nativas

> DISEÑO. Nada de código hasta aprobación. Git-ignored (como el resto de los .md).
> Regla de oro: **una estructura de pantalla + 3 conjuntos de tokens CSS.** El tema cambia el
> VALOR de unas vars (radio, densidad, tipografía, forma), NUNCA el layout ni el markup ni la
> paleta. Si aparece markup distinto por tema, se para: ese es el camino inmantenible.

---

## 0. Arquitectura de capas (por qué esto es barato y no un rediseño de 3 productos)

Hoy el root `.o_pcm_app` ya recibe variables por `rootStyle` (inline). Se apilan TRES capas de
tokens ortogonales sobre el mismo markup:

1. **Paleta + estados** (FIJA, no se toca): navy del sidebar, buckets `--pcm-ok/warn/neutral/error`,
   colores base. Vive en `pcm_theme.scss`/`pcm_app.scss`.
2. **Acento** (ya existe): `--pcm-accent` / `--pcm-accent-ink` / `--pcm-accent-tint`, 5 presets +
   custom, por usuario. Se inyecta en `rootStyle`.
3. **Tema (NUEVO)**: `--pcm-radius-*`, `--pcm-space-*`, `--pcm-font-*`, `--pcm-tab-*`, `--pcm-cmd-*`,
   etc. SOLO forma/densidad/tipografía. Se inyecta en el MISMO `rootStyle`, seleccionado por
   `res.users.pcm_theme` (A/B/C).

Las tres son independientes: cualquier acento funciona en cualquier tema, y ambos sobre la paleta
fija. El tema NUNCA define un color — esa es la línea que separa "capa de forma" de "capa de color".

**Regla dura de implementación:** el SCSS de las pantallas deja de tener valores fijos de
`border-radius`, `padding`, `font-size`, `gap` — todos pasan a `var(--pcm-…)`. Un valor fijo que
quede es un bug del sistema de temas (rompe uno de los tres). El SCSS se audita una vez para
reemplazar los ~40 literales actuales (card 14px, chip 9px, font 14px, paddings, gaps) por tokens.

---

## 1. Sistema de tokens de tema (el corazón) — tabla token × tema

Los tres temas comparten paleta, acento, estados y textos. Difieren SOLO en estos tokens. Valores
propuestos (ajustables al pulir, pero la LISTA de tokens es el contrato):

### Forma / radios
| Token | Para qué | A · Consola técnica | B · Panel de operaciones | C · Editorial cálido |
|---|---|---|---|---|
| `--pcm-radius-card` | tarjetas, hero, paneles | `8px` | `16px` | `10px` |
| `--pcm-radius-control` | botones, inputs, tabs-pill | `6px` | `12px` | `8px` |
| `--pcm-radius-pill` | chips, badges de estado | `6px` (casi recto) | `999px` (pill) | `4px` (recto, editorial) |
| `--pcm-card-border` | borde de tarjeta | `1px solid var(--pcm-line)` | `1px solid var(--pcm-line)` | `1px solid var(--pcm-line)` |
| `--pcm-card-shadow` | elevación | `none` (plano, hairline) | `0 1px 3px rgba(16,41,51,.08)` | `none` (papel) |

### Densidad / espaciado
| Token | Para qué | A | B | C |
|---|---|---|---|---|
| `--pcm-fs-base` | tamaño base de texto | `13px` | `14px` | `15px` |
| `--pcm-space-screen` | padding del área de contenido | `16px` | `24px` | `28px` |
| `--pcm-space-card` | padding interno de tarjeta | `12px 14px` | `20px 24px` | `22px 26px` |
| `--pcm-space-gap` | gap entre tarjetas/secciones | `10px` | `16px` | `18px` |
| `--pcm-space-row` | padding de fila de lista (kv, line) | `6px 10px` | `10px 14px` | `12px 14px` |
| `--pcm-space-kpi-row` | gap entre KPIs | `10px` (fila compacta) | `16px` | `20px` |

### Tipografía
| Token | Para qué | A | B | C |
|---|---|---|---|---|
| `--pcm-font-ui` | cuerpo / labels | stack sans del sistema | stack sans del sistema | stack sans del sistema |
| `--pcm-font-display` | títulos + números destacados | = `--pcm-font-ui` | = `--pcm-font-ui` (peso ↑) | **serif de marca** (bundled) |
| `--pcm-font-mono` | hashes, comandos, IPs | mono del sistema | mono del sistema | mono del sistema |
| `--pcm-fs-title` | h1/h2 de hero y secciones | `1.05rem` | `1.25rem` | `1.6rem` |
| `--pcm-fw-title` | peso de título | `600` | `650` | `500` (serif) |
| `--pcm-fs-kpi` | número grande de KPI | `1.4rem` | `1.8rem` | `2.3rem` |
| `--pcm-fw-kpi` | peso del número | `600` | `650` | `500` (serif) |

### Eyebrows (labels de sección en mayúsculas — protagonistas en C)
| Token | A | B | C |
|---|---|---|---|
| `--pcm-eyebrow-transform` | `uppercase` | `uppercase` | `uppercase` |
| `--pcm-eyebrow-spacing` | `0.04em` | `0.06em` | `0.12em` |
| `--pcm-eyebrow-weight` | `600` | `600` | `700` |
| `--pcm-eyebrow-rule` | línea de acento antes del label | `none` | `none` | `2px de --pcm-accent` |

### Tabs (mismo markup `.o_pcm_tabs > .o_pcm_tab`)
| Token | A · subrayado fino | B · pill | C · eyebrow-underline |
|---|---|---|---|
| `--pcm-tab-radius` | `0` | `999px` | `0` |
| `--pcm-tab-bg-active` | `transparent` | `var(--pcm-accent-tint)` | `transparent` |
| `--pcm-tab-indicator` | `2px solid var(--pcm-accent)` (underline) | `none` (lo hace el bg) | `2px solid var(--pcm-accent)` |
| `--pcm-tab-transform` | `none` | `none` | `uppercase` |
| `--pcm-tab-pad` | `6px 10px` | `8px 16px` | `6px 12px` |

### Bloque de comando (mismo markup `.o_pcm_cmd_body`)
| Token | A · terminal oscuro | B · claro suave | C · enmarcado |
|---|---|---|---|
| `--pcm-cmd-bg` | `var(--pcm-navy)` | `var(--pcm-surface)` | `var(--pcm-navy)` |
| `--pcm-cmd-ink` | `#D8E6E4` | `var(--pcm-ink)` | `var(--pcm-teal-tint)` |
| `--pcm-cmd-border` | `none` | `1px solid var(--pcm-line)` | `2px izq de --pcm-accent` |
| `--pcm-cmd-radius` | `var(--pcm-radius-control)` | `var(--pcm-radius-control)` | `var(--pcm-radius-control)` |

### Botones / chips (heredan radios de arriba)
| Token | A | B | C |
|---|---|---|---|
| `--pcm-btn-pad` | `6px 12px` | `9px 16px` | `8px 14px` |
| `--pcm-chip-pad` | `3px 8px` | `5px 12px` | `4px 10px` |
| `--pcm-btn-weight` | `600` | `600` | `600` |

**Total: ~30 tokens.** Un solo markup + estos 30 valores producen las 3 estéticas de los bocetos.
Ningún token es un color de paleta (solo referencian `--pcm-line/navy/surface/accent`, que son
fijos). Eso garantiza que "los colores no se tocan".

**Nota tipográfica (única dependencia nueva):** el Tema C necesita UNA cara serif de display
(solo títulos y números). Se **bundlea un `.woff2`** en assets (Odoo permite fuentes en el bundle;
no hay CDN). A y B usan el stack sans del sistema, cero peso extra. Si preferís no bundlear,
fallback a un stack serif del sistema (`Georgia, "Times New Roman", serif`) — se decide en B1.

---

## 2. Inventario de pantallas (OWL vs nativo)

### Ya son OWL (reciben el tema en B1, sin reescribir layout)
- **Core / listas OWL**: `inicio`, `entornos`, `proyectos`, `costos`.
- **Detalles OWL (drill-through)**: `entorno_detalle`, `servidor_detalle`, `instancia_detalle`
  (con tabs + bloque de comando — el caso más rico), `proyecto_detalle`, `base_datos_detalle`,
  `repositorio_detalle`, `despliegue_detalle`, `dns_detalle`, `cuenta_detalle`.
- **13 pantallas** ya en el lenguaje OWL. B1 prueba que las 3 estéticas andan sobre TODAS sin tocar
  el markup.

### Todavía nativas (form/list stock de Odoo — hay que migrar)
Son las **listas** de inventario y los **wizards de creación**, embebidas hoy con el componente
`View` (nativeList) o el `wizard_drawer` (form nativo dentro del drawer):

| Pantalla | Qué usa hoy | Prioridad (Daryl) |
|---|---|---|
| **Registros DNS** (list + crear/editar) | nativeList + form nativo | **1 — "realmente confuso"** |
| **Crear entorno** (wizard provision) | form nativo en drawer | **2 — "se muestra feo el formulario"** |
| Bases de datos (list) | nativeList | 3 |
| Repositorios (list + agregar) | nativeList + form | 4 |
| Despliegues (list + ejecutar) | nativeList + form | 5 |
| Crear instancia EC2 (wizard) | form nativo | 6 |
| Cuentas AWS (list + form) | nativeList | 7 |
| Instancias EC2 (list) | nativeList | 8 |
| Bitácora / operation.log (list) | nativeList | (queda nativa: es una grilla de auditoría, read-only; no molesta) |

**Escape hatch:** "Gestionar" (form nativo completo, con chatter) se conserva como acción avanzada
en cada detalle. La operación NORMAL (crear DNS, crear entorno, agregar repo) no debe requerirlo.

---

## 3. Kit de componentes base reutilizables

Hoy las superficies compartidas son **clases CSS repetidas** (`o_pcm_card` ×79, `o_pcm_kv` ×89,
`o_pcm_hero`, `o_pcm_tab`, `o_pcm_cmd`, `o_pcm_kpi`), usadas across 13 pantallas. El tema funciona
sobre esas clases sin componetizar. Pero para (a) DRY y (b) construir las pantallas migradas y sus
FORMULARIOS, se formaliza un kit chico de componentes OWL que TODAS comparten y que responde a los
tokens:

**Kit de display (envuelven las clases actuales — 1 implementación, N usos):**
- `PcmDetailHeader` — el hero: título (usa `--pcm-font-display`), badge de estado, barra de acciones.
- `PcmCard` — tarjeta (`--pcm-space-card`, `--pcm-radius-card`, `--pcm-card-shadow`).
- `PcmKpi` / `PcmKpiRow` — número grande (`--pcm-fs-kpi`, `--pcm-font-display`) + label.
- `PcmTabs` — tabs (lee `--pcm-tab-*`: subrayado/pill/eyebrow según tema, mismo markup).
- `PcmCommandBlock` — bloque de comando (`--pcm-cmd-*`) con botón copiar.
- `PcmKv` / `PcmLine` — filas key-value y filas de lista clickeables.
- `PcmEyebrow` — label de sección en mayúsculas (protagonista en C vía `--pcm-eyebrow-rule`).

**Kit de formulario (NUEVO — lo que habilita migrar las pantallas nativas):**
- `PcmForm` — contenedor de formulario OWL en el drawer/pila (reemplaza el form nativo stock).
- `PcmField` — campo etiquetado con estados de validación y **copy de error accionable** (no el
  traceback crudo). Variantes: texto, número, textarea.
- `PcmSelect` — envuelve el `o_select_menu` de Odoo con el look del tema (el mismo widget que ya
  usa el smoke, pero themed).
- `PcmToggle` / `PcmCheckbox` — booleanos.
- `PcmFormActions` — barra Guardar/Cancelar (acción principal con `o_pcm_btn_accent` en B).

Los métodos backend que estos formularios llaman **ya existen** (crear DNS, provision wizard, etc.);
el kit de formulario es SOLO presentación + validación + copy — cero lógica de negocio nueva.

**Decisión de alcance (a aprobar):** el kit de display se puede introducir de dos formas —
   (a) **incremental**: B1 aplica tokens a las clases actuales (prueba los 3 temas sin refactor), y
       cada pantalla adopta los componentes cuando se la toca. Bajo riesgo, no big-bang.
   (b) **de una**: extraer los 7 componentes de display y reescribir las 13 pantallas para usarlos
       en B1. Más limpio pero es reescribir layout que hoy anda (riesgo).
   **Recomiendo (a):** el tema NO necesita los componentes para funcionar (funciona sobre clases);
   los componentes se ganan valor recién con la migración de formularios (B2+). Big-bang de las 13
   pantallas contradice "probar que las 3 andan sin reescribir layout".

---

## 4. Plan por bloques (freno entre cada uno)

- **B1 — Sistema de temas + tokens + selector (sobre lo que YA es OWL).**
  - `res.users.pcm_theme` (Selection A/B/C, default A) + a `SELF_READABLE/WRITEABLE_FIELDS` (calcado
    del acento).
  - `THEME_TOKENS` en `pcm_app.js` (el equivalente a `ACCENT_PRESETS`): mapa tema→tokens.
  - `rootStyle` extendido para emitir acento **+** tokens de tema en la misma cadena inline.
  - Auditoría del SCSS: reemplazar los ~40 literales por `var(--pcm-…)` con fallback = valor actual
    (así, sin tema seteado, se ve idéntico a hoy = Tema A ≈ estado actual, bajo riesgo).
  - Selector en la topbar, al lado del acento: acento (swatches) **+** estilo (3 opciones, segmented
    control con nombre). Cambio instantáneo (setea la var, persiste en `res.users`, sin reload).
  - Tema C: bundlear/definir la serif de display.
  - **Puerta:** las 13 pantallas OWL renderizan bien en A/B/C (smoke en los 3 temas, ver §5).
  - **FRENO.**

- **B2 — Migrar Registros DNS (prioridad 1) + el kit de formulario.**
  - Pantalla OWL de lista DNS (reemplaza el nativeList) + `PcmForm` para crear/editar (reemplaza el
    form nativo del drawer). Estrena el kit de formulario (§3).
  - Copy de error accionable (no traceback boto3/SSM).
  - "Gestionar" queda como escape hatch.
  - Smoke: escenario DNS-OWL en los 3 temas.
  - **FRENO.**

- **B3 — Migrar Crear entorno (prioridad 2).**
  - El wizard de provision (cómputo/BD/DNS) reconstruido con el kit de formulario, en el drawer/pila.
    Es el form más grande → valida el kit de verdad.
  - Mantiene el "recuerda lo ingresado" y la doble confirmación destructiva existentes.
  - Smoke: crear entorno OWL (el ciclo error→corrección→éxito ya existe en s04, se adapta).
  - **FRENO.**

- **B4+ — El resto de a una** (Bases de datos, Repositorios, Despliegues, Crear instancia EC2,
  Cuentas AWS), cada una con su freno y su escenario de smoke. Bitácora queda nativa (grilla de
  auditoría read-only). Orden por la tabla de prioridad §2.

---

## 5. Cómo se prueba

- **B1 — el tema no rompe ninguna pantalla:** escenario nuevo `s11_temas` que, en la app real,
  recorre las pantallas core (inicio, entornos, un detalle con tabs+comandos = instancia, proyecto,
  costos) **cambiando el tema A→B→C con el selector** y verificando cero errores de consola + que
  cada pantalla sigue renderizando su hero/tarjetas. Es el chequeo barato de "las 3 andan".
- **Alternativa/complemento:** correr el smoke completo 3 veces con `pcm_theme` seteado por fixture
  (A, B, C) — más caro, más exhaustivo. Propongo el escenario de switch (barato) + un run completo
  en Tema B como sanity (el más distinto de A).
- **Cada pantalla migrada entra al smoke** con su escenario propio (DNS-OWL, crear-entorno-OWL, …),
  en al menos un tema no-default para pescar literales que se hayan escapado.
- Recordatorio operativo del proyecto: tras tocar assets, `-u` pcm_demo + limpiar bundles (o
  `?debug=assets`) para no pelear con la caché de templates.

---

## 6. Conflictos con el sistema de acento actual (lo que hay que decidir)

1. **Ninguno estructural.** El tema calca el mecanismo del acento (campo en `res.users`, mapa de
   presets en JS, inyección en `rootStyle`, selector en la topbar). Conviven en la misma cadena
   inline sin pisarse.
2. **`pcm_accent_custom` (hex libre):** el tema NO tiene equivalente "custom" — son 3 opciones
   cerradas (A/B/C). Es deliberado (los 3 temas son diseños curados, no configurables al detalle).
3. **El acento sigue mandando el color de acción/tint** en los 3 temas — el tema nunca lo pisa.
4. **Riesgo real y único:** los ~40 valores fijos hoy hardcodeados en el SCSS. Si UNO queda sin
   tokenizar, ese aspecto no cambia entre temas (bug silencioso). Mitigación: la auditoría de B1 es
   exhaustiva y el smoke en 3 temas la caza (una pantalla que no cambia de densidad/radio salta).
5. **Odoo.sh/native "header morado":** al migrar formularios, el chatter y el header stock
   desaparecen de la operación normal (quedan en "Gestionar"). Eso es parte del objetivo, pero
   implica que acciones que hoy dependían del chatter (ej. ver el historial de un registro) se
   acceden por "Gestionar" — a confirmar que no perdemos nada crítico de la operación diaria.

---

## 7. Decisiones a aprobar antes de escribir código

1. **Valores de los tokens** (§1): ¿te cierran los 30 tokens y sus 3 valores, o querés ajustar
   algún tema contra los bocetos?
2. **Serif del Tema C**: ¿bundleamos una cara de marca (.woff2) o stack serif del sistema?
3. **Alcance del kit de display** (§3): incremental (recomendado) vs big-bang de las 13 pantallas.
4. **Testing** (§5): escenario de switch A/B/C + un run completo en Tema B, ¿alcanza o querés el
   smoke completo ×3?
5. **Orden de migración** (§2/§4): DNS → Crear entorno → resto. ¿Confirmás?

**FRENO. Espero aprobación (o ajustes) antes de una línea de código.**
