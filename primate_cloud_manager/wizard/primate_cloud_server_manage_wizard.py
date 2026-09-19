# -*- coding: utf-8 -*-
"""Asistente para habilitar la gestión de un servidor adoptado (F2).

Sacar un servidor de ``adopted_readonly`` es la decisión que lo vuelve
operable: a partir de ahí se lo puede detener, reiniciar, terminar y
restaurarle bases. Y los servidores que entran por la importación son
PRODUCTIVOS de clientes reales, así que la confirmación no es una formalidad:
muestra el servidor, la cuenta y las instancias Odoo detectadas con su cliente,
para que quien habilita vea exactamente a quién se está exponiendo.

Mismo nivel de fricción que las otras operaciones peligrosas del módulo
(terminar, borrar DNS, restaurar sobre producción): admin-only server-side y
tipeo del nombre exacto del servidor.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PrimateCloudServerManageWizard(models.TransientModel):
    """Confirmación del pase ``adopted_readonly`` → ``managed``."""

    _name = "primate.cloud.server.manage.wizard"
    _description = "Habilitar la gestión de un servidor adoptado"

    instance_id = fields.Many2one(
        "primate.cloud.ec2.instance",
        string="Servidor",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    account_name = fields.Char(
        related="instance_id.account_id.name", string="Cuenta AWS")
    aws_instance_id = fields.Char(
        related="instance_id.aws_instance_id", string="Instance ID")
    region = fields.Char(related="instance_id.region", string="Región")
    instance_state = fields.Selection(
        related="instance_id.instance_state", string="Estado en AWS")
    detected_instances = fields.Text(
        string="Instancias Odoo detectadas",
        compute="_compute_detected_instances",
        help="Lo que PCM encontró en el servidor, con su cliente. Es a quién "
             "se está exponiendo al habilitar la gestión.",
    )
    confirm_server_name = fields.Char(
        string="Confirmá el nombre del servidor",
        help="Escribí el nombre EXACTO del servidor para habilitar la gestión.",
    )

    def _compute_detected_instances(self):
        """Lista las instancias Odoo del servidor, con su proyecto (cliente)."""
        for wizard in self:
            instancias = wizard.instance_id.environment_id.instance_ids.filtered(
                lambda i: i.state != "archived")
            if not instancias:
                wizard.detected_instances = _(
                    "Ninguna instancia Odoo detectada. Si el servidor tiene "
                    "Odoo instalado, corré el discovery antes de habilitar la "
                    "gestión.")
                continue
            wizard.detected_instances = "\n".join(
                _("• %(slug)s — cliente: %(cliente)s (%(tipo)s, Odoo %(ver)s)",
                  slug=inst.slug or inst.display_name,
                  cliente=inst.project_id.name or _("SIN ASIGNAR"),
                  tipo=dict(inst._fields["env_type"].selection).get(
                      inst.env_type, inst.env_type or "?"),
                  ver=inst.odoo_version or "?")
                for inst in instancias)

    def action_confirm(self):
        """Valida la fricción y efectúa el pase a ``managed``."""
        self.ensure_one()
        servidor = self.instance_id
        # Re-valida server-side TODO lo que validó la acción que abrió el
        # wizard: el wizard no es el único camino hasta acá.
        if servidor.management_state != "adopted_readonly":
            raise UserError(_(
                "El servidor ya no está adoptado en solo lectura (modo "
                "actual: %s).") % dict(
                    servidor._fields["management_state"].selection)[
                        servidor.management_state])
        sin_clasificar = servidor._instancias_sin_clasificar()
        if sin_clasificar:
            raise UserError(_(
                "Clasificá primero las instancias Odoo: %s siguen sin cliente "
                "asignado.") % ", ".join(sin_clasificar.mapped("display_name")))
        if (self.confirm_server_name or "").strip() != servidor.name:
            raise UserError(_(
                "Para habilitar la gestión escribí el nombre exacto del "
                "servidor ('%s').") % servidor.name)
        servidor._habilitar_gestion_confirmado()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Gestión habilitada"),
                "message": _(
                    "'%s' ya se puede operar desde PCM. Quedó registrado en la "
                    "bitácora.") % servidor.name,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
