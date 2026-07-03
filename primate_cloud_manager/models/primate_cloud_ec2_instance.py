# -*- coding: utf-8 -*-
"""Instancia EC2: inventario (Fase 2) y acciones de operación (Fase 3)."""
import json
import logging
import shlex

from odoo import _, SUPERUSER_ID, fields, models
from odoo.exceptions import UserError

from ..services import aws_ec2, aws_ssm
from ..tools import bus, dates

_logger = logging.getLogger(__name__)

# Estados posibles devueltos por AWS (describe_instances → State.Name).
EC2_STATES = [
    ("pending", "Pendiente"),
    ("running", "En ejecución"),
    ("shutting-down", "Apagando"),
    ("stopping", "Deteniendo"),
    ("stopped", "Detenida"),
    ("terminated", "Terminada"),
]


class PrimateCloudEc2Instance(models.Model):
    """Instancia EC2 importada desde AWS y operable desde Odoo."""

    _name = "primate.cloud.ec2.instance"
    _description = "Instancia EC2"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, help="Tag Name en AWS.")
    # account_id es obligatorio: el inventario importa instancias que todavía no
    # están asociadas a ningún entorno. El entorno se vincula manualmente luego.
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS",
        required=True,
        ondelete="cascade",
        index=True,
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno",
        ondelete="set null",
        index=True,
    )
    aws_instance_id = fields.Char(string="Instance ID", required=True, index=True)
    instance_state = fields.Selection(EC2_STATES, string="Estado", tracking=True)
    # region como Char: el inventario debe aceptar cualquier región sin fallar.
    region = fields.Char(string="Región")
    instance_type = fields.Char(string="Tipo de instancia")
    public_ip = fields.Char(string="IP pública")
    private_ip = fields.Char(string="IP privada")
    os_type = fields.Selection(
        [
            ("ubuntu_22", "Ubuntu 22.04"),
            ("ubuntu_24", "Ubuntu 24.04"),
            ("amazon_linux", "Amazon Linux"),
            ("other", "Otro"),
        ],
        string="Sistema operativo",
    )
    disk_size_gb = fields.Integer(string="Disco raíz (GB)")
    aws_tags = fields.Text(string="Tags AWS", help="Tags de AWS serializados en JSON.")
    aws_created_at = fields.Datetime(string="Creada en AWS", readonly=True)
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True)

    # --- Métricas (cache de la última snapshot, Fase 9) ---
    last_cpu = fields.Float(string="CPU % (última)", readonly=True)
    last_status_check_failed = fields.Float(
        string="Status check (última)", readonly=True,
        help="≥1 = falló algún chequeo de estado de EC2 (señal de error real).",
    )
    last_metric_date = fields.Datetime(string="Métricas al", readonly=True)

    _aws_instance_uniq = models.Constraint(
        "UNIQUE(account_id, aws_instance_id)",
        "Esa instancia EC2 ya existe para la cuenta.",
    )

    # ------------------------------------------------------------------
    # Inventario / sincronización
    # ------------------------------------------------------------------
    @staticmethod
    def _aws_vals(data, now):
        """Construye el dict de campos comunes a partir de datos normalizados."""
        return {
            "name": data.get("name"),
            "instance_state": data.get("instance_state"),
            "region": data.get("region"),
            "instance_type": data.get("instance_type"),
            "public_ip": data.get("public_ip") or False,
            "private_ip": data.get("private_ip") or False,
            "aws_tags": json.dumps(data.get("tags") or {}, ensure_ascii=False),
            "aws_created_at": dates.to_naive_utc(data.get("created_at")),
            "last_sync_date": now,
        }

    def _sync_from_aws(self, account, instances):
        """Crea/actualiza instancias EC2 a partir de datos normalizados de AWS.

        Hace upsert por (cuenta, instance_id). No toca el vínculo con el entorno
        si ya existe (el inventario no pisa asociaciones manuales).

        Args:
            account (recordset): la cuenta AWS de origen.
            instances (list[dict]): salida de ``AwsEc2Service.list_instances``.

        Returns:
            dict: ``{"created": int, "updated": int}``.
        """
        now = fields.Datetime.now()
        created = updated = 0
        for data in instances:
            vals = self._aws_vals(data, now)
            existing = self.search(
                [
                    ("account_id", "=", account.id),
                    ("aws_instance_id", "=", data["aws_instance_id"]),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                vals.update(
                    {
                        "account_id": account.id,
                        "aws_instance_id": data["aws_instance_id"],
                    }
                )
                self.create(vals)
                created += 1
        return {"created": created, "updated": updated}

    def _register_provisioned(self, account, aws_data, environment=None, os_type=None, disk_size_gb=None):
        """Crea el registro de una instancia recién creada en AWS (Fase 4).

        Reutilizado por el wizard de creación de EC2 y por el flujo de
        aprovisionamiento. Hace upsert por (cuenta, instance_id) por si la
        instancia ya fue sincronizada antes de registrarse.

        Args:
            account (recordset): cuenta AWS dueña de la instancia.
            aws_data (dict): instancia normalizada (salida de ``create_instance``).
            environment (recordset, optional): entorno al que se asocia.
            os_type (str, optional): sistema operativo (campo informativo).
            disk_size_gb (int, optional): tamaño del disco raíz solicitado.

        Returns:
            recordset: la instancia creada o actualizada.
        """
        vals = self._aws_vals(aws_data, fields.Datetime.now())
        if environment:
            vals["environment_id"] = environment.id
        if os_type:
            vals["os_type"] = os_type
        if disk_size_gb:
            vals["disk_size_gb"] = disk_size_gb
        if aws_data.get("instance_type"):
            vals["instance_type"] = aws_data["instance_type"]
        existing = self.search(
            [
                ("account_id", "=", account.id),
                ("aws_instance_id", "=", aws_data["aws_instance_id"]),
            ],
            limit=1,
        )
        if existing:
            existing.write(vals)
            return existing
        vals.update(
            {"account_id": account.id, "aws_instance_id": aws_data["aws_instance_id"]}
        )
        return self.create(vals)

    # ------------------------------------------------------------------
    # Helpers de servicios AWS
    # ------------------------------------------------------------------
    def _get_ec2_service(self):
        """Devuelve el adaptador EC2 autenticado con la cuenta de la instancia."""
        self.ensure_one()
        return aws_ec2.AwsEc2Service(self.account_id._get_aws_service())

    def _get_ssm_service(self):
        """Devuelve el adaptador SSM autenticado con la cuenta de la instancia."""
        self.ensure_one()
        return aws_ssm.AwsSsmService(self.account_id._get_aws_service())

    # Fuentes de log (spec §11.1) → comando de lectura. NINGUNO vuelca
    # credenciales ni el odoo.conf: solo se leen archivos/journal de log.
    _LOG_SOURCES = {
        "odoo": "journalctl -u odoo --no-pager",
        "nginx": "tail -n %(lines)s /var/log/nginx/error.log "
                 "/var/log/nginx/access.log 2>/dev/null",
        "postgres": "journalctl -u postgresql --no-pager 2>/dev/null || "
                    "tail -n %(lines)s /var/log/postgresql/*.log 2>/dev/null",
        "os": "journalctl --no-pager",
    }

    def fetch_logs(self, source, lines=200, since=None, until=None, grep=None):
        """Trae logs en vivo por SSM (on-demand, NO se persisten).

        Los comandos solo LEEN archivos/journal de log — nunca el odoo.conf ni
        variables con credenciales. El texto se devuelve para mostrar/descargar,
        no se guarda en BD.

        Args:
            source (str): ``odoo`` / ``nginx`` / ``postgres`` / ``os``.
            lines (int): cantidad de líneas (100/500/1000...).
            since/until (str, optional): rango para journalctl (``YYYY-MM-DD``).
            grep (str, optional): filtro de texto libre.

        Returns:
            dict: ``{"status": str, "text": str}``.
        """
        self.ensure_one()
        if source not in self._LOG_SOURCES:
            raise UserError(_("Fuente de log no soportada: %s.") % source)
        try:
            lines = int(lines)
        except (TypeError, ValueError):
            lines = 200
        base = self._LOG_SOURCES[source] % {"lines": lines}
        # journalctl acepta rango y --lines; los archivos ya traen -n arriba.
        if base.startswith("journalctl"):
            if since:
                base += " --since %s" % shlex.quote(since)
            if until:
                base += " --until %s" % shlex.quote(until)
            base += " -n %d" % lines
        if grep:
            base += " | grep -a -i -- %s" % shlex.quote(grep)
        # Cota dura de salida para no traer megabytes a la UI.
        command = "%s | tail -n %d" % (base, lines)
        output = self._get_ssm_service().run_script(
            self.aws_instance_id, command, region=self.region,
            comment="pcm logs: %s" % source, timeout=120)
        text = (output.get("stdout") or "") or (output.get("stderr") or "")
        return {"status": output.get("status") or "Unknown", "text": text}

    def _log(self, action_type, result="success", error_message=None, aws_request_id=None, name=None):
        """Atajo para registrar en la bitácora sobre esta instancia."""
        return self.env["primate.cloud.operation.log"].log_operation(
            action_type,
            name=name,
            record=self,
            result=result,
            error_message=error_message,
            aws_request_id=aws_request_id,
        )

    # ------------------------------------------------------------------
    # Acciones de ciclo de vida (botón -> job). Toda llamada AWS va async.
    # ------------------------------------------------------------------
    def action_start(self):
        """Arranca la(s) instancia(s). Sin confirmación."""
        return self._enqueue_lifecycle("ec2_start")

    def action_stop(self):
        """Detiene (confirmación vía atributo confirm en la vista)."""
        return self._enqueue_lifecycle("ec2_stop")

    def action_restart(self):
        """Reinicia (reboot in situ)."""
        return self._enqueue_lifecycle("ec2_restart")

    def action_terminate(self):
        """Termina la(s) instancia(s). Operación destructiva: solo admin + wizard.

        Se valida el grupo aquí también (defensa en profundidad), además de la
        restricción de la vista/wizard.
        """
        if self.env.uid != SUPERUSER_ID and not self.env.user.has_group(
            "primate_cloud_manager.group_cloud_admin"
        ):
            raise UserError(_("Solo un Cloud Admin puede terminar instancias."))
        return self._enqueue_lifecycle("ec2_terminate")

    def _enqueue_lifecycle(self, action_type):
        """Patrón botón→job: NO cambia el estado; encola y avisa "por favor espere".

        El estado NO se toca acá: se actualiza recién cuando el job termina y
        consulta el estado real en AWS. Al terminar, el job notifica al navegador
        por el bus (ver job_lifecycle → _notify_user_done) para refrescar la vista
        y mostrar el resultado o el error. Mientras tanto, se muestra un cartel de
        "procesando, por favor espere".
        """
        spec = self._LIFECYCLE[action_type]
        for instance in self:
            instance.with_delay(
                description=_("%(act)s EC2: %(name)s")
                % {"act": spec["label"], "name": instance.name}
            ).job_lifecycle(action_type)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Procesando"),
                "message": _(spec["wait"]),
                "sticky": False,
            },
        }

    def action_execute_command(self):
        """Abre el wizard para ejecutar un comando vía SSM."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Ejecutar comando"),
            "res_model": "primate.cloud.ec2.command.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_instance_id": self.id},
        }

    def action_open_terminate_wizard(self):
        """Abre el wizard de terminación (doble confirmación)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Terminar instancia"),
            "res_model": "primate.cloud.ec2.terminate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_instance_id": self.id},
        }

    def action_create_staging_from_instance(self):
        """Abre el wizard de staging con esta instancia como origen (Bloque 5).

        Preselecciona el entorno, esta instancia y su BD si es única: el flujo
        "staging desde la instancia" del hub.
        """
        self.ensure_one()
        environment = self.environment_id
        if not environment:
            raise UserError(_("La instancia no pertenece a ningún entorno."))
        if environment.state != "active":
            raise UserError(_("Solo se puede crear un staging desde un "
                              "entorno activo."))
        context = {
            "default_origin_environment_id": environment.id,
            "default_origin_instance_id": self.id,
        }
        databases = (environment.database_ids.filtered(
            lambda d: d.ec2_instance_id == self) or environment.database_ids)
        if len(databases) == 1:
            context["default_origin_database_id"] = databases.id
        return {
            "type": "ir.actions.act_window",
            "name": _("Crear staging desde esta instancia"),
            "res_model": "primate.cloud.staging.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": context,
        }

    def action_sync_from_aws(self):
        """Encola la sincronización puntual de esta instancia desde AWS."""
        for instance in self:
            instance.with_delay(
                description=_("Sincronizar EC2: %s") % instance.name
            ).job_sync_from_aws()
        return self._notify(_("Sincronización encolada."))

    def action_refresh_metrics(self):
        """Encola un refresh on-demand de métricas de esta instancia."""
        self.ensure_one()
        self.with_delay(
            description=_("Métricas EC2: %s") % self.name
        ).job_snapshot_metrics()
        return self._notify(_("Actualización de métricas encolada."))

    def job_snapshot_metrics(self):
        """Job: toma una snapshot de métricas de esta instancia (CloudWatch)."""
        self.ensure_one()
        from ..services import aws_cloudwatch
        cw = aws_cloudwatch.AwsCloudWatchService(self.account_id._get_aws_service())
        self._take_metrics_snapshot(cw)
        return True

    def _take_metrics_snapshot(self, cw):
        """Consulta CloudWatch, crea la ``monitor.snapshot`` y refresca el cache.

        Un valor ``None`` de CloudWatch (métrica sin datos) NO se persiste como
        cero: se deja el campo sin escribir. RAM/disco NO se consultan (requieren
        agente CloudWatch; la UI las muestra como "requiere agente", no en cero).
        """
        self.ensure_one()
        if not self.aws_instance_id:
            return self.env["primate.cloud.monitor.snapshot"]
        from datetime import timedelta
        end = fields.Datetime.now()
        start = end - timedelta(hours=1)
        metrics = cw.get_ec2_metrics(self.aws_instance_id, self.region, start, end)
        # Solo se escriben las métricas con dato (None → no se persiste = NULL,
        # no cero). status_check: 0 es un valor real (chequeo OK), se escribe.
        vals = {"ec2_instance_id": self.id,
                "environment_id": self.environment_id.id}
        for key in ("cpu", "network_in", "network_out", "status_check_failed"):
            if metrics.get(key) is not None:
                vals[key] = metrics[key]
        snapshot = self.env["primate.cloud.monitor.snapshot"].sudo().create(vals)
        cache = {"last_metric_date": end}
        if metrics.get("cpu") is not None:
            cache["last_cpu"] = metrics["cpu"]
        if metrics.get("status_check_failed") is not None:
            cache["last_status_check_failed"] = metrics["status_check_failed"]
        self.write(cache)
        return snapshot

    # ------------------------------------------------------------------
    # Jobs (queue_job)
    # ------------------------------------------------------------------
    # Mapeo acción -> {método del servicio, etiqueta, carteles de espera/éxito}.
    _LIFECYCLE = {
        "ec2_start": {
            "method": "start_instance", "label": "Iniciar",
            "wait": "El servidor se está iniciando, por favor espere…",
            "done": "Servidor iniciado.",
        },
        "ec2_stop": {
            "method": "stop_instance", "label": "Detener",
            "wait": "El servidor se está deteniendo, por favor espere…",
            "done": "Servidor detenido.",
        },
        "ec2_restart": {
            "method": "reboot_instance", "label": "Reiniciar",
            "wait": "El servidor se está reiniciando, por favor espere…",
            "done": "Servidor reiniciado.",
        },
        "ec2_terminate": {
            "method": "terminate_instance", "label": "Terminar",
            "wait": "El servidor se está terminando, por favor espere…",
            "done": "Servidor terminado.",
        },
    }
    def job_lifecycle(self, action_type):
        """Job: ejecuta la acción, deja el estado real y avisa al navegador.

        Corre async (rápido: una sola llamada a AWS, sin polling). Recién al
        terminar consulta el estado real en AWS y lo escribe; luego emite un
        evento por el bus para que la vista se refresque y se muestre el resultado
        (o el motivo del error). Nunca se traga el error: queda en la bitácora.
        """
        self.ensure_one()
        spec = self._LIFECYCLE[action_type]
        try:
            service = self._get_ec2_service()
            request_id = getattr(service, spec["method"])(
                self.aws_instance_id, region=self.region
            )
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self._refresh_state_silently()
            self.message_post(
                body=_("Error al %(act)s: %(err)s")
                % {"act": spec["label"].lower(), "err": error}
            )
            self._log(action_type, result="failed", error_message=str(error),
                      name=_("%(act)s EC2: %(name)s") % {"act": spec["label"], "name": self.name})
            self._notify_user_done(action_type, ok=False, detail=str(error))
            return False

        # Reflejar el estado real que reporta AWS tras la acción.
        self._refresh_state_silently()
        self.message_post(body=_("Acción '%s' enviada a AWS correctamente.") % spec["label"])
        self._log(action_type, result="success", aws_request_id=request_id,
                  name=_("%(act)s EC2: %(name)s") % {"act": spec["label"], "name": self.name})
        self._notify_user_done(action_type, ok=True)
        return True

    def _notify_user_done(self, action_type, ok, detail=None):
        """Avisa al usuario que disparó la acción (por bus) que el job terminó.

        El front (ver static/src/js/pcm_notifier.js) muestra el toast y refresca
        la vista. En caso de error, el mensaje incluye el motivo.
        """
        spec = self._LIFECYCLE[action_type]
        if ok:
            message = _(spec["done"])
            title = _("Listo")
        else:
            message = _("Error al %(act)s «%(name)s»: %(err)s") % {
                "act": spec["label"].lower(), "name": self.name, "err": detail or "",
            }
            title = _("Error")
        bus.toast(self.env, message, title=title,
                  ntype="success" if ok else "danger", sticky=not ok, reload=True)

    def _refresh_state_silently(self):
        """Lee el estado real de la instancia en AWS y lo escribe (best-effort).

        Returns:
            bool: True si pudo consultar AWS; False si la consulta falló.
        """
        self.ensure_one()
        try:
            data = self._get_ec2_service().get_instance(
                self.aws_instance_id, region=self.region
            )
        except Exception:  # noqa: BLE001 - best-effort, no debe romper el job
            return False
        # Si AWS ya no la lista, quedó terminada.
        self.instance_state = data["instance_state"] if data else "terminated"
        return True

    def job_sync_from_aws(self):
        """Job: actualiza estado, IPs y tags de esta instancia desde AWS."""
        self.ensure_one()
        try:
            service = self._get_ec2_service()
            data = service.get_instance(self.aws_instance_id, region=self.region)
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Error al sincronizar: %s") % error)
            self._log("sync", result="failed", error_message=str(error))
            return False
        if not data:
            # La instancia ya no existe en AWS: se marca como terminada.
            self.instance_state = "terminated"
            self.message_post(body=_("La instancia ya no existe en AWS (terminada)."))
        else:
            self.write(self._aws_vals(data, fields.Datetime.now()))
        return True

    def job_execute_command(self, command):
        """Job: ejecuta un comando vía SSM y deja la salida en el chatter + log.

        El output se publica en el chatter de la instancia (no en vivo en pantalla):
        toda llamada SSM va por queue_job, así que la espera no bloquea la UI.
        """
        self.ensure_one()
        try:
            service = self._get_ssm_service()
            output = service.run_script(
                self.aws_instance_id,
                command,
                region=self.region,
                comment="pcm: %s" % (self.name or self.aws_instance_id),
            )
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Error al ejecutar comando: %s") % error)
            self._log("ssm_command", result="failed", error_message=str(error),
                      name=_("Comando SSM: %s") % self.name)
            return False

        ok = output.get("status") == "Success"
        body = _(
            "<b>Comando SSM</b> (estado: %(st)s)<br/>"
            "<b>$</b> <code>%(cmd)s</code><br/>"
            "<b>stdout:</b><pre>%(out)s</pre>"
            "%(err)s"
        ) % {
            "st": output.get("status"),
            "cmd": command,
            "out": (output.get("stdout") or "").strip() or "(vacío)",
            "err": (
                _("<b>stderr:</b><pre>%s</pre>") % output["stderr"].strip()
                if output.get("stderr")
                else ""
            ),
        }
        self.message_post(body=body)
        self._log(
            "ssm_command",
            result="success" if ok else "failed",
            error_message=None if ok else (output.get("stderr") or output.get("status")),
            aws_request_id=output.get("command_id"),
            name=_("Comando SSM: %s") % self.name,
        )
        bus.toast(
            self.env,
            _("Comando ejecutado (estado: %s). La salida quedó en el historial.")
            % output.get("status") if ok else
            _("El comando SSM falló: %s") % (output.get("stderr") or output.get("status")),
            title=_("Comando SSM: %s") % self.name,
            ntype="success" if ok else "danger", sticky=not ok, reload=True,
        )
        return ok

    # ------------------------------------------------------------------
    def _notify(self, message):
        """Notificación no bloqueante para los botones de acción."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info", "message": message, "next": {"type": "ir.actions.act_window_close"}},
        }
