# Primate Cloud Manager — Resumen del proyecto y plan de trabajo

## De qué se trata

La idea de este proyecto es construir un módulo dentro de Odoo 19 Enterprise que nos permita administrar de forma centralizada toda la infraestructura AWS que usamos en Primate, tanto la nuestra como la de nuestros clientes. Hoy dependemos de herramientas externas y de conocimiento técnico repartido entre pocas personas para tareas como crear un servidor, instalar un entorno, levantar un staging o configurar un DNS. Lo que queremos es que Odoo deje de ser solo nuestro ERP y pase a ser también el centro único desde donde gobernamos los servidores, los despliegues, los repositorios y el ciclo de vida completo de cada entorno que operamos.

No queremos reemplazar AWS. Queremos gobernarlo desde Odoo. Desde una sola pantalla deberíamos poder conectar cuentas AWS, crear y administrar instancias EC2 y bases PostgreSQL (locales o RDS), instalar distintas versiones de Odoo Community o Enterprise, conectar repositorios de GitHub, desplegar y actualizar, ver logs y métricas, manejar DNS con Route53, gestionar respaldos, crear ambientes de prueba a partir de producción y entender los costos de cada cliente.

Y hay un objetivo que para mí es el corazón de todo esto: trazabilidad técnica completa de cada entorno. Quiero poder mirar cualquier instalación y saber en el momento qué versión de Odoo corre, qué repositorios tiene asociados, qué módulos están disponibles, cuáles están instalados, en qué commit está cada repo y qué se desplegó y cuándo. Eso es lo que hoy nos cuesta reconstruir a mano y lo que más tiempo y errores nos genera.

## Por qué lo dividimos en fases

Esto no es una herramienta más: es una plataforma completa, parecida en alcance a lo que ofrecen productos comerciales enteros. Si intentamos construirla toda de una, nos vamos a quedar meses sin nada funcionando y con un riesgo enorme de equivocarnos en decisiones de base que después son carísimas de cambiar.

Lo dividimos en fases por tres razones concretas. La primera es que las capacidades dependen unas de otras en un orden natural: no puedo saber qué módulos tiene un entorno si todavía no sé instalar Odoo; no puedo instalar Odoo si no sé crear el servidor; y no puedo crear el servidor si antes no resolví cómo conectarme de forma segura a la cuenta del cliente. Las fases siguen esa cadena de dependencias. La segunda razón es que cada fase nos deja algo que funciona y que ya aporta valor, en vez de esperar al final para ver resultados. La tercera es que cada fase cerrada reduce el riesgo de la siguiente: vamos aprendiendo del entorno real y validando las decisiones difíciles antes de construir encima de ellas.

Hay además un orden de prudencia, no solo técnico: primero le damos al sistema la capacidad de ver lo que ya tenemos en AWS, y recién después la de crear o eliminar recursos. Preferimos que aprenda a mirar antes que a tocar.

## Qué se hace en cada fase

**Fase 1 — Las fundaciones.** Acá no se ve nada espectacular en pantalla, pero es lo que sostiene todo el resto. Armamos el esqueleto del módulo, resolvemos cómo guardar las credenciales de forma segura (nunca en texto plano, y usando roles temporales en las cuentas de cliente siempre que se pueda, en lugar de claves fijas), dejamos la regla de que todo recurso que creemos quede etiquetado (para después poder inventariar y atribuir costos), montamos la bitácora inmutable que registra cada acción, y dejamos lista la base para que las tareas largas corran en segundo plano sin trabar la interfaz.

**Fase 2 — Ver lo que ya tenemos.** Antes de darle al sistema la capacidad de crear o borrar algo, le damos la de mirar. Conectamos las cuentas AWS e importamos automáticamente todo lo que ya está corriendo: las instancias, las bases de datos y los registros de DNS. El primer resultado tangible es tener, por fin, un inventario único y actualizado de toda nuestra infraestructura, sin haber tocado nada.

**Fase 3 — Operar los servidores.** Con el inventario hecho, sumamos las acciones sobre las instancias que ya existen: arrancarlas, detenerlas, reiniciarlas, eliminarlas y ejecutar comandos en ellas, todo desde Odoo y todo quedando registrado. Las acciones peligrosas, como eliminar una instancia, piden confirmación expresa.

**Fase 4 — Crear entornos nuevos.** Acá llega la automatización más esperada: crear un entorno completo de punta a punta desde un asistente. El sistema levanta el servidor, prepara la base de datos (local o RDS), instala la versión de Odoo elegida (Community o Enterprise), configura el proxy y el certificado, y crea el registro de DNS. Lo que hoy lleva horas de trabajo manual y conocimiento específico, pasa a ser completar un formulario.

**Fase 5 — La trazabilidad.** Esta es la fase que más nos diferencia y la que más valor nos da en el día a día. El sistema averigua, para cada entorno, qué versión de Odoo corre, qué repositorios tiene, qué módulos están disponibles y cuáles instalados, y en qué commit está parado cada repositorio, marcando incluso cuándo un servidor tiene cambios manuales que no están en el remoto. Esa información se refresca sola y también cuando la pidamos.

**Fase 6 — Despliegues.** Una vez que sabemos el estado exacto de cada entorno, pasamos a poder cambiarlo de forma controlada: traer los últimos cambios, cambiar de rama, posicionar un repositorio en un commit puntual, actualizar módulos o reiniciar servicios. Cada despliegue queda registrado con el commit de origen, el de destino y su resultado, y se puede revertir.

**Fase 7 — Ambientes de prueba.** Automatizamos la creación de un staging a partir de un entorno de producción: se copia la base, se la "neutraliza" para que no mande correos ni dispare integraciones reales, se clonan los repositorios en el mismo commit y se levanta con su propio subdominio. También se puede refrescar un staging existente con datos frescos sin rehacer toda la infraestructura.

**Fase 8 — DNS y respaldos.** Sumamos la administración de los registros de DNS en cualquier momento, no solo al crear un entorno, y la gestión de respaldos: definimos qué política de backup esperamos para cada entorno y el sistema la compara contra lo que realmente hay en AWS, avisándonos cuando algo no cumple.

**Fase 9 — Monitoreo y costos.** Incorporamos las métricas técnicas (CPU, memoria, disco, conexiones), el acceso a los logs desde la propia pantalla del entorno, y la visibilidad de costos de AWS, atribuidos a cada cliente y entorno gracias al etiquetado que dejamos desde la Fase 1, incluso repartiendo el costo de los recursos compartidos.

**Fase 10 — Más allá de AWS (a futuro).** Todo se diseña pensando en que AWS es el primer proveedor, no el único. Sobre la misma base quedan para más adelante el soporte de Azure y Google Cloud, y módulos complementarios como la facturación por cliente, un portal para que los clientes consulten el estado de sus entornos y el análisis de logs asistido.
