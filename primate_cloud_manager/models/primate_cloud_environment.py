# -*- coding: utf-8 -*-
"""Entorno: instalación específica de Odoo (objeto central del módulo).

En Fase 2 es un esqueleto que agrupa la infraestructura sincronizada (EC2,
bases, DNS). En Fase 4 gana el flujo de aprovisionamiento completo
(EC2 → DB → install_odoo.sh por SSM → nginx → SSL → DNS), todo async vía
queue_job. La trazabilidad (Fase 5) y el staging (Fase 7) se agregan luego.
"""
import json
import logging
import shlex
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from ..services import aws_base, aws_ec2, aws_rds, aws_route53, aws_s3, aws_ssm
from ..tools import bus, crypto

_logger = logging.getLogger(__name__)

# Rutas (relativas al addons-path) de las plantillas corridas por SSM.
INSTALL_SCRIPT_PATH = "primate_cloud_manager/data/install_odoo.sh"
NEUTRALIZATION_SQL_PATH = "primate_cloud_manager/data/neutralization.sql"


class PrimateCloudEnvironment(models.Model):
    """Combinación de infraestructura, base de datos y configuración de un Odoo."""

    _name = "primate.cloud.environment"
    _description = "Entorno Cloud"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, tracking=True)
    project_id = fields.Many2one(
        "primate.cloud.project",
        string="Proyecto",
        required=True,
        ondelete="cascade",
    )
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS",
        compute="_compute_account_id",
        store=True,
        readonly=False,
        ondelete="restrict",
        help="Heredada del proyecto; puede sobrescribirse.",
    )
    env_type = fields.Selection(
        [
            ("production", "Producción"),
            ("staging", "Staging"),
            ("testing", "Testing"),
            ("development", "Desarrollo"),
        ],
        string="Tipo",
        required=True,
        default="production",
    )
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("provisioning", "Aprovisionando"),
            ("active", "Activo"),
            ("error", "Error"),
            ("archived", "Archivado"),
        ],
        string="Estado",
        default="draft",
        required=True,
        tracking=True,
    )
    odoo_version = fields.Selection(
        [("17", "17"), ("18", "18"), ("19", "19")],
        string="Versión Odoo",
    )
    odoo_edition = fields.Selection(
        [("community", "Community"), ("enterprise", "Enterprise")],
        string="Edición Odoo",
    )
    main_url = fields.Char(string="URL principal", help="Ej.: forum.primate.cloud")

    ec2_instance_ids = fields.One2many(
        "primate.cloud.ec2.instance", "environment_id", string="Instancias EC2"
    )
    database_ids = fields.One2many(
        "primate.cloud.database", "environment_id", string="Bases de datos"
    )
    dns_record_ids = fields.One2many(
        "primate.cloud.dns.record", "environment_id", string="Registros DNS"
    )
    repository_ids = fields.One2many(
        "primate.cloud.repository", "environment_id", string="Repositorios"
    )
    deployment_ids = fields.One2many(
        "primate.cloud.deployment", "environment_id", string="Despliegues"
    )

    creation_date = fields.Datetime(
        string="Fecha de creación", default=fields.Datetime.now, readonly=True
    )
    active = fields.Boolean(string="Activo", default=True)
    notes = fields.Text(string="Notas")

    # --- Staging (Fase 7) ---
    origin_environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno origen", readonly=True,
        ondelete="set null",
        help="Para staging: entorno del que se clonó (normalmente producción).",
    )
    staging_ids = fields.One2many(
        "primate.cloud.environment", "origin_environment_id", string="Stagings"
    )
    staging_creation_date = fields.Datetime(string="Creación del staging", readonly=True)
    staging_created_by = fields.Many2one(
        "res.users", string="Staging creado por", readonly=True
    )
    staging_origin_backup = fields.Char(
        string="Backup de origen", readonly=True,
        help="Referencia (key S3) del dump usado para crear/refrescar el staging.",
    )
    staging_neutralization_log = fields.Text(
        string="Log de neutralización", readonly=True
    )

    # Última configuración de los wizards (cifrada), para pre-cargarlos al
    # reintentar tras un error sin re-tipear todo. provision_config: del wizard
    # de aprovisionar (sobre este entorno). staging_config: del wizard de crear
    # staging (este entorno como ORIGEN).
    provision_config_encrypted = fields.Char(
        string="Config de aprovisionamiento (cifrada)", copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )
    staging_config_encrypted = fields.Char(
        string="Config de staging (cifrada)", copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )

    def _save_provision_config(self, values, field="provision_config_encrypted"):
        """Guarda (cifrada) la última config de un wizard en el campo indicado."""
        self.ensure_one()
        key = self.env["primate.cloud.account"]._get_encryption_key()
        self.sudo()[field] = crypto.encrypt(key, json.dumps(values))

    def _load_provision_config(self, field="provision_config_encrypted"):
        """Devuelve la última config guardada del campo indicado (o {} si no hay)."""
        self.ensure_one()
        blob = self.sudo()[field]
        if not blob:
            return {}
        key = self.env["primate.cloud.account"]._get_encryption_key()
        try:
            return json.loads(crypto.decrypt(key, blob) or "{}")
        except Exception:  # noqa: BLE001 - config corrupta: se ignora
            return {}

    @api.depends("project_id")
    def _compute_account_id(self):
        """Propone la cuenta del proyecto, sin pisar una elección manual previa."""
        for environment in self:
            if not environment.account_id and environment.project_id:
                environment.account_id = environment.project_id.account_id

    # ------------------------------------------------------------------
    # Aprovisionamiento (Fase 4): botón -> wizard -> job
    # ------------------------------------------------------------------
    def action_provision(self):
        """Abre el wizard de aprovisionamiento (captura EC2 + DB + DNS).

        El botón no toca AWS: solo presenta el wizard. La confirmación del wizard
        encola el flujo completo (ver :meth:`job_provision`).
        """
        self.ensure_one()
        if self.state not in ("draft", "error"):
            raise UserError(
                _("Solo se puede aprovisionar un entorno en estado Borrador o Error.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Aprovisionar entorno"),
            "res_model": "primate.cloud.provision.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_environment_id": self.id},
        }

    def _enqueue_provision(self, params):
        """Valida, deja el entorno en 'provisioning' y encola el flujo completo.

        Args:
            params (dict): parámetros de aprovisionamiento del wizard.
        """
        self.ensure_one()
        if not self.account_id:
            raise UserError(_("El entorno necesita una cuenta AWS para aprovisionar."))
        self.state = "provisioning"
        # Token de idempotencia: viaja en los args del job (queue_job los persiste),
        # así un requeue tras reiniciar el server NO crea una EC2 duplicada.
        params = dict(params, client_token="pcm-%s" % uuid.uuid4().hex)
        self.with_delay(
            description=_("Aprovisionar entorno: %s") % self.name
        ).job_provision(params)

    # ------------------------------------------------------------------
    # Helpers de servicios / script
    # ------------------------------------------------------------------
    def _log(self, action_type, result="success", error_message=None,
             aws_request_id=None, name=None, record=None):
        """Atajo para registrar en la bitácora (por defecto sobre el entorno)."""
        return self.env["primate.cloud.operation.log"].log_operation(
            action_type, name=name, record=record if record is not None else self,
            result=result, error_message=error_message, aws_request_id=aws_request_id,
        )

    @api.model
    def _render_install_script(self, tokens):
        """Lee la plantilla install_odoo.sh y reemplaza los tokens %%...%%.

        Args:
            tokens (dict): ``{"ODOO_VERSION": "19", ...}``.

        Returns:
            str: el script listo para correr por SSM.
        """
        with file_open(INSTALL_SCRIPT_PATH, "r") as script_file:
            script = script_file.read()
        for key, value in tokens.items():
            script = script.replace("%%%%%s%%%%" % key, str(value or ""))
        return script

    # ------------------------------------------------------------------
    # Job de aprovisionamiento (flujo 7.1)
    # ------------------------------------------------------------------
    def job_provision(self, params):
        """Job: ejecuta el flujo completo de aprovisionamiento del entorno.

        Pasos: crear EC2 → [crear RDS o PostgreSQL local] → install_odoo.sh por
        SSM (nginx + SSL) → crear registro DNS → activar el entorno. Cada paso se
        audita en la bitácora. Ante cualquier error deja el entorno en 'error',
        registra el motivo y no se lo traga.

        Args:
            params (dict): parámetros del wizard (cómputo, base de datos, DNS).

        Returns:
            bool: True si el flujo terminó bien; False si falló.
        """
        self.ensure_one()
        account = self.account_id
        bus.provision_start(self.env, self, title=_("Aprovisionando: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or account.default_region
            domain = params.get("domain") or self.main_url or self.name

            # 1. Crear EC2 ------------------------------------------------------
            bus.provision_step(self.env, self, _("Creando instancia EC2…"))
            instance = self._provision_ec2(base, account, params, region, domain)

            # 2. Base de datos (RDS / PostgreSQL local / ninguna) --------------
            bus.provision_step(self.env, self, _("Configurando la base de datos…"))
            db_host = self._provision_database(base, account, instance, params, region)

            # 3. install_odoo.sh por SSM (instala Odoo, nginx, SSL) -----------
            bus.provision_step(self.env, self,
                               _("Instalando Odoo por SSM (puede tardar varios minutos)…"))
            self._provision_run_install(base, instance, params, region, db_host, domain)

            # 4. Registro DNS (A -> IP pública) -------------------------------
            bus.provision_step(self.env, self, _("Configurando DNS…"))
            self._provision_dns(base, account, instance, params, domain)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            self.message_post(body=_("Aprovisionamiento fallido: %s") % error)
            self._log("provision", name=_("Aprovisionar: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Aprovisionamiento fallido: %s") % error)
            return False

        # 5. Activar el entorno -----------------------------------------------
        self.write({"state": "active", "main_url": domain})
        self.message_post(body=_("Entorno aprovisionado y activo en %s.") % domain)
        self._log("provision", name=_("Aprovisionar: %s") % self.name, result="success")
        bus.provision_done(self.env, self, ok=True,
                           message=_("Entorno activo en %s.") % domain)
        return True

    def _provision_ec2(self, base, account, params, region, domain):
        """Crea la EC2 del entorno y registra el recurso. Devuelve la instancia."""
        ec2 = aws_ec2.AwsEc2Service(base)
        tags = aws_base.build_resource_tags(
            client=self.project_id.name,
            environment=self.name,
            extra={"Name": params.get("instance_name") or domain},
        )
        # AMI: si el usuario lo dejó vacío, se resuelve para la región; si lo
        # cargó, se limpia (tolera corchetes/comillas/espacios pegados).
        image_id = aws_ec2.normalize_ami_id(params.get("image_id")) \
            or account.resolve_ubuntu_ami(region)
        data = ec2.create_instance(
            image_id=image_id,
            instance_type=params["instance_type"],
            tags=tags,
            region=region,
            disk_size_gb=params.get("disk_size_gb") or None,
            key_name=params.get("key_name") or None,
            security_group_ids=params.get("security_group_ids") or None,
            subnet_id=params.get("subnet_id") or None,
            instance_profile=params.get("instance_profile") or None,
            client_token=params.get("client_token") or None,
        )
        instance = self.env["primate.cloud.ec2.instance"]._register_provisioned(
            account, data, environment=self,
            os_type=params.get("os_type"), disk_size_gb=params.get("disk_size_gb"),
        )
        self.message_post(body=_("EC2 creada: %s (%s).")
                          % (instance.name, instance.aws_instance_id))
        self._log("ec2_create", name=_("Crear EC2: %s") % instance.name,
                  result="success", record=instance)
        return instance

    def _provision_database(self, base, account, instance, params, region):
        """Crea la base (RDS o local) y devuelve el host de conexión para Odoo."""
        db_mode = params.get("db_mode") or "none"
        if db_mode == "rds":
            rds = aws_rds.AwsRdsService(base)
            tags = aws_base.build_resource_tags(
                client=self.project_id.name, environment=self.name
            )
            data = rds.create_instance(
                identifier=params["rds_identifier"],
                instance_class=params["rds_instance_class"],
                storage_gb=params.get("rds_storage_gb") or 20,
                master_username=params.get("db_user") or "odoo",
                master_password=params["db_password"],
                tags=tags,
                engine_version=self._pg_engine_version(params.get("pg_version")),
                multi_az=params.get("rds_multi_az") or False,
                backup_retention_days=params.get("backup_retention_days") or 7,
                region=region,
            )
            database = self.env["primate.cloud.database"].create({
                "name": params["rds_identifier"],
                "account_id": account.id,
                "environment_id": self.id,
                "db_type": "rds",
                "rds_identifier": params["rds_identifier"],
                "rds_endpoint": data.get("endpoint") or False,
                "rds_instance_class": params["rds_instance_class"],
                "rds_storage_gb": params.get("rds_storage_gb") or 0,
                "rds_multi_az": params.get("rds_multi_az") or False,
                "backup_retention_days": params.get("backup_retention_days") or 0,
                "pg_version": params.get("pg_version") or False,
                "state": "available",
            })
            self.message_post(body=_("RDS creada: %s.") % database.name)
            self._log("rds_create", name=_("Crear RDS: %s") % database.name,
                      result="success", record=database)
            return data.get("endpoint") or "localhost"

        if db_mode == "local_pg":
            self.env["primate.cloud.database"].create({
                "name": params.get("db_name") or self.name,
                "account_id": account.id,
                "environment_id": self.id,
                "db_type": "local_pg",
                "ec2_instance_id": instance.id,
                "pg_version": params.get("pg_version") or False,
                "state": "available",
            })
            return "localhost"

        # db_mode == "none": no se crea base; install_odoo.sh asume localhost.
        return "localhost"

    def _provision_run_install(self, base, instance, params, region, db_host, domain):
        """Renderiza y corre install_odoo.sh por SSM; deja la salida en el chatter."""
        ssm = aws_ssm.AwsSsmService(base)
        tokens = {
            "ODOO_VERSION": self.odoo_version or "19",
            "ODOO_EDITION": self.odoo_edition or "community",
            "DB_HOST": db_host,
            "DB_PORT": "5432",
            "DB_NAME": params.get("db_name") or self.name,
            "DB_USER": params.get("db_user") or "odoo",
            "DB_PASSWORD": params.get("db_password") or "",
            "DB_LOCAL": "1" if params.get("db_mode") == "local_pg" else "0",
            "DOMAIN": domain,
            "ADMIN_PASSWORD": params.get("admin_password") or "",
        }
        script = self._render_install_script(tokens)
        output = ssm.run_script(
            instance.aws_instance_id, script, region=region,
            comment="pcm provision: %s" % self.name,
            timeout=params.get("install_timeout") or 900,
        )
        ok = output.get("status") == "Success"
        self.message_post(body=_("Instalación Odoo (SSM) — estado: %s.") % output.get("status"))
        self._log("ssm_command", name=_("Instalar Odoo: %s") % self.name,
                  result="success" if ok else "failed",
                  error_message=None if ok else (output.get("stderr") or output.get("status")),
                  aws_request_id=output.get("command_id"), record=instance)
        if not ok:
            raise UserError(
                _("La instalación por SSM no terminó con éxito (estado: %s).")
                % output.get("status")
            )

    def _provision_dns(self, base, account, instance, params, domain):
        """Crea el registro DNS A apuntando a la IP pública (si se pidió)."""
        if not params.get("create_dns"):
            return
        if not instance.public_ip:
            self.message_post(body=_("Sin IP pública: se omite la creación del registro DNS."))
            return
        route53 = aws_route53.AwsRoute53Service(base)
        change_id = route53.create_record(
            hosted_zone_id=params["hosted_zone_id"],
            name=domain,
            record_type="A",
            value=instance.public_ip,
            ttl=params.get("ttl") or 300,
            comment="pcm provision: %s" % self.name,
        )
        record = self.env["primate.cloud.dns.record"].create({
            "name": domain,
            "account_id": account.id,
            "environment_id": self.id,
            "hosted_zone_id": params["hosted_zone_id"],
            "record_type": "A",
            "record_value": instance.public_ip,
            "ttl": params.get("ttl") or 300,
            "state": "active",
        })
        self.message_post(body=_("Registro DNS creado: %s -> %s.")
                          % (domain, instance.public_ip))
        self._log("dns_create", name=_("Crear DNS: %s") % domain,
                  result="success", aws_request_id=change_id, record=record)

    @staticmethod
    def _pg_engine_version(pg_version):
        """Mapea la versión mayor de PostgreSQL al EngineVersion de RDS, o None."""
        return pg_version or None

    def job_resume_provision(self, params):
        """Job: reintenta la instalación sobre la EC2 YA creada (recuperación).

        Útil cuando el aprovisionamiento creó la instancia pero falló en un paso
        posterior (p. ej. el agente SSM aún no estaba online). No crea recursos
        nuevos: reusa la instancia y la base existentes, corre el install y activa.
        """
        self.ensure_one()
        instance = self.ec2_instance_ids[:1]
        if not instance:
            raise UserError(_("El entorno no tiene una instancia EC2 para reanudar."))
        account = self.account_id
        self.state = "provisioning"
        bus.provision_start(self.env, self, title=_("Reanudando: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or instance.region or account.default_region
            domain = params.get("domain") or self.main_url or self.name
            db_host = "localhost" if params.get("db_mode") == "local_pg" else (
                params.get("db_host") or "localhost")
            bus.provision_step(self.env, self,
                               _("Instalando Odoo por SSM (puede tardar varios minutos)…"))
            self._provision_run_install(base, instance, params, region, db_host, domain)
            bus.provision_step(self.env, self, _("Configurando DNS…"))
            self._provision_dns(base, account, instance, params, domain)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            self.message_post(body=_("Reintento de aprovisionamiento fallido: %s") % error)
            self._log("provision", name=_("Reanudar: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Reintento fallido: %s") % error)
            return False
        self.write({"state": "active", "main_url": domain})
        self.message_post(body=_("Entorno aprovisionado (reanudado) y activo en %s.") % domain)
        self._log("provision", name=_("Reanudar: %s") % self.name, result="success")
        bus.provision_done(self.env, self, ok=True,
                           message=_("Entorno activo en %s.") % domain)
        return True

    # ==================================================================
    # Staging (Fase 7): botón -> wizard -> flujo de 12 pasos
    # ==================================================================
    def action_create_staging(self):
        """Abre el wizard de creación de staging a partir de este entorno."""
        self.ensure_one()
        if self.state != "active":
            raise UserError(_("Solo se puede crear un staging desde un entorno activo."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Crear staging"),
            "res_model": "primate.cloud.staging.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_origin_environment_id": self.id},
        }

    def _enqueue_staging(self, params):
        """Crea el entorno staging (en 'provisioning') y encola su construcción.

        ``self`` es el entorno ORIGEN. Devuelve el entorno staging creado.
        """
        self.ensure_one()
        if not self.account_id:
            raise UserError(_("El entorno origen necesita una cuenta AWS."))
        if not self.ec2_instance_ids[:1]:
            raise UserError(_("El entorno origen no tiene una instancia EC2 (para el dump)."))
        staging = self.create({
            "name": params["name"],
            "project_id": self.project_id.id,
            "account_id": params.get("account_id") or self.account_id.id,
            "env_type": "staging",
            "state": "provisioning",
            "odoo_version": self.odoo_version,
            "odoo_edition": self.odoo_edition,
            "main_url": params.get("domain"),
            "origin_environment_id": self.id,
        })
        # Token de idempotencia (ver _enqueue_provision): evita EC2 duplicadas
        # si el job de staging se re-ejecuta tras reiniciar el server.
        params = dict(params, client_token="pcm-%s" % uuid.uuid4().hex)
        staging.with_delay(
            description=_("Crear staging: %s") % staging.name
        ).job_create_staging(params)
        return staging

    def action_refresh_staging(self):
        """Encola el refresco de este staging desde su entorno origen."""
        self.ensure_one()
        if self.env_type != "staging":
            raise UserError(_("Refrescar solo aplica a entornos de tipo staging."))
        if not self.origin_environment_id:
            raise UserError(_("Este staging no tiene entorno origen registrado."))
        self.with_delay(
            description=_("Refrescar staging: %s") % self.name
        ).job_refresh_staging()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Refresco de staging encolado."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    # ------------------------------------------------------------------
    # Jobs de staging
    # ------------------------------------------------------------------
    def job_create_staging(self, params):
        """Job: flujo de 12 pasos para construir el staging (spec 7.2).

        ``self`` es el entorno staging. Reusa los helpers de aprovisionamiento
        (EC2, base vacía, instalación, DNS) y agrega los pasos propios de staging:
        copiar la base de origen, neutralizarla y clonar los repos al mismo commit.
        """
        self.ensure_one()
        origin = self.origin_environment_id
        account = self.account_id
        bus.provision_start(self.env, self, title=_("Creando staging: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or account.default_region
            domain = params.get("domain") or self.main_url or self.name
            staging_url = "https://%s" % domain

            bus.provision_step(self.env, self, _("Creando instancia EC2…"))
            instance = self._provision_ec2(base, account, params, region, domain)
            bus.provision_step(self.env, self, _("Configurando la base de datos…"))
            db_host = self._provision_database(base, account, instance, params, region)
            bus.provision_step(self.env, self, _("Instalando Odoo por SSM…"))
            self._provision_run_install(base, instance, params, region, db_host, domain)
            bus.provision_step(self.env, self, _("Copiando la base desde el origen…"))
            dump_key = self._staging_copy_database(origin, instance, params, region)
            bus.provision_step(self.env, self, _("Neutralizando la base…"))
            self._staging_neutralize(instance, params, region, staging_url)
            bus.provision_step(self.env, self, _("Clonando repositorios…"))
            self._staging_clone_repos(account, origin, instance, params, region)
            bus.provision_step(self.env, self, _("Configurando DNS…"))
            self._provision_dns(base, account, instance, params, domain)
            bus.provision_step(self.env, self, _("Reiniciando servicios…"))
            self._staging_restart(instance, region)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            self.message_post(body=_("Creación de staging fallida: %s") % error)
            self._log("staging_create", name=_("Crear staging: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Creación de staging fallida: %s") % error)
            return False

        self.write({
            "state": "active", "main_url": domain,
            "staging_creation_date": fields.Datetime.now(),
            "staging_created_by": self.env.uid,
            "staging_origin_backup": dump_key,
        })
        self.message_post(body=_("Staging creado y activo en %s.") % domain)
        self._log("staging_create", name=_("Crear staging: %s") % self.name, result="success")
        bus.provision_done(self.env, self, ok=True,
                           message=_("Staging activo en %s.") % domain)
        return True

    def job_refresh_staging(self):
        """Job: refresca la base del staging desde el origen y la re-neutraliza."""
        self.ensure_one()
        origin = self.origin_environment_id
        instance = self.ec2_instance_ids[:1]
        if not instance:
            raise UserError(_("El staging no tiene una instancia EC2 asociada."))
        params = {
            "db_name": self.database_ids[:1].name or self.name,
            "db_user": "odoo",
            "transfer_bucket": self.staging_origin_backup and
                               self.staging_origin_backup.split("/")[0] or "pcm-staging",
            "db_host": "localhost",
        }
        bus.provision_start(self.env, self, title=_("Refrescando staging: %s") % self.name)
        try:
            region = self.account_id.default_region
            staging_url = "https://%s" % (self.main_url or self.name)
            bus.provision_step(self.env, self, _("Copiando la base desde el origen…"))
            dump_key = self._staging_copy_database(origin, instance, params, region)
            bus.provision_step(self.env, self, _("Neutralizando la base…"))
            self._staging_neutralize(instance, params, region, staging_url)
            bus.provision_step(self.env, self, _("Reiniciando servicios…"))
            self._staging_restart(instance, region)
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Refresco de staging fallido: %s") % error)
            self._log("staging_create", name=_("Refrescar staging: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Refresco fallido: %s") % error)
            return False
        self.write({"staging_origin_backup": dump_key,
                    "staging_creation_date": fields.Datetime.now()})
        self.message_post(body=_("Staging refrescado desde %s.") % origin.display_name)
        self._log("staging_create", name=_("Refrescar staging: %s") % self.name)
        bus.provision_done(self.env, self, ok=True,
                           message=_("Staging refrescado desde %s.") % origin.display_name)
        return True

    # ------------------------------------------------------------------
    # Pasos específicos de staging
    # ------------------------------------------------------------------
    def _staging_copy_database(self, origin, dest_instance, params, region):
        """Copia la base de origen al staging vía pg_dump → S3 → pg_restore.

        Devuelve la key del dump en S3 (referencia de trazabilidad).
        """
        if not origin:
            raise UserError(_("El staging no tiene entorno origen para copiar la base."))
        origin_instance = origin.ec2_instance_ids[:1]
        if not origin_instance:
            raise UserError(_("El entorno origen no tiene una instancia EC2."))
        bucket = params.get("transfer_bucket")
        if not bucket:
            raise UserError(_("Indicá el bucket S3 de transferencia para el dump."))

        # Asegurar que el bucket exista (idempotente).
        s3 = aws_s3.AwsS3Service(dest_instance.account_id._get_aws_service())
        s3.ensure_bucket(bucket, region=region)

        origin_db = origin.database_ids[:1].name
        if not origin_db:
            raise UserError(_("El entorno origen no tiene una base de datos asociada."))
        staging_db = params.get("db_name") or self.name
        db_user = params.get("db_user") or "odoo"
        dump_key = "pcm-staging/%s-%s.dump" % (self.id, staging_db)

        # 1. Dump en el origen + subida a S3.
        dump_out = origin_instance._get_ssm_service().run_script(
            origin_instance.aws_instance_id,
            self._build_dump_script(origin_db, bucket, dump_key),
            region=origin_instance.region, comment="pcm staging dump: %s" % self.name,
            timeout=1800,
        )
        if dump_out.get("status") != "Success":
            raise UserError(_("Falló el pg_dump del origen: %s")
                            % (dump_out.get("stderr") or dump_out.get("status")))

        # 2. Descarga desde S3 + restore en el destino.
        restore_out = dest_instance._get_ssm_service().run_script(
            dest_instance.aws_instance_id,
            self._build_restore_script(staging_db, db_user, bucket, dump_key),
            region=region, comment="pcm staging restore: %s" % self.name, timeout=1800,
        )
        if restore_out.get("status") != "Success":
            raise UserError(_("Falló el pg_restore en el staging: %s")
                            % (restore_out.get("stderr") or restore_out.get("status")))

        self.message_post(body=_("Base copiada desde %s (dump %s).")
                          % (origin.display_name, dump_key))
        return dump_key

    def _staging_neutralize(self, instance, params, region, staging_url):
        """Corre el SQL de neutralización en la base del staging (vía SSM)."""
        staging_db = params.get("db_name") or self.name
        sql = self._render_neutralization_sql(staging_url)
        output = instance._get_ssm_service().run_script(
            instance.aws_instance_id,
            self._build_neutralize_script(staging_db, sql),
            region=region, comment="pcm staging neutralize: %s" % self.name,
        )
        log_text = "%s\n%s" % (output.get("stdout") or "", output.get("stderr") or "")
        self.staging_neutralization_log = log_text.strip()
        if output.get("status") != "Success":
            raise UserError(_("Falló la neutralización: %s")
                            % (output.get("stderr") or output.get("status")))
        self.message_post(body=_("Base neutralizada para staging."))

    def _staging_clone_repos(self, account, origin, dest_instance, params, region):
        """Clona los repos del origen al mismo commit y los registra en el staging."""
        if not origin:
            return
        Repo = self.env["primate.cloud.repository"]
        ssm = dest_instance._get_ssm_service()
        for source in origin.repository_ids:
            if not source.github_url or not source.local_path:
                continue
            ref = source.current_commit or source.configured_branch or ""
            output = ssm.run_script(
                dest_instance.aws_instance_id,
                self._build_clone_script(source.github_url, source.local_path, ref),
                region=region, comment="pcm staging clone: %s" % source.name, timeout=900,
            )
            ok = output.get("status") == "Success"
            Repo.create({
                "name": source.name,
                "environment_id": self.id,
                "repo_type": source.repo_type,
                "github_url": source.github_url,
                "organization": source.organization,
                "configured_branch": source.configured_branch,
                "local_path": source.local_path,
                "current_commit": source.current_commit if ok else False,
            })
        self.message_post(body=_("Repositorios clonados desde el origen."))

    def _staging_restart(self, instance, region):
        """Reinicia los servicios (odoo + nginx) en el staging vía SSM."""
        instance._get_ssm_service().run_script(
            instance.aws_instance_id, self._build_restart_script(),
            region=region, comment="pcm staging restart: %s" % self.name,
        )

    @api.model
    def _render_neutralization_sql(self, staging_url):
        """Lee neutralization.sql y reemplaza el token %%STAGING_URL%%."""
        with file_open(NEUTRALIZATION_SQL_PATH, "r") as sql_file:
            sql = sql_file.read()
        return sql.replace("%%STAGING_URL%%", staging_url or "")

    # ------------------------------------------------------------------
    # Constructores de scripts SSM (puros, testeables por contenido)
    # ------------------------------------------------------------------
    @staticmethod
    def _dump_filename(key):
        """Nombre de archivo temporal a partir de la key S3."""
        return shlex.quote("/tmp/%s" % key.split("/")[-1])

    def _build_dump_script(self, origin_db, bucket, key):
        """pg_dump en el origen + subida a S3."""
        fname = self._dump_filename(key)
        return "\n".join([
            "set -e",
            "sudo -u postgres pg_dump -Fc -d %s -f %s" % (shlex.quote(origin_db), fname),
            "aws s3 cp %s s3://%s/%s" % (fname, shlex.quote(bucket), shlex.quote(key)),
            "rm -f %s" % fname,
        ])

    def _build_restore_script(self, staging_db, db_user, bucket, key):
        """Descarga desde S3 + recreación de la base + pg_restore en el destino."""
        fname = self._dump_filename(key)
        db = shlex.quote(staging_db)
        return "\n".join([
            "set -e",
            "aws s3 cp s3://%s/%s %s" % (shlex.quote(bucket), shlex.quote(key), fname),
            "sudo -u postgres dropdb --if-exists %s" % db,
            "sudo -u postgres createdb -O %s %s" % (shlex.quote(db_user), db),
            "sudo -u postgres pg_restore -d %s %s || true" % (db, fname),
            "rm -f %s" % fname,
        ])

    @staticmethod
    def _build_neutralize_script(staging_db, sql):
        """psql -f con el SQL de neutralización (heredoc, sin expansión de shell)."""
        return "sudo -u postgres psql -d %s <<'PCMSQL'\n%s\nPCMSQL" % (
            shlex.quote(staging_db), sql,
        )

    @staticmethod
    def _build_clone_script(url, path, ref):
        """git clone del repo al destino y checkout al commit/rama indicado."""
        quoted_path = shlex.quote(path)
        lines = [
            "set -e",
            "sudo rm -rf %s" % quoted_path,
            "sudo -u odoo git clone %s %s" % (shlex.quote(url), quoted_path),
        ]
        if ref:
            lines.append("sudo -u odoo git -C %s checkout %s"
                         % (quoted_path, shlex.quote(ref)))
        return "\n".join(lines)

    @staticmethod
    def _build_restart_script():
        """Reinicio de servicios del staging."""
        return "set -e\nsudo systemctl restart odoo\nsudo systemctl restart nginx || true"
