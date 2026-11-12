# Monitoreo CloudWatch en servidores PCM (pedido INCO 2026-11)

Todo servidor que monta PCM puede mandar **RAM y disco** a CloudWatch con el
**agente oficial de AWS**, usando la configuración que INCO mantiene en un
parámetro SSM (hoy `/alarm/AWS-CWAgentLinConfig`). El script de INCO es para
Amazon Linux; los servidores de PCM son Ubuntu, así que PCM instala el `.deb`
oficial (`amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/...`) y
aplica la MISMA config (`fetch-config -m ec2 -c ssm:<parámetro> -s`).

## Qué hace PCM

- **Opción «Monitoreo CloudWatch»** en la ficha de cada entorno montado por
  PCM (sección propia, con estado y botones Activar/Desactivar).
  - **Activar**: instala el `.deb` (idempotente: si ya está, no re-instala),
    aplica la config del parámetro, habilita y arranca el servicio, y verifica
    que quedó `active`. El resultado queda en la ficha: **Activo**,
    **Pendiente** o **Error con motivo** (p. ej. parámetro inexistente en la
    región). Un error **no bloquea** ninguna otra operación.
  - **Desactivar**: detiene y deshabilita el servicio, **sin desinstalar**.
  - Bloqueada para entornos **importados**. Permitida sobre el servidor que
    hospeda a ESTE PCM (no es destructiva).
- **Montaje nuevo**: el wizard trae la opción **activada por default**; el
  agente se instala al final del montaje (incluido el perfil PCM →
  `pcm.erp.primateuy.com` nace con el agente).
- **Health**: el snapshot periódico de métricas también verifica el servicio
  `amazon-cloudwatch-agent` en los servidores con la opción activa, y pasa la
  ficha a Error (o de vuelta a Activo) según lo que responda el servidor.
- **Parámetro SSM**: el nombre sale del parámetro de sistema
  `pcm.cw_agent_ssm_parameter` (default `/alarm/AWS-CWAgentLinConfig`) y es
  **editable por entorno** en la ficha.
- Todo queda en la bitácora con el tipo «Monitoreo CloudWatch».

## Permisos (una vez por cuenta)

`pcm-ssm-role` (plantilla `infra/cloudformation/pcm-prerequisitos.yaml`) lleva:

- `CloudWatchAgentServerPolicy` (managed de AWS): publicar métricas y leer los
  parámetros `AmazonCloudWatch-*`.
- Inline `PcmCWAgentConfigRead`: `ssm:GetParameter` sobre
  `arn:aws:ssm:*:<cuenta>:parameter/alarm/*` (la config de INCO vive bajo
  `/alarm/`).

El update del stack en una cuenta ya desplegada es **un solo Modify** del
recurso `RolSSM` (el nombre del rol no cambia → sin replacement, los
servidores existentes toman los permisos al instante, sin reinicio).

## Paso a paso: activar en bj-uat

1. **Permisos**: actualizar el stack `pcm-prerequisitos` de la cuenta de
   Primate con la plantilla nueva (consola → CloudFormation → Update →
   reemplazar plantilla). Revisar el change set: tiene que ser **un único
   Modify** de `RolSSM`. Ejecutar.
2. **Config**: confirmar con INCO que el parámetro
   `/alarm/AWS-CWAgentLinConfig` existe en `us-east-1` (región de bj-uat) y
   que la config sirve para Ubuntu (rutas de disco `/`). Si INCO usa otro
   nombre, ponerlo en el parámetro de sistema `pcm.cw_agent_ssm_parameter`
   (Ajustes → Técnico → Parámetros del sistema) o editarlo en la ficha del
   entorno.
3. **Activar**: PCM → Entornos → bj-uat → sección «Monitoreo CloudWatch» →
   **Activar**. En ~1 minuto la ficha pasa a **Activo** (si da Error, el
   motivo queda a la vista y en la bitácora).
4. **Verificar**: consola AWS → CloudWatch → Metrics → namespace **CWAgent**:
   tienen que aparecer `mem_used_percent` y `disk_used_percent` de la
   instancia de bj-uat (las métricas llegan según el `metrics_collection_interval`
   de la config de INCO; esperar un par de minutos).

## Operación

- **Desactivar / reactivar**: mismos botones; desactivar detiene el servicio y
  deja de publicar métricas (las históricas quedan en CloudWatch).
- **Cambio de config de INCO**: como el agente lee el parámetro al aplicar
  (`fetch-config`), tras un cambio de config hay que **Desactivar y volver a
  Activar** (re-aplica el parámetro). No hace falta tocar nada más.
- **Costo**: las métricas custom de CloudWatch cuestan ~US$ 0,30/métrica/mes
  (mem + disk ≈ US$ 0,60/servidor/mes, según la config de INCO).
