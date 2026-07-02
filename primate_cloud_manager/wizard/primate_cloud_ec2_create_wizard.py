# -*- coding: utf-8 -*-
"""Wizard de creación de una instancia EC2 (spec 5.3, Fase 4).

Captura los parámetros y encola la creación como job (toda llamada AWS va en
queue_job). La instancia queda registrada en el inventario, opcionalmente
asociada a un entorno.
"""
import uuid

from odoo import _, api, fields, models

from ..models.primate_cloud_account import AWS_REGIONS
from ..services import aws_ec2

# Sistemas operativos ofrecidos (mismo dominio que el campo del modelo EC2).
OS_TYPES = [
    ("ubuntu_22", "Ubuntu 22.04"),
    ("ubuntu_24", "Ubuntu 24.04"),
    ("amazon_linux", "Amazon Linux"),
    ("other", "Otro"),
]


class PrimateCloudEc2CreateWizard(models.TransientModel):
    """Crea una instancia EC2 y la registra en el inventario."""

    _name = "primate.cloud.ec2.create.wizard"
    _description = "Crear instancia EC2"

    name = fields.Char(string="Nombre", required=True, help="Tag Name de la instancia.")
    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True, ondelete="cascade"
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno", ondelete="cascade",
        help="Entorno al que se asocia la instancia (opcional).",
    )
    region = fields.Selection(AWS_REGIONS, string="Región", required=True)
    instance_type = fields.Char(string="Tipo de instancia", required=True, default="t3.medium")
    os_type = fields.Selection(OS_TYPES, string="Sistema operativo", default="ubuntu_24")
    image_id = fields.Char(
        string="AMI",
        help="ID de la AMI base (ami-...). Si lo dejás vacío, se resuelve el "
             "último Ubuntu 24.04 de la región automáticamente.",
    )
    disk_size_gb = fields.Integer(string="Disco raíz (GB)", default=30)
    key_name = fields.Char(string="Par de claves (SSH)")
    security_group_ids = fields.Char(
        string="Grupos de seguridad", help="IDs separados por coma (sg-...)."
    )
    subnet_id = fields.Char(string="Subred")
    instance_profile = fields.Char(
        string="Instance profile", help="Perfil IAM de la instancia (agente SSM)."
    )

    @api.onchange("account_id")
    def _onchange_account_id(self):
        """Propone la región por defecto de la cuenta."""
        if self.account_id and not self.region:
            self.region = self.account_id.default_region

    @api.onchange("environment_id")
    def _onchange_environment_id(self):
        """Hereda cuenta y región del entorno elegido."""
        if self.environment_id:
            self.account_id = self.environment_id.account_id
            if self.environment_id.account_id and not self.region:
                self.region = self.environment_id.account_id.default_region

    def _prepare_vals(self):
        """Construye el dict de parámetros para el job de creación."""
        self.ensure_one()
        security_groups = [
            sg.strip() for sg in (self.security_group_ids or "").split(",") if sg.strip()
        ]
        return {
            "name": self.name,
            "environment_id": self.environment_id.id,
            "region": self.region,
            "instance_type": self.instance_type,
            "os_type": self.os_type,
            "image_id": self.image_id,
            "disk_size_gb": self.disk_size_gb,
            "key_name": self.key_name,
            "security_group_ids": security_groups,
            "subnet_id": self.subnet_id,
            "instance_profile": self.instance_profile,
            # Token de idempotencia: evita EC2 duplicadas si el job se re-ejecuta.
            "client_token": "pcm-%s" % uuid.uuid4().hex,
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

    def action_create_instance(self):
        """Encola la creación de la instancia en AWS (AMI opcional: se resuelve)."""
        self.ensure_one()
        self.account_id.with_delay(
            description=_("Crear EC2: %s") % self.name
        ).job_create_ec2(self._prepare_vals())
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "message": _("Creación de instancia encolada. Se registrará al terminar."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
