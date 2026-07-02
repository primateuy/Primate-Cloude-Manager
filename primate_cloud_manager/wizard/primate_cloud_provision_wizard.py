# -*- coding: utf-8 -*-
"""Wizard de aprovisionamiento de un entorno (flujo 7.1, Fase 4).

Captura los parámetros de cómputo (EC2), base de datos (RDS / PostgreSQL local /
ninguna) y DNS, y encola el flujo completo en el entorno. La contraseña maestra
de la base se usa solo para la creación: no se persiste en la base de Odoo.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.primate_cloud_account import AWS_REGIONS
from ..services import aws_ec2
from .primate_cloud_ec2_create_wizard import OS_TYPES


# Campos del wizard que se persisten (cifrados) en el entorno para precargarlos
# al reintentar tras un error. Incluye las contraseñas (van cifradas).
PERSISTED_FIELDS = [
    "region", "domain", "instance_name", "instance_type", "os_type", "image_id",
    "disk_size_gb", "key_name", "security_group_ids", "subnet_id", "instance_profile",
    "db_mode", "db_name", "db_user", "db_password", "pg_version", "rds_identifier",
    "rds_instance_class", "rds_storage_gb", "rds_multi_az", "backup_retention_days",
    "create_dns", "hosted_zone_id", "ttl", "admin_password",
]


class PrimateCloudProvisionWizard(models.TransientModel):
    """Parámetros del aprovisionamiento completo de un entorno."""

    _name = "primate.cloud.provision.wizard"
    _description = "Aprovisionar entorno"

    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno", required=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        related="environment_id.account_id", string="Cuenta AWS", readonly=True
    )
    region = fields.Selection(AWS_REGIONS, string="Región", required=True)
    domain = fields.Char(
        string="Dominio principal", required=True, help="Ej.: forum.primate.cloud"
    )

    # --- Cómputo (EC2) ---
    instance_name = fields.Char(string="Nombre de la instancia")
    instance_type = fields.Char(string="Tipo de instancia", required=True, default="t3.medium")
    os_type = fields.Selection(OS_TYPES, string="Sistema operativo", default="ubuntu_24")
    image_id = fields.Char(
        string="AMI",
        help="ID de la AMI base (ami-...). Si lo dejás vacío, se resuelve "
             "automáticamente el último Ubuntu 24.04 de la región.",
    )
    disk_size_gb = fields.Integer(string="Disco raíz (GB)", default=30)
    key_name = fields.Char(string="Par de claves (SSH)")
    security_group_ids = fields.Char(string="Grupos de seguridad", help="IDs separados por coma.")
    subnet_id = fields.Char(string="Subred")
    instance_profile = fields.Char(
        string="Instance profile", help="Perfil IAM de la instancia (agente SSM)."
    )

    # --- Base de datos ---
    db_mode = fields.Selection(
        [("none", "Ninguna"), ("local_pg", "PostgreSQL local"), ("rds", "Amazon RDS")],
        string="Base de datos", required=True, default="local_pg",
    )
    db_name = fields.Char(string="Nombre de la base")
    db_user = fields.Char(string="Usuario PostgreSQL", default="odoo")
    db_password = fields.Char(
        string="Contraseña PostgreSQL",
        help="Solo se usa para crear la base; no se almacena en Odoo.",
    )
    pg_version = fields.Selection(
        [("13", "13"), ("14", "14"), ("15", "15"), ("16", "16")],
        string="Versión PostgreSQL", default="16",
    )
    rds_identifier = fields.Char(string="Identificador RDS")
    rds_instance_class = fields.Char(string="Clase RDS", default="db.t3.medium")
    rds_storage_gb = fields.Integer(string="Almacenamiento RDS (GB)", default=20)
    rds_multi_az = fields.Boolean(string="Multi-AZ")
    backup_retention_days = fields.Integer(string="Retención de backup (días)", default=7)

    # --- DNS ---
    create_dns = fields.Boolean(string="Crear registro DNS", default=True)
    hosted_zone_id = fields.Char(string="Hosted Zone ID")
    ttl = fields.Integer(string="TTL", default=300)

    # --- Odoo ---
    admin_password = fields.Char(string="Contraseña admin (odoo.conf)")

    @api.model
    def default_get(self, fields_list):
        """Precarga el wizard con la última config guardada del entorno.

        Si un aprovisionamiento previo falló, el usuario reabre el wizard y
        encuentra todo lo que había ingresado (no tiene que re-tipearlo).
        """
        defaults = super().default_get(fields_list)
        env_id = self.env.context.get("default_environment_id")
        if env_id:
            saved = self.env["primate.cloud.environment"].browse(env_id)._load_provision_config()
            for key, value in saved.items():
                if key in self._fields and value not in (None,):
                    defaults[key] = value
        return defaults

    @api.onchange("environment_id")
    def _onchange_environment_id(self):
        """Prefija región, dominio y nombres a partir del entorno."""
        env = self.environment_id
        if not env:
            return
        if env.account_id and not self.region:
            self.region = env.account_id.default_region
        if env.main_url and not self.domain:
            self.domain = env.main_url
        if not self.instance_name:
            self.instance_name = env.main_url or env.name
        if not self.db_name:
            self.db_name = (env.name or "").lower().replace(" ", "_")
        if not self.rds_identifier:
            self.rds_identifier = (
                (env.name or "db").lower().replace(" ", "-").replace("_", "-")
            )

    def _prepare_params(self):
        """Construye el dict de parámetros que consume ``job_provision``."""
        self.ensure_one()
        security_groups = [
            sg.strip() for sg in (self.security_group_ids or "").split(",") if sg.strip()
        ]
        return {
            "region": self.region,
            "domain": self.domain,
            # Cómputo
            "instance_name": self.instance_name or self.domain,
            "instance_type": self.instance_type,
            "os_type": self.os_type,
            "image_id": self.image_id,
            "disk_size_gb": self.disk_size_gb,
            "key_name": self.key_name,
            "security_group_ids": security_groups,
            "subnet_id": self.subnet_id,
            "instance_profile": self.instance_profile,
            # Base de datos
            "db_mode": self.db_mode,
            "db_name": self.db_name,
            "db_user": self.db_user,
            "db_password": self.db_password,
            "pg_version": self.pg_version,
            "rds_identifier": self.rds_identifier,
            "rds_instance_class": self.rds_instance_class,
            "rds_storage_gb": self.rds_storage_gb,
            "rds_multi_az": self.rds_multi_az,
            "backup_retention_days": self.backup_retention_days,
            # DNS
            "create_dns": self.create_dns,
            "hosted_zone_id": self.hosted_zone_id,
            "ttl": self.ttl,
            # Odoo
            "admin_password": self.admin_password,
        }

    @api.onchange("image_id")
    def _onchange_image_id(self):
        """Limpia el AMI ingresado (tolera corchetes/comillas/espacios)."""
        if self.image_id:
            self.image_id = aws_ec2.normalize_ami_id(self.image_id)

    def action_resolve_ami(self):
        """Botón: autocompleta el campo AMI resolviéndolo para la región."""
        self.ensure_one()
        self.image_id = self.account_id.resolve_ubuntu_ami(self.region)
        return False

    def _validate(self):
        """Valida la coherencia de los parámetros antes de encolar."""
        self.ensure_one()
        # El AMI es opcional: vacío => se resuelve para la región al crear la EC2.
        if self.db_mode == "rds":
            if not (self.rds_identifier or "").strip():
                raise UserError(_("RDS: indicá el identificador de la base."))
            if not (self.db_password or "").strip():
                raise UserError(_("RDS: indicá la contraseña maestra de la base."))
        if self.create_dns and not (self.hosted_zone_id or "").strip():
            raise UserError(_("Para crear DNS, indicá el Hosted Zone ID de Route 53."))

    def _raw_values(self):
        """Valores crudos del wizard a persistir (para precargar al reintentar)."""
        self.ensure_one()
        return {name: self[name] for name in PERSISTED_FIELDS}

    def action_provision(self):
        """Valida, guarda lo ingresado y encola el flujo de aprovisionamiento."""
        self.ensure_one()
        self._validate()
        # Se guarda ANTES de encolar: si el job falla, la config queda igual.
        self.environment_id._save_provision_config(self._raw_values())
        self.environment_id._enqueue_provision(self._prepare_params())
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Aprovisionando"),
                "message": _(
                    "Aprovisionamiento encolado. El entorno pasará a Activo al terminar."
                ),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
