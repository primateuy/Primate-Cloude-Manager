# -*- coding: utf-8 -*-
"""Wizard de creación de staging a partir de un entorno (flujo 7.2, Fase 7).

Captura los parámetros del staging destino (cómputo, base, DNS y bucket de
transferencia) y encola el flujo de 12 pasos en un nuevo entorno staging.
La contraseña de la base se usa solo para crear/restaurar: no se persiste.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.primate_cloud_account import AWS_REGIONS
from ..services import aws_ec2
from .primate_cloud_ec2_create_wizard import OS_TYPES

# Campos del wizard que se persisten (cifrados) en el entorno ORIGEN para
# precargarlos al reintentar tras un error. Incluye contraseñas (van cifradas).
STAGING_PERSISTED_FIELDS = [
    "name", "domain", "region", "instance_name", "instance_type", "os_type",
    "image_id", "disk_size_gb", "key_name", "security_group_ids", "subnet_id",
    "instance_profile", "db_mode", "db_name", "db_user", "db_password", "pg_version",
    "rds_identifier", "rds_instance_class", "rds_storage_gb", "rds_multi_az",
    "backup_retention_days", "transfer_bucket", "create_dns", "hosted_zone_id",
    "ttl", "admin_password", "use_last_backup",
]

# Campo del entorno origen donde se guarda la config cifrada del staging.
STAGING_CONFIG_FIELD = "staging_config_encrypted"


class PrimateCloudStagingCreateWizard(models.TransientModel):
    """Parámetros para construir un entorno staging desde uno de origen."""

    _name = "primate.cloud.staging.create.wizard"
    _description = "Crear staging"

    origin_environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno origen", required=True,
        ondelete="cascade",
    )
    account_id = fields.Many2one(
        related="origin_environment_id.account_id", string="Cuenta AWS", readonly=True
    )
    # Origen EXPLÍCITO (Fase 8, Bloque 5): de qué instancia y BD sale el
    # staging. Con un solo candidato se preseleccionan (el caso común no
    # cambia); con varios, el usuario elige — reemplaza el [:1] de Fase 7.
    origin_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Instancia de origen",
        help="EC2 desde la que se ejecuta el dump (con red a la BD de origen).",
    )
    origin_database_id = fields.Many2one(
        "primate.cloud.database", string="Base de datos de origen",
        help="Base que se copia al staging (local o RDS por endpoint).",
    )
    use_last_backup = fields.Boolean(
        string="Usar el último backup del origen",
        help="Restaura el último backup completado en vez de dumpear "
             "producción de nuevo (más rápido y sin carga sobre el origen).",
    )
    name = fields.Char(string="Nombre del staging", required=True)
    domain = fields.Char(string="Subdominio", required=True,
                         help="Ej.: staging.forum.primate.cloud")
    region = fields.Selection(AWS_REGIONS, string="Región", required=True)
    # R4-B5 (D-R4.6): dónde vive el staging. Vacío = servidor NUEVO (cadena
    # R2, comportamiento actual). Con un servidor multi-Odoo activo = el
    # staging se monta como una INSTANCIA ahí (el caso barato de R3; default
    # el mismo servidor del origen). Con destino existente no hacen falta los
    # campos de cómputo/red (se ocultan en la vista).
    target_server_id = fields.Many2one(
        "primate.cloud.environment", string="Servidor destino",
        domain="[('state', '=', 'active')]",
        help="Servidor multi-Odoo donde montar el staging como instancia. "
             "Vacío = crear un servidor nuevo dedicado.",
    )

    # --- Cómputo (EC2) ---
    instance_name = fields.Char(string="Nombre de la instancia")
    instance_type = fields.Char(string="Tipo de instancia", required=True, default="t3.small")
    os_type = fields.Selection(OS_TYPES, string="Sistema operativo", default="ubuntu_24")
    image_id = fields.Char(
        string="AMI",
        help="ID de la AMI base (ami-...). Vacío => se resuelve el último "
             "Ubuntu 24.04 de la región automáticamente.",
    )
    disk_size_gb = fields.Integer(string="Disco raíz (GB)", default=30)
    key_name = fields.Char(string="Par de claves (SSH)")
    security_group_ids = fields.Char(string="Grupos de seguridad", help="IDs separados por coma.")
    subnet_id = fields.Char(string="Subred")
    instance_profile = fields.Char(string="Instance profile", help="Perfil IAM (SSM + S3).")

    # --- Base de datos ---
    db_mode = fields.Selection(
        [("local_pg", "PostgreSQL local"), ("rds", "Amazon RDS")],
        string="Base de datos", required=True, default="local_pg",
    )
    db_name = fields.Char(string="Nombre de la base")
    db_user = fields.Char(string="Usuario PostgreSQL", default="odoo")
    db_password = fields.Char(string="Contraseña PostgreSQL", password="True",
                              help="Solo para crear/restaurar; no se almacena.")
    pg_version = fields.Selection(
        [("13", "13"), ("14", "14"), ("15", "15"), ("16", "16")],
        string="Versión PostgreSQL", default="16",
    )
    rds_identifier = fields.Char(string="Identificador RDS")
    rds_instance_class = fields.Char(string="Clase RDS", default="db.t3.medium")
    rds_storage_gb = fields.Integer(string="Almacenamiento RDS (GB)", default=20)
    rds_multi_az = fields.Boolean(string="Multi-AZ")
    backup_retention_days = fields.Integer(string="Retención de backup (días)", default=7)

    # --- Transferencia / DNS / Odoo ---
    transfer_bucket = fields.Char(
        string="Bucket S3 de transferencia", required=True,
        help="Bucket usado para mover el dump origen → destino.",
    )
    create_dns = fields.Boolean(string="Crear registro DNS", default=True)
    hosted_zone_id = fields.Char(string="Hosted Zone ID")
    ttl = fields.Integer(string="TTL", default=300)
    admin_password = fields.Char(string="Contraseña admin (odoo.conf)", password="True")

    @api.model
    def default_get(self, fields_list):
        """Precarga el wizard con la última config de staging guardada en el origen.

        Si una creación previa de staging falló, el usuario reabre el wizard y
        encuentra lo que había ingresado (no tiene que re-tipearlo).
        """
        defaults = super().default_get(fields_list)
        origin_id = self.env.context.get("default_origin_environment_id")
        if origin_id:
            origin = self.env["primate.cloud.environment"].browse(origin_id)
            saved = origin._load_provision_config(field=STAGING_CONFIG_FIELD)
            for key, value in saved.items():
                if key in self._fields:
                    defaults[key] = value
        return defaults

    @api.onchange("origin_environment_id")
    def _onchange_origin(self):
        """Prefija valores a partir del entorno origen."""
        origin = self.origin_environment_id
        if not origin:
            return
        if origin.account_id and not self.region:
            self.region = origin.account_id.default_region
        if not self.name:
            self.name = "%s (Staging)" % origin.name
        if origin.main_url and not self.domain:
            self.domain = "staging.%s" % origin.main_url
        if not self.db_name:
            self.db_name = ("%s_staging" % (origin.name or "")).lower().replace(" ", "_")
        # Default D-R4.6: montar el staging en el MISMO servidor del origen si
        # es multi-Odoo (no legacy) — el caso barato que R3 habilitó.
        if not self.target_server_id and origin.state == "active" \
                and not origin._is_legacy_layout():
            self.target_server_id = origin
        # Origen explícito: preselección solo cuando es inequívoco.
        if not self.origin_instance_id and len(origin.ec2_instance_ids) == 1:
            self.origin_instance_id = origin.ec2_instance_ids
        if not self.origin_database_id and len(origin.database_ids) == 1:
            self.origin_database_id = origin.database_ids

    def _prepare_params(self):
        """Construye el dict de parámetros para el flujo de staging."""
        self.ensure_one()
        security_groups = [
            sg.strip() for sg in (self.security_group_ids or "").split(",") if sg.strip()
        ]
        return {
            "name": self.name,
            "account_id": self.account_id.id,
            "region": self.region,
            "domain": self.domain,
            # Destino (R4-B5): servidor existente (staging=instancia) o nuevo.
            "target_server_id": self.target_server_id.id,
            # Origen explícito (Bloque 5)
            "origin_instance_id": self.origin_instance_id.id,
            "origin_database_id": self.origin_database_id.id,
            "use_last_backup": self.use_last_backup,
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
            # Transferencia / DNS / Odoo
            "transfer_bucket": self.transfer_bucket,
            "create_dns": self.create_dns,
            "hosted_zone_id": self.hosted_zone_id,
            "ttl": self.ttl,
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
        if not (self.transfer_bucket or "").strip():
            raise UserError(_("Indicá el bucket S3 de transferencia."))
        origin = self.origin_environment_id
        if not self.origin_instance_id and len(origin.ec2_instance_ids) != 1:
            raise UserError(_("El origen tiene varias (o ninguna) instancias: "
                              "elegí la instancia de origen."))
        if not self.origin_database_id and len(origin.database_ids) != 1:
            raise UserError(_("El origen tiene varias (o ninguna) bases: "
                              "elegí la base de origen."))
        if (self.origin_instance_id
                and self.origin_instance_id.environment_id != origin):
            raise UserError(_("La instancia de origen no pertenece al entorno "
                              "origen."))
        if (self.origin_database_id
                and self.origin_database_id.environment_id != origin):
            raise UserError(_("La base de origen no pertenece al entorno "
                              "origen."))
        if self.db_mode == "rds":
            if not (self.rds_identifier or "").strip():
                raise UserError(_("RDS: indicá el identificador de la base."))
            if not (self.db_password or "").strip():
                raise UserError(_("RDS: indicá la contraseña maestra."))
        if self.create_dns and not (self.hosted_zone_id or "").strip():
            raise UserError(_("Para crear DNS, indicá el Hosted Zone ID."))

    def _raw_values(self):
        """Valores crudos del wizard a persistir (para precargar al reintentar)."""
        self.ensure_one()
        return {name: self[name] for name in STAGING_PERSISTED_FIELDS}

    def action_create_staging(self):
        """Valida, guarda lo ingresado en el origen y encola el staging."""
        self.ensure_one()
        self._validate()
        # Se guarda en el ORIGEN antes de encolar: si el job falla, queda igual.
        self.origin_environment_id._save_provision_config(
            self._raw_values(), field=STAGING_CONFIG_FIELD)
        self.origin_environment_id._enqueue_staging(self._prepare_params())
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info", "title": _("Creando staging"),
                "message": _("Staging encolado. Pasará a Activo al terminar el flujo."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
