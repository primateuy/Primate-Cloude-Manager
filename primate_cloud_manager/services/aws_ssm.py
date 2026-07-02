# -*- coding: utf-8 -*-
"""Adaptador SSM: ejecución remota de comandos en instancias EC2.

Se prefiere SSM sobre SSH (paramiko queda como fallback en fases posteriores).
Requisito asumido: agente SSM en la AMI + instance profile con permisos SSM.
"""
import time

# Estados terminales de una invocación de comando SSM.
SSM_TERMINAL_STATES = {"Success", "Failed", "Cancelled", "TimedOut"}

# Documento estándar de AWS para correr comandos de shell en Linux.
RUN_SHELL_DOCUMENT = "AWS-RunShellScript"

# Señales de que el agente SSM aún no está registrado tras arrancar la EC2.
# SendCommand falla con estos errores hasta que el agente queda "online".
SSM_AGENT_NOT_READY = ("InvalidInstanceId", "not in a valid state")


class AwsSsmService:
    """Envía comandos y recupera su salida vía AWS Systems Manager."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def send_command(self, instance_id, commands, region=None, comment=None):
        """Envía un comando de shell a una instancia.

        Args:
            instance_id (str): ID de la instancia EC2.
            commands (str|list[str]): comando(s) a ejecutar.
            region (str, optional): región de la instancia.
            comment (str, optional): comentario para trazabilidad en SSM.

        Returns:
            str: CommandId de la invocación.
        """
        if isinstance(commands, str):
            commands = [commands]
        client = self._base.get_client("ssm", region=region)
        response = client.send_command(
            InstanceIds=[instance_id],
            DocumentName=RUN_SHELL_DOCUMENT,
            Parameters={"commands": commands},
            Comment=(comment or "primate_cloud_manager")[:100],
        )
        return response["Command"]["CommandId"]

    def get_command_output(self, command_id, instance_id, region=None):
        """Recupera el resultado de un comando ya enviado.

        Returns:
            dict: ``{"status", "stdout", "stderr", "response_code", "request_id"}``.
        """
        client = self._base.get_client("ssm", region=region)
        result = client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        return {
            "status": result.get("Status"),
            "stdout": result.get("StandardOutputContent", ""),
            "stderr": result.get("StandardErrorContent", ""),
            "response_code": result.get("ResponseCode"),
            "request_id": result.get("ResponseMetadata", {}).get("RequestId"),
        }

    def _send_waiting_for_agent(
        self, instance_id, script, region, comment, agent_timeout, agent_poll, _sleep
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
        while True:
            try:
                return self.send_command(
                    instance_id, script, region=region, comment=comment
                )
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
            timeout (int): segundos máximos de espera del resultado.
            poll_interval (int): segundos entre consultas de estado.
            agent_timeout (int): segundos máximos esperando que el agente SSM aparezca.
            agent_poll (int): segundos entre reintentos de envío.
            _sleep (callable): inyectable para testear sin esperas reales.

        Returns:
            dict: salida de :meth:`get_command_output` más ``command_id``.
                  Si ``wait=False``, solo ``{"command_id": ...}``.
        """
        command_id = self._send_waiting_for_agent(
            instance_id, script, region, comment, agent_timeout, agent_poll, _sleep
        )
        if not wait:
            return {"command_id": command_id, "status": "Pending"}

        waited = 0
        while waited < timeout:
            output = self.get_command_output(command_id, instance_id, region=region)
            if output["status"] in SSM_TERMINAL_STATES:
                output["command_id"] = command_id
                return output
            _sleep(poll_interval)
            waited += poll_interval

        return {
            "command_id": command_id,
            "status": "TimedOut",
            "stdout": "",
            "stderr": "Tiempo de espera agotado al esperar el resultado del comando.",
            "response_code": None,
            "request_id": None,
        }
