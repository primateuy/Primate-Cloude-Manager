# -*- coding: utf-8 -*-
"""Bitácora inmutable de operaciones ejecutadas desde Odoo sobre AWS."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tipos de acción auditables. Se irá ampliando a medida que avanzan las fases.
ACTION_TYPES = [
    ("connection_test", "Validar conexión"),
    ("sync", "Sincronizar recursos"),
    ("retag", "Re-etiquetar recursos"),
    ("ec2_start", "Iniciar EC2"),
    ("ec2_stop", "Detener EC2"),
    ("ec2_restart", "Reiniciar EC2"),
    ("ec2_terminate", "Terminar EC2"),
    ("ec2_create", "Crear EC2"),
    ("ssm_command", "Ejecutar comando SSM"),
    ("rds_create", "Crear RDS"),
    ("dns_create", "Crear registro DNS"),
    ("dns_update", "Actualizar registro DNS"),
    ("dns_delete", "Eliminar registro DNS"),
    ("provision", "Aprovisionar entorno"),
    ("commit_sync", "Sincronizar commits"),
    ("repo_check", "Verificar estado de repo"),
    ("module_detect", "Detectar módulos"),
    ("deploy", "Despliegue"),
    ("staging_create", "Crear staging"),
    ("staging_refresh", "Refrescar staging"),
    ("backup_run", "Ejecutar backup"),
    ("backup_restore", "Restaurar backup"),
    ("backup_check", "Verificar cumplimiento de respaldo"),
    ("other", "Otra"),
]


class PrimateCloudOperationLog(models.Model):
    """Registro de auditoría append-only.

    Toda acción que el módulo ejecuta sobre AWS/SSM/SSH se asienta acá con
    usuario, fecha, recurso afectado, resultado y, cuando aplica, el AWS
    Request ID. El registro es **inmutable**: no se puede editar ni eliminar
    desde la interfaz (ni por código de negocio); solo se crean entradas.
    """

    _name = "primate.cloud.operation.log"
    _description = "Bitácora de Operaciones Cloud"
    _order = "execution_date desc, id desc"

    name = fields.Char(string="Acción", required=True, readonly=True)
    user_id = fields.Many2one(
        "res.users",
        string="Usuario",
        required=True,
        readonly=True,
        ondelete="restrict",
        default=lambda self: self.env.user,
        index=True,
    )
    execution_date = fields.Datetime(
        string="Fecha",
        required=True,
        readonly=True,
        default=fields.Datetime.now,
        index=True,
    )
    action_type = fields.Selection(
        ACTION_TYPES,
        string="Tipo de acción",
        required=True,
        readonly=True,
        index=True,
    )
    resource_model = fields.Char(string="Modelo afectado", readonly=True)
    resource_id = fields.Integer(string="ID del recurso", readonly=True)
    resource_name = fields.Char(string="Nombre del recurso", readonly=True)
    result = fields.Selection(
        [("success", "Exitoso"), ("failed", "Fallido"), ("partial", "Parcial")],
        string="Resultado",
        required=True,
        readonly=True,
        default="success",
        index=True,
    )
    error_message = fields.Text(string="Mensaje de error", readonly=True)
    aws_request_id = fields.Char(string="AWS Request ID", readonly=True)

    @api.model
    def log_operation(
        self,
        action_type,
        name=None,
        record=None,
        result="success",
        error_message=None,
        aws_request_id=None,
    ):
        """Crea una entrada de bitácora. Punto único de registro para los modelos.

        Se ejecuta con ``sudo`` para que cualquier perfil con permiso de acción
        pueda dejar traza, sin necesidad de permiso de escritura sobre el log.

        Args:
            action_type (str): uno de :data:`ACTION_TYPES`.
            name (str, optional): descripción legible; si falta se deriva del tipo.
            record (recordset, optional): recurso afectado (se toma modelo/id/nombre).
            result (str): ``success`` / ``failed`` / ``partial``.
            error_message (str, optional): detalle del error si ``result == failed``.
            aws_request_id (str, optional): Request ID devuelto por AWS.

        Returns:
            recordset: la entrada creada.
        """
        vals = {
            "name": name or dict(ACTION_TYPES).get(action_type, action_type),
            "action_type": action_type,
            "result": result,
            "error_message": error_message,
            "aws_request_id": aws_request_id,
            "user_id": self.env.uid,
        }
        if record is not None and record:
            record = record[:1]
            vals.update(
                {
                    "resource_model": record._name,
                    "resource_id": record.id,
                    "resource_name": record.display_name,
                }
            )
        return self.sudo().create(vals)

    def write(self, vals):
        """Bloquea toda edición: la bitácora es inmutable."""
        raise UserError(_("La bitácora de operaciones es inmutable: no puede editarse."))

    def unlink(self):
        """Bloquea toda eliminación: la bitácora es inmutable."""
        raise UserError(_("La bitácora de operaciones es inmutable: no puede eliminarse."))
