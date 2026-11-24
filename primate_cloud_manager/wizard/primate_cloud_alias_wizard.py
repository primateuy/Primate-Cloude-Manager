# -*- coding: utf-8 -*-
"""Wizard «Agregar dominio adicional» (alias) — decisión 2026-11-24.

Una SEGUNDA URL para la misma instancia: el registro A lo carga el usuario en
su DNS (PCM no lo escribe); PCM verifica que apunte a la IP del servidor,
monta un vhost en paralelo con certificado propio y deja el dominio
principal y ``web.base.url`` exactamente como estaban. Si algo falla, lo
nuevo se desarma y el servidor queda como estaba.
"""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .primate_cloud_domain_wizard import (
    DOMAIN_RE,
    RESERVED_SUFFIXES,
    PrimateCloudDomainWizard,
)

_logger = logging.getLogger(__name__)


class PrimateCloudAliasWizard(models.TransientModel):
    _name = "primate.cloud.alias.wizard"
    _description = "Dominio adicional (alias) de una instancia"

    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia", required=True,
        ondelete="cascade")
    domain = fields.Char(string="Dominio adicional", required=True)
    current_domain = fields.Char(
        string="Dominio principal", related="instance_id.main_url")
    server_ip = fields.Char(string="IP del servidor",
                            compute="_compute_server_ip")

    @api.depends("instance_id")
    def _compute_server_ip(self):
        for wiz in self:
            machine = wiz.instance_id._machine() if wiz.instance_id else False
            wiz.server_ip = machine.public_ip if machine else False

    def _clean_domain(self):
        domain = (self.domain or "").strip().lower().rstrip(".")
        domain = re.sub(r"^https?://", "", domain).split("/")[0]
        if not DOMAIN_RE.match(domain):
            raise UserError(_("«%s» no es un dominio válido.") % self.domain)
        if domain.endswith(RESERVED_SUFFIXES):
            raise UserError(_(
                "«%s» es un dominio reservado: nunca va a resolver en "
                "internet.") % domain)
        if domain == (self.instance_id.main_url or "").strip().lower():
            raise UserError(_(
                "%s ya es el dominio PRINCIPAL de la instancia.") % domain)
        return domain

    def action_confirm(self):
        self.ensure_one()
        instance = self.instance_id
        instance._pcm_assert_not_imported(_("agregar un dominio adicional"))
        if instance.state != "active":
            raise UserError(_("La instancia tiene que estar activa."))
        machine = instance._machine()
        if not machine or not machine.public_ip:
            raise UserError(_(
                "El servidor no tiene máquina activa con IP pública."))
        domain = self._clean_domain()
        existente = self.env["primate.cloud.instance.alias"].sudo().search(
            [("name", "=", domain)], limit=1)
        if existente:
            raise UserError(_(
                "%(d)s ya está asociado como alias de «%(i)s».",
                d=domain, i=existente.instance_id.display_name))
        # Aviso temprano (resolver de PCM); el job vuelve a verificar desde
        # el servidor, que es el que decide.
        ips = PrimateCloudDomainWizard._resolve_ipv4(domain)
        if machine.public_ip not in ips:
            if ips:
                motivo = _(
                    "%(d)s apunta a %(a)s, no a la IP del servidor (%(ip)s).",
                    d=domain, a=", ".join(ips), ip=machine.public_ip)
            else:
                motivo = _(
                    "%(d)s todavía no resuelve: tiene que apuntar a la IP "
                    "del servidor (%(ip)s).", d=domain, ip=machine.public_ip)
            raise UserError(_(
                "%s Creá o corregí el registro A y volvé a intentar. No se "
                "cambió nada.") % motivo)
        alias = self.env["primate.cloud.instance.alias"].create({
            "name": domain, "instance_id": instance.id})
        alias.with_delay(
            description=_("Dominio adicional: %s") % domain
        ).job_mount()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info", "sticky": False,
                       "message": _(
                           "%(d)s apunta al servidor. PCM monta el vhost en "
                           "paralelo y emite su certificado; %(main)s sigue "
                           "intacto.", d=domain,
                           main=instance.main_url or "—"),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
