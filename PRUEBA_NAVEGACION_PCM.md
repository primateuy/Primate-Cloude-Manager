# Guion de prueba manual — Navegación y hub de PCM

Recorrido completo de la app tras el rediseño de navegación (pila + breadcrumb,
drill-through, hub del entorno, sidebar completo, wizards en drawer).

Requisitos: demo en `http://localhost:19088` (admin/admin). Abrir con
`?debug=assets` la primera vez para ver assets frescos.

## 1. Entrada y sidebar
1. Menú **Cloud Manager → Inicio**. Se abre la app a página completa (sin tapar la
   barra de Odoo: se puede entrar y salir).
2. En el sidebar, verificar los grupos colapsables con paridad con los menús
   nativos: **Inventario** (Entornos, Instancias EC2, Crear instancia EC2, Bases
   de datos, Repositorios, Registros DNS), **Operaciones** (Despliegues, Bitácora)
   y **Configuración** (Cuentas AWS, Proyectos). Colapsar/expandir cada grupo.
3. El acento (selector arriba a la derecha) sigue funcionando en las 5 variantes.

## 2. Entornos → hub del entorno
1. Sidebar **Entornos**. Buscar/filtrar. Botón **Nuevo entorno** abre el form
   nativo inline (no popup); volver con el breadcrumb.
2. Click en un entorno (p. ej. "Forum Producción"). Se abre el **hub**:
   - Cabecera con badge de estado y chips (tipo, versión, proyecto, cuenta, URL).
     Proyecto y cuenta son clickeables (drill-through).
   - **Barra de acciones**: según estado, aparecen Aprovisionar / Crear staging /
     Refrescar staging / Sincronizar. Botón **Gestionar** abre el form nativo.
   - Sección **Instancias**: cada servidor con sus bases anidadas (agrupadas por
     su servidor). Botón **Nueva instancia**.
   - Secciones **Registros DNS**, **Despliegues** (con "Ejecutar" en borradores) y
     **Repositorios** (con Verificar estado / Sincronizar commits / Detectar
     módulos), cada una con su botón de "Agregar".

## 3. Drill-through (pila + breadcrumb)
1. Desde el hub, click en un **servidor** → detalle de servidor. El breadcrumb
   muestra `Entornos / <entorno> / <servidor>`. Cada nivel del breadcrumb vuelve
   a ese punto.
2. En el servidor, click en una **base** asociada → detalle de base de datos.
3. Volver y probar el resto de detalles: repositorio (módulos + commits),
   despliegue (log), DNS, cuenta (entornos + servidores), proyecto (entornos).
   Todos con su cabecera + badges + acciones + listas navegables.
4. Refrescar el navegador (F5) estando en un detalle profundo: la pila se
   restaura desde el hash de la URL.

## 4. Acciones y wizards en drawer (no modales)
1. En el hub, **Aprovisionar** (entorno en borrador/error) abre el wizard en un
   **panel lateral** dentro de la app (no un popup centrado).
   - Completar campos; **Resolver AMI** rellena el campo y el drawer sigue abierto.
   - **Cancelar** cierra el drawer.
   - Introducir un dato inválido y confirmar: aparece el error y **el drawer NO se
     cierra** (los datos quedan). Corregir y confirmar: sale el toast de
     "encolado" y el drawer se cierra.
2. **Nueva instancia** (hub) y **Crear instancia EC2** (sidebar) abren el wizard de
   EC2 en el drawer. En el servidor, **Ejecutar comando** y **Terminar** también.
3. Las acciones que solo encolan (Sincronizar, Detectar módulos, Ejecutar deploy,
   Validar conexión, etc.) muestran un **toast** y refrescan, sin cerrar la app.

## 5. Estados vacíos y consistencia
- Un entorno sin instancias muestra un **empty state** con CTA "Crear la primera
  instancia". Secciones sin datos muestran su vacío ("Sin registros DNS", etc.).
- Ningún registro abre un diálogo modal stock dentro de la app (salvo
  confirmaciones destructivas y toasts).

> Recordatorio: no reiniciar el servidor mientras haya un job de
> aprovisionamiento/staging en curso (queue_job reencola).
