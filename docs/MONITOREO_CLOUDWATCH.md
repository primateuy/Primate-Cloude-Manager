# Monitoreo CloudWatch en servidores PCM (pedido INCO 2026-11)

Todo servidor que monta PCM puede mandar **RAM y disco** a CloudWatch con el
**agente oficial de AWS**, usando **la misma configuración de INCO**
(`mem_used_percent` + `disk_used_percent` de `/`, namespace `CWAgent`, cada
60 s). El script de INCO es para Amazon Linux; los servidores de PCM son
Ubuntu, así que PCM instala el `.deb` oficial
(`amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/...`).

**Modo default (respuesta de INCO 2026-11-12): archivo local, sin Parameter
Store.** La config de INCO viaja embebida en PCM (plantilla versionada
`primate_cloud_manager/data/cw_agent_config.json`, byte a byte la que pasó
INCO), PCM la escribe en el servidor en
`/opt/aws/amazon-cloudwatch-agent/etc/pcm-cloudwatch.json` y el agente la
carga con `fetch-config -m ec2 -c file:<ruta> -s`.

## Qué hace PCM

- **Opción «Monitoreo CloudWatch»** en la ficha de cada entorno montado por
  PCM (sección propia, con estado y botones Activar/Desactivar).
  - **Activar**: instala el `.deb` (idempotente: si ya está, no re-instala),
    escribe la config, la aplica, habilita y arranca el servicio, y verifica
    que quedó `active`. El resultado queda en la ficha: **Activo**,
    **Pendiente** o **Error con motivo**. Un error **no bloquea** ninguna
    otra operación.
  - **Desactivar**: detiene y deshabilita el servicio, **sin desinstalar**.
  - Bloqueada para entornos **importados**. Permitida sobre el servidor que
    hospeda a ESTE PCM (no es destructiva).
- **Montaje nuevo**: el wizard trae la opción **activada por default**; el
  agente se instala al final del montaje (incluido el perfil PCM →
  `pcm.erp.primateuy.com` nace con el agente).
- **Health**: el snapshot periódico de métricas también verifica el servicio
  `amazon-cloudwatch-agent` en los servidores con la opción activa, y pasa la
  ficha a Error (o de vuelta a Activo) según responda el servidor.
- **La config es editable** sin tocar código: parámetro de sistema
  `pcm.cw_agent_config` (JSON completo del agente; vacío = la plantilla de
  INCO). Tras cambiarla: Desactivar y Activar en cada servidor (re-escribe y
  re-aplica).
- **Modo alternativo por entorno**: si el campo «Parámetro SSM del agente»
  tiene valor, la config se lee de ese parámetro (`ssm:<nombre>`) en vez del
  archivo local. Ojo: ese modo exige darle al rol `pcm-ssm-role` el permiso
  `ssm:GetParameter` sobre ese nombre (la plantilla NO lo trae).
- Todo queda en la bitácora con el tipo «Monitoreo CloudWatch».

## Permisos (una vez por cuenta)

`pcm-ssm-role` (plantilla `infra/cloudformation/pcm-prerequisitos.yaml`) suma
**solo** la managed policy de AWS **`CloudWatchAgentServerPolicy`**
(PutMetricData restringido al namespace CWAgent). El update del stack en una
cuenta ya desplegada es **un solo Modify** del recurso `RolSSM` (el nombre del
rol no cambia → sin replacement; los servidores existentes toman el permiso al
instante, sin reinicio).

## Paso a paso: cuenta real (Primate) + bj-uat

1. **Update del stack `pcm-prerequisitos`** (consola → CloudFormation →
   stack → Update → Replace template → subir
   `infra/cloudformation/pcm-prerequisitos.yaml`), manteniendo los
   parámetros: `Sufijo` vacío, **`ZonaDns=Z06178081BO0Q1OK7MTAO`** y
   **`NombresDns=primatecloudmanager.com,*.primatecloudmanager.com`** (zona
   nueva 2026-11-16; hecho ese día). Revisar el change set: **un único
   Modify de `RolSSM`**. Ejecutar.
2. **Activar en bj-uat**: PCM → Entornos → bj-uat → sección «Monitoreo
   CloudWatch» → **Activar**. En ~1 minuto la ficha pasa a **Activo** (si da
   Error, el motivo queda a la vista y en la bitácora).
3. **Verificar**: consola AWS → CloudWatch → Metrics → namespace **CWAgent**:
   `mem_used_percent` y `disk_used_percent` de la instancia de bj-uat
   (aparecen a los ~2 minutos).
4. Confirmar con INCO que sus alarmas enganchan esas métricas (mismo
   namespace y dimensiones `InstanceId` que su config).

## Operación

- **Desactivar / reactivar**: mismos botones; desactivar detiene el servicio y
  deja de publicar métricas (las históricas quedan en CloudWatch).
- **Cambio de config**: editar `pcm.cw_agent_config` (o esperar una versión
  nueva de la plantilla) y Desactivar + Activar en cada servidor.
- **Costo**: las métricas custom de CloudWatch cuestan ~US$ 0,30/métrica/mes
  (mem + disk ≈ US$ 0,60/servidor/mes).
