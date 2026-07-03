# -*- coding: utf-8 -*-
"""Cuenta AWS administrada por el módulo."""
import logging
import os
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import (
    aws_base, aws_cost_explorer, aws_ec2, aws_rds, aws_route53,
)
from ..tools import bus, crypto

_logger = logging.getLogger(__name__)

# No re-consultar Cost Explorer si el último pull fue hace menos de esto (el
# dato de CE tiene retardo de horas: pegarle cada minuto paga de más sin cambio).
COST_PULL_FRESHNESS_HOURS = 6
# Tolerancia (en la moneda) al verificar que suma de atribuciones = total CE.
COST_RECONCILE_TOLERANCE = 0.01

# Parámetro de sistema donde se guarda la clave de cifrado si no se define por
# variable de entorno. Ver _get_encryption_key().
FERNET_KEY_PARAM = "primate_cloud_manager.fernet_key"
FERNET_KEY_ENV = "PRIMATE_CLOUD_FERNET_KEY"

# Máscara mostrada en lugar del secreto: nunca se devuelve el valor en claro.
SECRET_MASK = "********"

# Regiones AWS más usadas por Primate. Ampliable según necesidad.
AWS_REGIONS = [
    ("us-east-1", "US East (N. Virginia) — us-east-1"),
    ("us-east-2", "US East (Ohio) — us-east-2"),
    ("us-west-2", "US West (Oregon) — us-west-2"),
    ("sa-east-1", "South America (São Paulo) — sa-east-1"),
    ("eu-west-1", "Europe (Ireland) — eu-west-1"),
    ("eu-central-1", "Europe (Frankfurt) — eu-central-1"),
]


class PrimateCloudAccount(models.Model):
    """Cuenta AWS con credenciales IAM cifradas y estado de conexión.

    Una instalación de Odoo puede administrar varias cuentas. Las credenciales
    se guardan **cifradas** (Fernet) y solo son visibles para el grupo
    ``group_cloud_admin``; el secreto, además, nunca se muestra en claro. La
    sesión boto3 se construye en memoria y no se persiste.
    """

    _name = "primate.cloud.account"
    _description = "Cuenta AWS"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, tracking=True)
    aws_account_id = fields.Char(
        string="AWS Account ID",
        copy=False,
        tracking=True,
        help="ID numérico de 12 dígitos de la cuenta AWS. Se completa al validar la conexión.",
    )
    default_region = fields.Selection(
        AWS_REGIONS,
        string="Región por defecto",
        required=True,
        default="us-east-1",
    )
    auth_method = fields.Selection(
        [
            ("access_key", "Access Key IAM"),
            ("assume_role", "Asumir rol IAM (cross-account)"),
        ],
        string="Método de autenticación",
        required=True,
        default="access_key",
        help=(
            "access_key: claves IAM de larga vida (default v1).\n"
            "assume_role: rol IAM cruzado en la cuenta destino (recomendado para "
            "cuentas de cliente; no guarda secretos persistentes)."
        ),
    )

    # --- Credenciales: columnas cifradas (en BD) ---
    iam_access_key_id_encrypted = fields.Char(
        string="Access Key ID (cifrado)",
        copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )
    iam_secret_access_key_encrypted = fields.Char(
        string="Secret Access Key (cifrado)",
        copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )

    # --- Credenciales: campos de UI (no almacenados, derivados de los cifrados) ---
    iam_access_key_id = fields.Char(
        string="Access Key ID",
        compute="_compute_credentials",
        inverse="_inverse_access_key_id",
        groups="primate_cloud_manager.group_cloud_admin",
    )
    # El enmascarado se hace en la vista (password="True") y en el compute, que
    # nunca devuelve el secreto en claro (ver SECRET_MASK).
    iam_secret_access_key = fields.Char(
        string="Secret Access Key",
        compute="_compute_credentials",
        inverse="_inverse_secret_access_key",
        groups="primate_cloud_manager.group_cloud_admin",
    )

    # --- assume_role ---
    role_arn = fields.Char(
        string="Role ARN",
        groups="primate_cloud_manager.group_cloud_admin",
        help="ARN del rol IAM a asumir en la cuenta destino (método assume_role).",
    )
    external_id = fields.Char(
        string="External ID",
        groups="primate_cloud_manager.group_cloud_admin",
        help="External ID exigido por la política de confianza del rol (assume_role).",
    )

    connection_state = fields.Selection(
        [("draft", "Borrador"), ("connected", "Conectada"), ("error", "Error")],
        string="Estado de conexión",
        default="draft",
        required=True,
        tracking=True,
        copy=False,
    )
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True, copy=False)
    cost_pulled_at = fields.Datetime(
        string="Costos consultados el", readonly=True, copy=False,
        help="Fecha del último pull de Cost Explorer (leyenda 'datos al').",
    )
    notes = fields.Text(string="Notas")

    _aws_account_id_unique = models.Constraint(
        "UNIQUE(aws_account_id)",
        "Ya existe una cuenta con ese AWS Account ID.",
    )

    # ------------------------------------------------------------------
    # Cifrado de credenciales
    # ------------------------------------------------------------------
    @api.model
    def _get_encryption_key(self):
        """Devuelve la clave Fernet para cifrar/descifrar credenciales.

        Prioridad: variable de entorno ``PRIMATE_CLOUD_FERNET_KEY`` (más segura,
        no vive en la BD) y, como fallback, un parámetro de sistema generado una
        sola vez de forma perezosa.

        Returns:
            bytes: clave Fernet.
        """
        # Preferimos la clave por variable de entorno: no vive en la BD junto al
        # texto cifrado, lo que mejora la postura de seguridad.
        env_key = os.environ.get(FERNET_KEY_ENV)
        if env_key:
            return env_key.encode()
        params = self.env["ir.config_parameter"].sudo()
        key = params.get_param(FERNET_KEY_PARAM)
        if not key:
            key = crypto.generate_key().decode()
            params.set_param(FERNET_KEY_PARAM, key)
            _logger.info("Generada clave de cifrado de credenciales para primate_cloud_manager.")
        return key.encode()

    @api.depends("iam_access_key_id_encrypted", "iam_secret_access_key_encrypted")
    def _compute_credentials(self):
        """Descifra el Access Key ID para mostrarlo; enmascara el secreto."""
        key = self._get_encryption_key()
        for account in self:
            account.iam_access_key_id = crypto.decrypt(
                key, account.iam_access_key_id_encrypted
            ) or False
            account.iam_secret_access_key = (
                SECRET_MASK if account.iam_secret_access_key_encrypted else False
            )

    def _inverse_access_key_id(self):
        """Cifra y almacena el Access Key ID ingresado."""
        key = self._get_encryption_key()
        for account in self:
            account.iam_access_key_id_encrypted = crypto.encrypt(key, account.iam_access_key_id)

    def _inverse_secret_access_key(self):
        """Cifra y almacena el secreto, ignorando la máscara (sin cambios reales)."""
        key = self._get_encryption_key()
        for account in self:
            value = account.iam_secret_access_key
            if value and value != SECRET_MASK:
                account.iam_secret_access_key_encrypted = crypto.encrypt(key, value)

    def _get_decrypted_credentials(self):
        """Devuelve las credenciales descifradas para construir la sesión AWS.

        Lee las columnas cifradas con ``sudo`` (los campos están restringidos por
        ``groups``) y descifra. Nunca se loguea el resultado.

        Returns:
            dict: ``{"access_key_id": str|bool, "secret_access_key": str|bool}``.
        """
        self.ensure_one()
        account = self.sudo()
        key = self._get_encryption_key()
        return {
            "access_key_id": crypto.decrypt(key, account.iam_access_key_id_encrypted),
            "secret_access_key": crypto.decrypt(key, account.iam_secret_access_key_encrypted),
        }

    # ------------------------------------------------------------------
    # Sesión AWS
    # ------------------------------------------------------------------
    def _get_aws_service(self):
        """Construye el adaptador :class:`AwsBaseService` para esta cuenta.

        La sesión vive en memoria y no se persiste. Soporta los dos métodos de
        autenticación (D3) según ``auth_method``.

        Returns:
            aws_base.AwsBaseService: servicio base autenticado.
        """
        self.ensure_one()
        account = self.sudo()
        creds = self._get_decrypted_credentials()
        if account.auth_method == "assume_role":
            if not account.role_arn:
                raise UserError(_("Falta el Role ARN para el método 'Asumir rol IAM'."))
            return aws_base.AwsBaseService(
                access_key_id=creds["access_key_id"] or None,
                secret_access_key=creds["secret_access_key"] or None,
                region=account.default_region,
                role_arn=account.role_arn,
                external_id=account.external_id,
            )
        if not creds["access_key_id"] or not creds["secret_access_key"]:
            raise UserError(_("Faltan las credenciales IAM (Access Key ID y Secret)."))
        return aws_base.AwsBaseService(
            access_key_id=creds["access_key_id"],
            secret_access_key=creds["secret_access_key"],
            region=account.default_region,
        )

    def resolve_ubuntu_ami(self, region=None):
        """Resuelve el AMI de Ubuntu 24.04 para la región (o la de la cuenta).

        Reutiliza el servicio EC2 (que cae de SSM a DescribeImages). Devuelve el
        id limpio o levanta ``UserError`` con un mensaje claro si no encuentra.
        """
        self.ensure_one()
        region = region or self.default_region
        try:
            ec2 = aws_ec2.AwsEc2Service(self._get_aws_service())
            ami = ec2.resolve_ubuntu_ami(region=region)
        except Exception as error:  # noqa: BLE001 - se normaliza a UserError
            raise UserError(
                _("Error al resolver el AMI en %(r)s: %(e)s") % {"r": region, "e": error}
            )
        if not ami:
            raise UserError(
                _("No se encontró ningún AMI de Ubuntu 24.04 en la región %s.") % region
            )
        return ami

    # ------------------------------------------------------------------
    # Validación de conexión (botón -> job)
    # ------------------------------------------------------------------
    def action_validate_connection(self):
        """Encola la validación de conexión (toda llamada AWS va en queue_job).

        El botón solo valida lo mínimo y delega el trabajo al job, que actualiza
        el estado y deja traza en la bitácora.
        """
        for account in self:
            account.with_delay(
                description=_("Validar conexión: %s") % account.name
            ).job_validate_connection()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _("Validación de conexión encolada. El estado se actualizará al terminar."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def job_validate_connection(self):
        """Job: valida credenciales contra AWS y actualiza estado + bitácora.

        Nunca se traga errores: ante una falla deja ``connection_state = error``,
        registra el mensaje y asienta la operación como fallida.
        """
        self.ensure_one()
        Log = self.env["primate.cloud.operation.log"]
        try:
            service = self._get_aws_service()
            result = service.test_connection()
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.connection_state = "error"
            self.message_post(body=_("Error al validar conexión: %s") % error)
            Log.log_operation(
                "connection_test",
                name=_("Validar conexión: %s") % self.name,
                record=self,
                result="failed",
                error_message=str(error),
            )
            bus.toast(self.env, _("Error al validar «%(n)s»: %(e)s")
                      % {"n": self.name, "e": error}, title=_("Conexión AWS"),
                      ntype="danger", sticky=True, reload=True)
            return False

        if result["success"]:
            vals = {"connection_state": "connected"}
            if result.get("account_id"):
                vals["aws_account_id"] = result["account_id"]
            self.write(vals)
            self.message_post(body=_("Conexión validada correctamente (cuenta %s).") % result.get("account_id"))
            Log.log_operation(
                "connection_test",
                name=_("Validar conexión: %s") % self.name,
                record=self,
                result="success",
            )
            bus.toast(self.env, _("Conexión validada (cuenta %s).") % result.get("account_id"),
                      title=_("Conexión AWS"), ntype="success", reload=True)
        else:
            self.connection_state = "error"
            self.message_post(body=_("Conexión fallida: %s") % result["error"])
            Log.log_operation(
                "connection_test",
                name=_("Validar conexión: %s") % self.name,
                record=self,
                result="failed",
                error_message=result["error"],
            )
            bus.toast(self.env, _("Conexión fallida: %s") % result["error"],
                      title=_("Conexión AWS"), ntype="danger", sticky=True, reload=True)
        return result["success"]

    # ------------------------------------------------------------------
    # Sincronización de recursos (inventario solo lectura)
    # ------------------------------------------------------------------
    def action_sync_resources(self):
        """Encola la sincronización de recursos AWS (EC2, RDS, DNS)."""
        for account in self:
            account.with_delay(
                description=_("Sincronizar recursos: %s") % account.name
            ).job_sync_resources()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _("Sincronización encolada. El inventario se actualizará al terminar."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    # ------------------------------------------------------------------
    # Costos: pull de Cost Explorer + atribución por pcm_ref (Fase 9, Bloque 2)
    # ------------------------------------------------------------------
    @api.model
    def _cron_pull_costs(self):
        """Cron diario: encola el pull de costos de cada cuenta conectada."""
        for account in self.search([("connection_state", "=", "connected")]):
            account.with_delay(
                description=_("Costos: %s") % account.name
            ).job_pull_costs()

    def action_pull_costs(self):
        """Botón: encola el pull de costos, respetando la guarda de frescura."""
        self.ensure_one()
        self.with_delay(
            description=_("Costos: %s") % self.name
        ).job_pull_costs()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Actualización de costos encolada."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_pull_costs(self, force=False):
        """Job: consulta Cost Explorer y persiste ``cost.entry`` (upsert).

        Guarda de frescura: si el último pull fue hace < COST_PULL_FRESHNESS_HOURS
        y no se fuerza, no vuelve a pegarle a CE (el dato tiene retardo). Cada
        pull re-consulta el mes en curso (que cambia día a día) y ACTUALIZA las
        filas por (cuenta, período, ref, servicio) — no duplica.
        """
        self.ensure_one()
        Entry = self.env["primate.cloud.cost.entry"]
        Log = self.env["primate.cloud.operation.log"]
        if not force and self._cost_pull_is_fresh():
            _logger.info("Costos de %s aún frescos; se omite el pull.", self.name)
            return {"skipped": "fresh"}
        try:
            service = aws_cost_explorer.AwsCostExplorerService(
                self._get_aws_service())
            now = fields.Datetime.now()
            today = fields.Date.context_today(self)
            month_start = today.replace(day=1)
            # Mes en curso [inicio de mes, hoy+1) para incluir el día actual.
            current = service.get_cost_and_usage(
                start=fields.Date.to_string(month_start),
                end=fields.Date.to_string(today + timedelta(days=1)),
                granularity="MONTHLY", group_by=self._cost_group_by())
            self._persist_cost_result(current, month_start,
                                      today + timedelta(days=1), "monthly", now)
            self._reconcile_costs(current, month_start, "monthly")
            # Mes anterior (comparativa).
            prev_end = month_start
            prev_start = (month_start - timedelta(days=1)).replace(day=1)
            previous = service.get_cost_and_usage(
                start=fields.Date.to_string(prev_start),
                end=fields.Date.to_string(prev_end),
                granularity="MONTHLY", group_by=self._cost_group_by())
            self._persist_cost_result(previous, prev_start, prev_end,
                                      "monthly", now)
            # Proyección a fin de mes (una fila is_forecast, no agrupada).
            forecast = service.get_cost_forecast(
                start=fields.Date.to_string(today + timedelta(days=1)),
                end=fields.Date.to_string(self._month_end(month_start)))
            self._persist_forecast(forecast, month_start,
                                   self._month_end(month_start), now)
        except Exception as error:  # noqa: BLE001 - se audita y no se traga
            self.message_post(body=_("Pull de costos fallido: %s") % error)
            Log.log_operation("cost_pull", name=_("Costos: %s") % self.name,
                              record=self, result="failed", error_message=str(error))
            return False
        self._store_cost_cache(current, previous, forecast, now)
        Log.log_operation("cost_pull", name=_("Costos: %s") % self.name,
                          record=self, result="success")
        return {"ok": True}

    def _cost_pull_is_fresh(self):
        """True si ya hay costos de esta cuenta consultados hace poco."""
        self.ensure_one()
        last = self.env["primate.cloud.cost.entry"].search(
            [("account_id", "=", self.id)], order="pulled_at desc", limit=1)
        if not last.pulled_at:
            return False
        age = fields.Datetime.now() - last.pulled_at
        return age.total_seconds() < COST_PULL_FRESHNESS_HOURS * 3600

    @staticmethod
    def _cost_group_by():
        """GroupBy estable: por tag de entorno + servicio (máx 2 en CE)."""
        return [
            {"Type": "TAG", "Key": aws_base.ENVIRONMENT_ID_TAG},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ]

    @staticmethod
    def _month_end(month_start):
        """Primer día del mes siguiente (fin exclusive del mes en curso)."""
        if month_start.month == 12:
            return date(month_start.year + 1, 1, 1)
        return date(month_start.year, month_start.month + 1, 1)

    def _resolve_attribution(self, tag_value):
        """pcm_ref (valor del tag) → (environment_id, environment_name,
        client_ref, client_name, partner_id), robusto ante entornos borrados.

        - tag con valor y entorno existe → todo resuelto (por pcm_ref EXACTO).
        - tag con valor pero entorno borrado → id/partner False, name "Entorno
          eliminado", ref conservado.
        - sin valor → "Sin atribuir".
        """
        if not tag_value:
            return (False, _("Sin atribuir"), False, _("Sin atribuir"), False)
        env = self.env["primate.cloud.environment"].search(
            [("pcm_ref", "=", tag_value)], limit=1)
        if not env:
            return (False, _("Entorno eliminado"), False,
                    _("Cliente eliminado"), False)
        partner = env.project_id.partner_id
        # Materializa el ref del cliente (lazy), como en el provisioning: así
        # el cost.entry guarda la clave estable del partner.
        client_ref = partner._ensure_pcm_ref() if partner else False
        client_name = partner.name or _("Cliente eliminado") if partner else False
        return (env.id, env.name, client_ref, client_name,
                partner.id if partner else False)

    def _persist_cost_result(self, result, period_start, period_end,
                             granularity, now):
        """Upsert de las filas cost.entry a partir del resultado de CE.

        Identidad = (cuenta, período, granularidad, is_forecast, ref, servicio).
        Re-pull del mismo período ACTUALIZA la fila (no inserta otra).
        """
        Entry = self.env["primate.cloud.cost.entry"]
        for group in result.get("groups", []):
            keys = group.get("keys", [])
            # GroupBy = [TAG env_id, SERVICE]. El tag viene como
            # "primate:environment_id$<valor>" (vacío si el recurso no lo tiene).
            tag_raw = keys[0] if keys else ""
            tag_value = tag_raw.split("$", 1)[1] if "$" in tag_raw else ""
            service_name = keys[1] if len(keys) > 1 else ""
            env_id, env_name, client_ref, client_name, partner_id = \
                self._resolve_attribution(tag_value)
            vals = {
                "environment_name": env_name, "environment_id": env_id,
                "client_ref": client_ref, "client_name": client_name,
                "partner_id": partner_id,
                "amount": group.get("amount") or 0.0,
                "currency": group.get("currency") or "USD",
                "pulled_at": now,
            }
            existing = Entry.search([
                ("account_id", "=", self.id),
                ("period_start", "=", period_start),
                ("granularity", "=", granularity),
                ("is_forecast", "=", False),
                ("environment_ref", "=", tag_value or False),
                ("service", "=", service_name or False),
            ], limit=1)
            if existing:
                existing.write(vals)
            else:
                Entry.create(dict(vals, account_id=self.id,
                                  period_start=period_start,
                                  period_end=period_end,
                                  granularity=granularity, is_forecast=False,
                                  environment_ref=tag_value or False,
                                  service=service_name or False))

    def _reconcile_costs(self, result, period_start, granularity):
        """Verifica que la suma de las atribuciones persistidas = total de CE.

        El bucket "Sin atribuir" existe justamente para que SIEMPRE cuadre. Si
        no cuadra (más allá de la tolerancia de redondeo), se avisa: un reporte
        de costos que no coincide con la factura de AWS no sirve.
        """
        self.ensure_one()
        persisted = sum(self.env["primate.cloud.cost.entry"].search([
            ("account_id", "=", self.id),
            ("period_start", "=", period_start),
            ("granularity", "=", granularity),
            ("is_forecast", "=", False),
        ]).mapped("amount"))
        total = result.get("total") or 0.0
        if abs(persisted - total) > COST_RECONCILE_TOLERANCE:
            self.message_post(body=_(
                "⚠️ Cuadre de costos: suma de atribuciones %(sum).4f ≠ total "
                "CE %(total).4f (dif %(diff).4f). Revisar.",
                sum=persisted, total=total, diff=persisted - total))
            _logger.warning("Costos de %s no cuadran: %.4f vs %.4f",
                            self.name, persisted, total)
            return False
        return True

    def _persist_forecast(self, forecast, period_start, period_end, now):
        """Upsert de la fila de proyección (is_forecast=True, no agrupada)."""
        Entry = self.env["primate.cloud.cost.entry"]
        vals = {
            "amount": forecast.get("amount") or 0.0,
            "currency": forecast.get("currency") or "USD",
            "environment_name": _("Proyección fin de mes"), "pulled_at": now,
        }
        existing = Entry.search([
            ("account_id", "=", self.id),
            ("period_start", "=", period_start),
            ("granularity", "=", "monthly"),
            ("is_forecast", "=", True),
        ], limit=1)
        if existing:
            existing.write(vals)
        else:
            Entry.create(dict(vals, account_id=self.id,
                              period_start=period_start, period_end=period_end,
                              granularity="monthly", is_forecast=True))

    def _store_cost_cache(self, current, previous, forecast, now):
        """Deja el timestamp del último pull a nivel cuenta (para la guarda de
        frescura y la leyenda 'datos al'). El cache por entorno es del Bloque 4."""
        self.sudo().cost_pulled_at = now

    # ------------------------------------------------------------------
    # Re-tagging de recursos existentes (Fase 9, Opción A, Bloque 1c)
    # ------------------------------------------------------------------
    def action_retag_preview(self):
        """Encola una PREVISUALIZACIÓN del re-etiquetado (no aplica nada).

        "Mirar antes que tocar": el plan (qué se taggearía/corregiría) queda en
        el chatter de la cuenta al terminar, sin escribir ningún tag en AWS.
        """
        self.ensure_one()
        self.with_delay(
            description=_("Previsualizar re-etiquetado: %s") % self.name
        ).job_retag_resources(dry_run=True)
        return self._retag_notification(preview=True)

    def action_retag_resources(self):
        """Encola el re-etiquetado REAL de los recursos gestionados."""
        self.ensure_one()
        self.with_delay(
            description=_("Re-etiquetar recursos: %s") % self.name
        ).job_retag_resources(dry_run=False)
        return self._retag_notification(preview=False)

    def _retag_notification(self, preview):
        msg = (_("Previsualización de re-etiquetado encolada; el plan quedará "
                 "en el chatter.") if preview else
               _("Re-etiquetado encolado; el resultado quedará en el chatter."))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info", "message": msg,
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_retag_resources(self, dry_run=False):
        """Job: re-taggea las EC2 gestionadas con los tags estables (Opción A).

        Idempotente y por VÍNCULO del modelo (no por el tag viejo): recorre las
        instancias de la cuenta con ``environment_id`` y compara el
        ``primate:environment_id`` REAL en AWS contra el ``pcm_ref`` del entorno:

        - **already_ok**: ya tiene el valor correcto → no se toca (por eso una
          2ª corrida da tagged=0, señal de que no hay pendientes).
        - **tagged**: le falta el tag → se aplica.
        - **fixed**: tiene un valor DISTINTO al del modelo (alguien tocó tags a
          mano) → el modelo es la fuente de verdad, se **sobrescribe**, pero se
          cuenta aparte y se deja en el log (un conflicto es señal de algo raro).
        - **skipped**: no está viva/encontrada en AWS. **failed**: CreateTags o
          el describe falló (no aborta el lote).

        En ``dry_run`` calcula el plan y lo reporta SIN aplicar ningún tag.
        """
        self.ensure_one()
        Instance = self.env["primate.cloud.ec2.instance"]
        Log = self.env["primate.cloud.operation.log"]
        managed = Instance.search([
            ("account_id", "=", self.id),
            ("environment_id", "!=", False),
            ("aws_instance_id", "!=", False),
            ("instance_state", "not in", ("terminated", "shutting-down")),
        ])
        counts = {"already_ok": 0, "tagged": 0, "fixed": 0,
                  "skipped": 0, "failed": 0}
        lines = []
        # Agrupar por región (el cliente EC2 y CreateTags son por región).
        by_region = {}
        for inst in managed:
            region = inst.region or self.default_region
            by_region[region] = by_region.get(region, Instance) | inst
        try:
            ec2 = aws_ec2.AwsEc2Service(self._get_aws_service())
        except Exception as error:  # noqa: BLE001 - sin servicio: se audita
            self.message_post(body=_("Re-etiquetado fallido: %s") % error)
            Log.log_operation("retag", name=_("Re-etiquetar: %s") % self.name,
                              record=self, result="failed", error_message=str(error))
            return counts
        for region, insts in by_region.items():
            try:
                live = {d["aws_instance_id"]: d
                        for d in ec2.list_instances(region=region)}
            except Exception as error:  # noqa: BLE001 - región no describible
                counts["failed"] += len(insts)
                lines.append(_("✖ región %s: no se pudo describir (%s)")
                             % (region, error))
                continue
            for inst in insts:
                self._retag_one(ec2, inst, live.get(inst.aws_instance_id),
                                region, dry_run, counts, lines)
        result = "success" if not counts["failed"] else "partial"
        header = (_("Previsualización de re-etiquetado") if dry_run
                  else _("Re-etiquetado"))
        summary = _("%(header)s: %(counts)s", header=header, counts=counts)
        self.message_post(body=summary + ("\n" + "\n".join(lines[:60])
                                          if lines else ""))
        Log.log_operation("retag", name=_("%s: %s") % (header, self.name),
                          record=self, result=result)
        return counts

    def _retag_one(self, ec2, inst, data, region, dry_run, counts, lines):
        """Clasifica y (si no es dry_run) aplica el re-tag de UNA instancia."""
        if not data:
            counts["skipped"] += 1
            lines.append(_("↷ %s: no encontrada en AWS") % inst.name)
            return
        env_ref, client_ref = inst.environment_id._cost_attribution_refs()
        current = (data.get("tags") or {}).get(aws_base.ENVIRONMENT_ID_TAG)
        if current == env_ref:
            counts["already_ok"] += 1
            return
        action = "fixed" if current else "tagged"
        if current:
            lines.append(_("⚠ %(name)s: conflicto '%(cur)s' → '%(ref)s' "
                           "(se sobrescribe)", name=inst.name, cur=current,
                           ref=env_ref))
        else:
            lines.append(_("+ %(name)s: %(ref)s", name=inst.name, ref=env_ref))
        if dry_run:
            counts[action] += 1
            return
        tags = aws_base.build_resource_tags(
            client=inst.environment_id.project_id.name,
            environment=inst.environment_id.name,
            client_ref=client_ref, environment_ref=env_ref,
        )
        resource_ids = [inst.aws_instance_id] + (data.get("volume_ids") or [])
        try:
            ec2.create_tags(resource_ids, tags, region=region)
            counts[action] += 1
        except Exception as error:  # noqa: BLE001 - un recurso no aborta el lote
            counts["failed"] += 1
            lines.append(_("✖ %(name)s: CreateTags falló (%(err)s)",
                           name=inst.name, err=error))

    def job_create_ec2(self, vals):
        """Job: crea una instancia EC2 en AWS y la registra en el inventario.

        Usado por el wizard de creación de EC2 (spec 5.3). Toda llamada AWS va en
        queue_job. Etiqueta el recurso con los tags obligatorios (D4). Nunca se
        traga el error: lo deja en la bitácora con resultado fallido.

        Args:
            vals (dict): parámetros del wizard (name, region, instance_type,
                image_id, os_type, disk_size_gb, environment_id, key_name,
                security_group_ids, subnet_id, instance_profile).

        Returns:
            recordset|bool: la instancia creada, o False si falló.
        """
        self.ensure_one()
        Log = self.env["primate.cloud.operation.log"]
        environment = self.env["primate.cloud.environment"].browse(
            vals.get("environment_id")
        ).exists()
        name = vals.get("name")
        try:
            region = vals.get("region") or self.default_region
            ec2 = aws_ec2.AwsEc2Service(self._get_aws_service())
            # EC2 suelta (sin entorno PCM) = sin refs estables → "sin atribuir"
            # limpio; con entorno, los refs salen de él (materializa el partner).
            env_ref, client_ref = (
                environment._cost_attribution_refs() if environment else (False, False)
            )
            tags = aws_base.build_resource_tags(
                client=(environment.project_id.name if environment else self.name),
                environment=(environment.name if environment else name),
                client_ref=client_ref, environment_ref=env_ref,
                extra={"Name": name},
            )
            # AMI: si viene vacío se resuelve para la región; si viene, se limpia.
            image_id = aws_ec2.normalize_ami_id(vals.get("image_id")) \
                or self.resolve_ubuntu_ami(region)
            data = ec2.create_instance(
                image_id=image_id,
                instance_type=vals["instance_type"],
                tags=tags,
                region=region,
                disk_size_gb=vals.get("disk_size_gb") or None,
                key_name=vals.get("key_name") or None,
                security_group_ids=vals.get("security_group_ids") or None,
                subnet_id=vals.get("subnet_id") or None,
                instance_profile=vals.get("instance_profile") or None,
                client_token=vals.get("client_token") or None,
            )
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.message_post(body=_("Error al crear instancia EC2 «%(n)s»: %(e)s")
                              % {"n": name, "e": error})
            Log.log_operation(
                "ec2_create", name=_("Crear EC2: %s") % name, record=self,
                result="failed", error_message=str(error),
            )
            bus.toast(self.env, _("No se pudo crear la EC2 «%(n)s»: %(e)s")
                      % {"n": name, "e": error}, title=_("Crear EC2"),
                      ntype="danger", sticky=True, reload=True)
            return False

        instance = self.env["primate.cloud.ec2.instance"]._register_provisioned(
            self, data, environment=environment,
            os_type=vals.get("os_type"), disk_size_gb=vals.get("disk_size_gb"),
        )
        self.message_post(body=_("Instancia EC2 «%(n)s» creada: %(id)s")
                          % {"n": instance.name, "id": instance.aws_instance_id})
        Log.log_operation(
            "ec2_create", name=_("Crear EC2: %s") % instance.name, record=instance,
            result="success",
        )
        bus.toast(self.env, _("Instancia EC2 «%(n)s» creada (%(id)s).")
                  % {"n": instance.name, "id": instance.aws_instance_id},
                  title=_("Crear EC2"), ntype="success", reload=True)
        return instance

    @api.model
    def _cron_sync_all_accounts(self):
        """Cron: encola la sincronización de cada cuenta conectada."""
        accounts = self.search([("connection_state", "=", "connected")])
        for account in accounts:
            account.with_delay(
                description=_("Sincronización diaria: %s") % account.name
            ).job_sync_resources()
        _logger.info("Sincronización diaria encolada para %s cuentas.", len(accounts))

    def job_sync_resources(self):
        """Job: importa EC2, RDS y DNS desde AWS hacia el inventario.

        Trae lo que ya está corriendo (solo lectura). Captura errores: los deja
        en la bitácora con resultado fallido sin tragárselos, y actualiza la
        fecha de sincronización solo si terminó bien.
        """
        self.ensure_one()
        Log = self.env["primate.cloud.operation.log"]
        try:
            base = self._get_aws_service()
            region = self.default_region
            ec2 = aws_ec2.AwsEc2Service(base)
            rds = aws_rds.AwsRdsService(base)
            route53 = aws_route53.AwsRoute53Service(base)

            ec2_res = self.env["primate.cloud.ec2.instance"]._sync_from_aws(
                self, ec2.list_instances(region=region)
            )
            rds_res = self.env["primate.cloud.database"]._sync_rds_from_aws(
                self, rds.list_instances(region=region)
            )
            dns_created = dns_updated = 0
            for zone in route53.list_zones():
                dns_res = self.env["primate.cloud.dns.record"]._sync_from_aws(
                    self, route53.list_records(zone["id"])
                )
                dns_created += dns_res["created"]
                dns_updated += dns_res["updated"]
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.message_post(body=_("Error al sincronizar recursos: %s") % error)
            Log.log_operation(
                "sync",
                name=_("Sincronizar recursos: %s") % self.name,
                record=self,
                result="failed",
                error_message=str(error),
            )
            bus.toast(self.env, _("Sincronización fallida: %s") % error,
                      title=_("Sincronizar %s") % self.name, ntype="danger",
                      sticky=True, reload=True)
            return False

        self.last_sync_date = fields.Datetime.now()
        summary = _(
            "Inventario sincronizado — EC2: %(ec)s nuevas / %(eu)s act. · "
            "RDS: %(rc)s nuevas / %(ru)s act. · DNS: %(dc)s nuevos / %(du)s act."
        ) % {
            "ec": ec2_res["created"],
            "eu": ec2_res["updated"],
            "rc": rds_res["created"],
            "ru": rds_res["updated"],
            "dc": dns_created,
            "du": dns_updated,
        }
        self.message_post(body=summary)
        Log.log_operation(
            "sync",
            name=_("Sincronizar recursos: %s") % self.name,
            record=self,
            result="success",
        )
        bus.toast(self.env, summary, title=_("Sincronizar %s") % self.name,
                  ntype="success", reload=True)
        return True
