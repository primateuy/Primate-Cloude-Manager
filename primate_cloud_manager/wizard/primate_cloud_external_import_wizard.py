# -*- coding: utf-8 -*-
"""Importar un servidor externo (Cloudpepper) por su gemelo, desde archivo.

PCM NO se conecta a Cloudpepper: ni API ni SSH (regla dura 2026-10-22,
punto 2e). El gemelo se genera en el servidor con el MISMO script de solo
lectura que usa el documento SSM de AWS (data/pcm_readonly_discovery.sh,
que no escribe nada), lo corre una persona por su acceso habitual, y el
archivo de salida se sube acá. Resultado: entorno con marca de origen
«Importado de Cloudpepper (solo lectura)», instancias Odoo, repos con
alertas y un snapshot versionado. Re-importar sobre el mismo entorno = nueva
versión del snapshot (para comparar contra el ambiente nuevo).
"""
import base64

from odoo import _, fields, models
from odoo.exceptions import UserError


class PrimateCloudExternalImportWizard(models.TransientModel):
    _name = "primate.cloud.external.import.wizard"
    _description = "Importar servidor externo (gemelo desde archivo)"

    provider = fields.Selection(
        [("cloudpepper", "Cloudpepper")], string="Proveedor",
        default="cloudpepper", required=True)
    server_name = fields.Char(
        string="Nombre del servidor",
        help="Cómo se va a llamar el entorno en PCM (ej.: cp-clientes-1).")
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno existente",
        domain=[("pcm_origin", "=", "imported_cloudpepper")],
        help="Para RE-capturar: una versión nueva del snapshot del mismo "
             "servidor. Vacío = servidor nuevo.")
    project_id = fields.Many2one(
        "primate.cloud.project", string="Proyecto (cliente)",
        help="Vacío = «sin clasificar»; las instancias se clasifican después.")
    twin_file = fields.Binary(string="Salida del discovery", required=True)
    twin_filename = fields.Char(string="Archivo")

    def action_import(self):
        self.ensure_one()
        try:
            stdout = base64.b64decode(self.twin_file or b"").decode(
                "utf-8", errors="replace")
        except Exception as error:  # noqa: BLE001
            raise UserError(_("El archivo no se pudo leer: %s") % error)
        twin = self.env["primate.cloud.ec2.instance"]._twin_parse(stdout)
        Environment = self.env["primate.cloud.environment"].with_context(
            pcm_import_sync=True)
        env_rec = self.environment_id
        if not env_rec:
            if not (self.server_name or "").strip():
                raise UserError(_("Poné el nombre del servidor."))
            # Sin proyecto elegido: el centinela «SIN CLASIFICAR» del import
            # de AWS (mismo criterio: se clasifica después, por instancia).
            project = self.project_id or self.env[
                "primate.cloud.server.import"]._proyecto_centinela()
            env_rec = Environment.create({
                "name": self.server_name.strip(),
                "project_id": project.id,
                "env_type": "production",
                "state": "active",
                "pcm_origin": "imported_cloudpepper",
            })
        snapshots = env_rec.with_context(pcm_import_sync=True)._twin_apply(twin)
        alertas = sum(snapshots.mapped("alert_count"))
        env_rec.message_post(body=_(
            "Gemelo importado desde archivo (%(prov)s, host «%(host)s»): "
            "%(n)s instancia(s) Odoo, %(a)s alerta(s). Solo lectura.",
            prov=dict(self._fields["provider"].selection)[self.provider],
            host=twin.get("host") or "?", n=len(snapshots), a=alertas))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "type": "warning" if alertas else "success", "sticky": True,
                "message": _("«%(env)s»: %(n)s instancia(s), %(a)s alerta(s) "
                             "(ver pestaña Gemelo de cada instancia).",
                             env=env_rec.name, n=len(snapshots), a=alertas),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
