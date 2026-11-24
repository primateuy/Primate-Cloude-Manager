# -*- coding: utf-8 -*-
"""Dominio adicional (alias) de una instancia — decisión 2026-11-24.

Una instancia puede servir por MÁS de una URL: su dominio principal
(``main_url``, el que manda en ``web.base.url`` y en los links de PCM) y N
dominios adicionales. El alias es solo nginx + certificado en el servidor:

- vhost PARALELO con el mismo template del dominio principal;
- certificado Let's Encrypt propio;
- NO toca el vhost ni el certificado del dominio principal, ni
  ``web.base.url``.

El DNS del alias lo carga el cliente en su proveedor (registro A a la IP
fija del servidor): PCM verifica, nunca lo escribe. Caso que originó la
feature: altanapipa.com junto a altanapipa.primatecloudmanager.com.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PrimateCloudInstanceAlias(models.Model):
    _name = "primate.cloud.instance.alias"
    _description = "Dominio adicional (alias) de una instancia"
    _order = "instance_id, name"

    name = fields.Char(
        string="Dominio", required=True, index=True, copy=False,
        readonly=True)
    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia", required=True,
        ondelete="cascade", index=True, readonly=True)
    state = fields.Selection(
        [("pending", "Montando"),
         ("active", "Activo"),
         ("failed", "Falló")],
        string="Estado", default="pending", required=True, copy=False)
    error_message = fields.Char(string="Último error", copy=False)
    ssl_cert_expiry = fields.Datetime(
        string="Vencimiento del certificado", copy=False)

    _alias_domain_uniq = models.Constraint(
        "UNIQUE(name)",
        "Ese dominio ya está asociado como alias de una instancia.",
    )

    # ------------------------------------------------------------------
    def action_retry(self):
        """Reintenta el montaje de un alias que falló."""
        self.ensure_one()
        inst = self.instance_id
        inst._pcm_assert_not_imported(_("agregar un dominio adicional"))
        if self.state == "active":
            raise UserError(_("El alias %s ya está activo.") % self.name)
        self.write({"state": "pending", "error_message": False})
        self.with_delay(
            description=_("Dominio adicional: %s") % self.name
        ).job_mount()
        return True

    def action_remove(self):
        """Quita el alias del servidor (vhost + certificado) y borra el
        registro. DESTRUCTIVO para el alias; el dominio principal y los
        demás alias no se tocan. La vista pide confirmación explícita."""
        self.ensure_one()
        inst = self.instance_id
        inst._pcm_assert_not_imported(_("quitar un dominio adicional"))
        self.with_delay(
            description=_("Quitar dominio adicional: %s") % self.name
        ).job_remove()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info", "sticky": False,
                       "message": _("Quitando %s del servidor…") % self.name,
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    # ------------------------------------------------------------------
    def job_mount(self):
        """Job: monta el alias en el servidor (vhost paralelo + certbot)."""
        self.ensure_one()
        inst = self.instance_id
        inst._pcm_assert_not_imported(_("agregar un dominio adicional"))
        return inst.environment_id._mount_instance_alias(inst, self)

    def job_remove(self):
        """Job: desmonta el alias del servidor y borra el registro."""
        self.ensure_one()
        inst = self.instance_id
        inst._pcm_assert_not_imported(_("quitar un dominio adicional"))
        return inst.environment_id._remove_instance_alias(inst, self)
