# -*- coding: utf-8 -*-
"""Adaptador SSM: ejecución remota de comandos en instancias EC2.

Se prefiere SSM sobre SSH (paramiko queda como fallback en fases posteriores).
Requisito asumido: agente SSM en la AMI + instance profile con permisos SSM.
"""
import time

# Estados terminales de una invocación de comando SSM.
SSM_TERMINAL_STATES = {"Success", "Failed", "Cancelled", "TimedOut"}

# Documento estándar de AWS para correr comandos de shell en Linux.
# Tope por defecto del agente para AWS-RunShellScript (executionTimeout).
SSM_DEFAULT_EXECUTION_TIMEOUT = 3600
RUN_SHELL_DOCUMENT = "AWS-RunShellScript"

# Señales de que el agente SSM aún no está registrado tras arrancar la EC2.
# SendCommand falla con estos errores hasta que el agente queda "online".
SSM_AGENT_NOT_READY = ("InvalidInstanceId", "not in a valid state")


class SsmCommandError(Exception):
    """Un comando SSM terminó en estado no-``Success`` (Failed/TimedOut/Cancelled).

    ``run_script`` levanta esto POR DEFAULT (``check=True``): el contrato es
    fallar RUIDOSO. Antes el default era tragarse el error (devolver el dict y
    confiar en que el llamador mirara ``status``); tres bugs de "el llamador se
    olvidó de mirar" justificaron invertirlo. Quien deba TOLERAR un fallo lo
    pide explícito con ``check=False`` y evalúa el dict devuelto.

    Excepción PURA (sin importar odoo): la capa de servicios no toca el ORM. Los
    modelos que quieran un ``UserError`` lo envuelven; en un ``queue_job`` basta
    con dejarla propagar (el job queda en fallado con este mensaje).
    """

    def __init__(self, instance_id, status, stderr=None, script=None,
                 comment=None, region=None, stdout=None, command_id=None):
        self.instance_id = instance_id
        self.status = status
        self.stderr = stderr
        self.stdout = stdout
        self.script = script
        self.comment = comment
        self.region = region
        self.command_id = command_id
        # Primera línea útil del script (ignora comentarios y vacías): dice QUÉ
        # se intentó correr sin volcar el heredoc entero.
        first = ""
        for line in (script or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                first = line
                break
        detail = (stderr or "").strip() or (stdout or "").strip() or "(sin salida)"
        super().__init__(
            "Comando SSM en estado %s en la instancia %s (región %s). "
            "Operación: %s. Comando: %s. Salida: %s" % (
                status, instance_id, region or "?",
                comment or "(sin etiqueta)",
                (first[:200] + "…") if len(first) > 200 else
                (first or "(script vacío)"),
                detail[:600]))


class AwsSsmService:
    """Envía comandos y recupera su salida vía AWS Systems Manager."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def send_command(self, instance_id, commands, region=None, comment=None,
                     execution_timeout=None):
        """Envía un comando de shell a una instancia.

        Args:
            instance_id (str): ID de la instancia EC2.
            commands (str|list[str]): comando(s) a ejecutar.
            region (str, optional): región de la instancia.
            comment (str, optional): comentario para trazabilidad en SSM.
            execution_timeout (int, optional): segundos que el AGENTE deja
                correr el script antes de matarlo. Sin esto vale el default
                de AWS-RunShellScript: 3600 s, aunque PCM espere más.

        Returns:
            str: CommandId de la invocación.
        """
        if isinstance(commands, str):
            commands = [commands]
        parameters = {"commands": commands}
        if execution_timeout:
            parameters["executionTimeout"] = [str(int(execution_timeout))]
        client = self._base.get_client("ssm", region=region)
        response = client.send_command(
            InstanceIds=[instance_id],
            DocumentName=RUN_SHELL_DOCUMENT,
            Parameters=parameters,
            Comment=(comment or "primate_cloud_manager")[:100],
        )
        return response["Command"]["CommandId"]

    def run_document(self, instance_id, document_name, region=None,
                     comment=None, timeout=300, poll_interval=3,
                     output_s3_bucket=None, output_s3_prefix=None,
                     steps=None, _sleep=time.sleep):
        """Ejecuta un documento SSM PROPIO sin parámetros y espera la salida.

        El camino de los ambientes IMPORTADOS (regla dura 2026-10-22): el
        documento pcm-readonly-discovery tiene comandos fijos de consulta,
        así que la credencial pcm-prod-readonly solo puede ejecutar ESO —
        nada de shell arbitrario sobre servidores productivos.
        """
        client = self._base.get_client("ssm", region)
        kwargs = {
            "InstanceIds": [instance_id],
            "DocumentName": document_name,
            "Parameters": {},
            "Comment": (comment or "pcm readonly discovery")[:100],
        }
        # get_command_invocation corta en 24.000 caracteres: con bucket, la
        # salida COMPLETA queda en S3 y se lee con s3:GetObject (lectura).
        if output_s3_bucket:
            kwargs["OutputS3BucketName"] = output_s3_bucket
            if output_s3_prefix:
                kwargs["OutputS3KeyPrefix"] = output_s3_prefix
        response = client.send_command(**kwargs)
        command_id = response["Command"]["CommandId"]
        waited = 0
        while waited < timeout:
            output = self._salida_por_pasos(
                command_id, instance_id, region, steps)
            if output.get("status") in ("Success", "Failed", "Cancelled",
                                        "TimedOut"):
                output = dict(output, command_id=command_id)
                if output_s3_bucket and output.get("status") == "Success":
                    full = self._read_s3_stdout(
                        output_s3_bucket, output_s3_prefix, command_id,
                        instance_id, region)
                    if full:
                        output["stdout"] = full
                return output
            _sleep(poll_interval)
            waited += poll_interval
        return {"command_id": command_id, "status": "TimedOut"}

    def document_exists(self, name, region=None):
        """True/False si el documento SSM existe en la región (lectura:
        ssm:DescribeDocument). None si no se pudo saber (otro error)."""
        client = self._base.get_client("ssm", region=region)
        try:
            client.describe_document(Name=name)
            return True
        except Exception as error:  # noqa: BLE001
            if "InvalidDocument" in str(error):
                return False
            return None

    _TERMINALES = ("Success", "Failed", "Cancelled", "TimedOut")

    def _salida_por_pasos(self, command_id, instance_id, region, steps):
        """Estado y salida del comando; con ``steps``, paso por paso.

        GetCommandInvocation exige PluginName en documentos de varios pasos y
        corta CADA paso en 24.000 caracteres: leerlos por separado multiplica
        lo que entra. El estado es terminal cuando todos los pasos lo son;
        Success sólo si todos fueron Success. Si el documento desplegado es
        uno viejo de un solo paso (el PluginName no existe), se lee entero.
        """
        if not steps:
            return self.get_command_output(command_id, instance_id, region)
        salidas = []
        for paso in steps:
            try:
                salidas.append(self.get_command_output(
                    command_id, instance_id, region, plugin_name=paso))
            except Exception as error:  # noqa: BLE001
                if "InvalidPluginName" in str(error) and not salidas:
                    return self.get_command_output(command_id, instance_id, region)
                raise
        estados = [o.get("status") for o in salidas]
        if not all(e in self._TERMINALES for e in estados):
            estado = "InProgress"
        elif all(e == "Success" for e in estados):
            estado = "Success"
        else:
            estado = next(e for e in estados if e != "Success")
        return {
            "status": estado,
            "stdout": "\n".join(o.get("stdout") or "" for o in salidas),
            "stderr": "\n".join(o.get("stderr") or "" for o in salidas
                                if o.get("stderr")),
            "steps": {paso: {"status": o.get("status"),
                             "chars": len(o.get("stdout") or "")}
                      for paso, o in zip(steps, salidas)},
        }

    def _read_s3_stdout(self, bucket, prefix, command_id, instance_id,
                        region=None):
        """Lee el stdout completo que SSM escribió en S3 (solo lectura). Con
        varios pasos hay un stdout por paso: se juntan en orden."""
        s3 = self._base.get_client("s3", region)
        base = "/".join(p for p in ((prefix or "").strip("/"), command_id,
                                    instance_id) if p)
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=base + "/")
        partes = []
        for obj in sorted(listed.get("Contents") or [], key=lambda o: o["Key"]):
            if obj["Key"].endswith("/stdout"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"]
                partes.append(body.read().decode("utf-8", errors="replace"))
        return "\n".join(partes)

    def describe_instance_information(self, instance_ids, region=None):
        """Estado del agente SSM de un conjunto de instancias (sólo lectura).

        Es la forma barata de saber si un servidor es alcanzable ANTES de
        mandarle un comando: una instancia que no aparece acá no tiene el
        agente registrado (no está instalado, o falta el instance profile con
        ``AmazonSSMManagedInstanceCore``), y un ``send_command`` contra ella
        sólo terminaría en timeout.

        ``DescribeInstanceInformation`` acepta hasta 50 ids por filtro, así que
        se consulta en tandas.

        Args:
            instance_ids (list[str]): ids de EC2.
            region (str, optional): región.

        Returns:
            dict: ``{instance_id: ping_status}`` con ``"Online"``,
                ``"ConnectionLost"`` o ``"Inactive"``. Las instancias que SSM no
                conoce no aparecen en el dict.
        """
        client = self._base.get_client("ssm", region=region)
        ids = [i for i in (instance_ids or []) if i]
        estados = {}
        for inicio in range(0, len(ids), 50):
            tanda = ids[inicio:inicio + 50]
            token = None
            while True:
                kwargs = {"Filters": [{"Key": "InstanceIds", "Values": tanda}]}
                if token:
                    kwargs["NextToken"] = token
                respuesta = client.describe_instance_information(**kwargs)
                for info in respuesta.get("InstanceInformationList", []):
                    estados[info["InstanceId"]] = info.get("PingStatus")
                token = respuesta.get("NextToken")
                if not token:
                    break
        return estados

    def get_command_output(self, command_id, instance_id, region=None,
                           plugin_name=None):
        """Recupera el resultado de un comando ya enviado.

        Args:
            plugin_name (str, optional): el paso, en documentos de varios
                pasos (cada paso trae su propia salida, con su propio tope de
                24.000 caracteres).

        Returns:
            dict: ``{"status", "stdout", "stderr", "response_code", "request_id"}``.
        """
        client = self._base.get_client("ssm", region=region)
        kwargs = {"CommandId": command_id, "InstanceId": instance_id}
        if plugin_name:
            kwargs["PluginName"] = plugin_name
        result = client.get_command_invocation(**kwargs)
        return {
            "status": result.get("Status"),
            "stdout": result.get("StandardOutputContent", ""),
            "stderr": result.get("StandardErrorContent", ""),
            "response_code": result.get("ResponseCode"),
            "request_id": result.get("ResponseMetadata", {}).get("RequestId"),
        }

    def _send_waiting_for_agent(
        self, instance_id, script, region, comment, agent_timeout, agent_poll, _sleep,
        execution_timeout=None,
    ):
        """Envía el comando reintentando hasta que el agente SSM esté online.

        Tras crear una EC2, el agente SSM tarda 1–3 min en registrarse; hasta
        entonces ``send_command`` falla con ``InvalidInstanceId``. Se reintenta
        con backoff hasta ``agent_timeout`` segundos.

        Returns:
            str: el CommandId de la invocación.

        Raises:
            Exception: el último error si el agente no aparece dentro del timeout.
        """
        waited = 0
        extra = {}
        if execution_timeout:
            extra["execution_timeout"] = execution_timeout
        while True:
            try:
                return self.send_command(
                    instance_id, script, region=region, comment=comment,
                    **extra)
            except Exception as error:  # noqa: BLE001 - se evalúa si es transitorio
                transient = any(sig in str(error) for sig in SSM_AGENT_NOT_READY)
                if not transient or waited >= agent_timeout:
                    raise
                _sleep(agent_poll)
                waited += agent_poll

    def run_script(
        self,
        instance_id,
        script,
        region=None,
        comment=None,
        wait=True,
        check=True,
        timeout=300,
        poll_interval=3,
        agent_timeout=180,
        agent_poll=15,
        _sleep=time.sleep,
    ):
        """Envía un comando y, por defecto, espera su finalización (polling).

        Pensado para correr dentro de un ``queue_job`` (la espera no bloquea la UI).
        Antes de enviar, espera a que el agente SSM esté online (la EC2 recién
        creada tarda en registrarse).

        Args:
            instance_id (str): ID de la instancia.
            script (str|list[str]): comando(s) a ejecutar.
            region (str, optional): región.
            comment (str, optional): comentario de trazabilidad.
            wait (bool): si espera el resultado (True) o solo encola (False).
            check (bool): si ``True`` (default), LEVANTA :class:`SsmCommandError`
                cuando el comando termina en estado no-``Success``. El contrato
                seguro: fallar ruidoso salvo que el llamador pida tolerar. Pasar
                ``check=False`` para lecturas que degradan o flujos best-effort
                que evalúan el ``status`` del dict devuelto — y es una DECISIÓN
                explícita, no el default. No aplica con ``wait=False`` (no hay
                estado terminal que chequear).
            timeout (int): segundos máximos de espera del resultado. Si pasa
                de 3600 (el tope por defecto del agente), se le pide al agente
                el mismo tope: si no, AWS mata el script a la hora mientras PCM
                sigue esperando.
            poll_interval (int): segundos entre consultas de estado.
            agent_timeout (int): segundos máximos esperando que el agente SSM aparezca.
            agent_poll (int): segundos entre reintentos de envío.
            _sleep (callable): inyectable para testear sin esperas reales.

        Returns:
            dict: salida de :meth:`get_command_output` más ``command_id``.
                  Si ``wait=False``, solo ``{"command_id": ...}``.

        Raises:
            SsmCommandError: si ``check`` y el comando no terminó en ``Success``.
        """
        command_id = self._send_waiting_for_agent(
            instance_id, script, region, comment, agent_timeout, agent_poll, _sleep,
            execution_timeout=(timeout if timeout > SSM_DEFAULT_EXECUTION_TIMEOUT
                               else None),
        )
        if not wait:
            return {"command_id": command_id, "status": "Pending"}

        waited = 0
        while waited < timeout:
            output = self.get_command_output(command_id, instance_id, region=region)
            if output["status"] in SSM_TERMINAL_STATES:
                output["command_id"] = command_id
                break
            _sleep(poll_interval)
            waited += poll_interval
        else:
            output = {
                "command_id": command_id,
                "status": "TimedOut",
                "stdout": "",
                "stderr": "Tiempo de espera agotado al esperar el resultado "
                          "del comando.",
                "response_code": None,
                "request_id": None,
            }

        if check and output.get("status") != "Success":
            raise SsmCommandError(
                instance_id=instance_id, status=output.get("status"),
                stderr=output.get("stderr"), stdout=output.get("stdout"),
                script=script if isinstance(script, str) else "\n".join(script),
                comment=comment, region=region,
                command_id=output.get("command_id"))
        return output
