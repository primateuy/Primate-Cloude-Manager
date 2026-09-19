# -*- coding: utf-8 -*-
"""Asistente para importar los servidores existentes de una cuenta AWS (F2).

Elige regiones, estados y si se corre el discovery, y encola la corrida. No
toca AWS: eso lo hace el job. Lo que entra queda en solo lectura
(``adopted_readonly``), así que lanzar la importación es inofensivo; aun así
es admin-only, porque es la puerta de entrada de servidores de clientes a PCM.
"""
from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

from ..models.primate_cloud_account import AWS_REGIONS


class PrimateCloudServerImportWizard(models.TransientModel):
    """Parámetros de la importación de servidores."""

    _name = "primate.cloud.server.import.wizard"
    _description = "Importar servidores desde AWS"

    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True,
        readonly=True, ondelete="cascade")
    all_regions = fields.Boolean(
        string="Todas las regiones soportadas",
        help="Barre todas las regiones que PCM soporta. Si no, sólo las que "
             "escribas abajo.")
    regions = fields.Char(
        string="Regiones",
        help="Códigos separados por coma. Ej.: us-east-1, sa-east-1")
    include_stopped = fields.Boolean(
        string="Incluir servidores detenidos", default=True,
        help="Los que están corriendo se importan siempre. Los detenidos se "
             "registran, pero sin acceso SSM no se les puede correr el "
             "discovery.")
    discover = fields.Boolean(
        string="Descubrir instancias Odoo", default=True,
        help="Por cada servidor con acceso SSM, lee (sólo lectura) sus units "
             "de Odoo, su odoo.conf y sus bases, y las registra sin cliente "
             "asignado.")
    regiones_validas = fields.Char(
        string="Regiones soportadas", readonly=True,
        default=lambda self: ", ".join(code for code, _label in AWS_REGIONS))

    @api.model
    def default_get(self, fields_list):
        """Propone la región por defecto de la cuenta."""
        res = super().default_get(fields_list)
        account = self.env["primate.cloud.account"].browse(
            res.get("account_id") or 0).exists()
        if account and "regions" in fields_list:
            res.setdefault("regions", account.default_region)
        return res

    def action_import(self):
        """Valida, crea la corrida y la encola. Devuelve la corrida abierta."""
        self.ensure_one()
        if self.env.uid != SUPERUSER_ID and not self.env.user.has_group(
                "primate_cloud_manager.group_cloud_admin"):
            raise UserError(_("Solo un Cloud Admin puede importar servidores."))
        if self.account_id.connection_state != "connected":
            raise UserError(_(
                "La cuenta no está conectada: validá la conexión antes de "
                "importar."))
        validas = [code for code, _label in AWS_REGIONS]
        if self.all_regions:
            regiones = validas
        else:
            regiones = [r.strip() for r in (self.regions or "").split(",")
                        if r.strip()]
            invalidas = [r for r in regiones if r not in validas]
            if invalidas:
                raise UserError(_(
                    "Regiones no soportadas: %(inv)s. Las soportadas son: "
                    "%(ok)s.", inv=", ".join(invalidas), ok=", ".join(validas)))
            if not regiones:
                raise UserError(_("Indicá al menos una región."))
        corrida = self.env["primate.cloud.server.import"].create({
            "name": _("Importación %(cuenta)s — %(fecha)s",
                      cuenta=self.account_id.name,
                      fecha=fields.Datetime.now().strftime("%Y-%m-%d %H:%M")),
            "account_id": self.account_id.id,
            "regions": ", ".join(regiones),
            "include_stopped": self.include_stopped,
            "discover": self.discover,
        })
        corrida.with_delay(
            description=_("Importar servidores: %s") % self.account_id.name
        ).job_import_servers()
        return {
            "type": "ir.actions.act_window",
            "name": _("Importación de servidores"),
            "res_model": "primate.cloud.server.import",
            "res_id": corrida.id,
            "view_mode": "form",
            "target": "current",
        }
