# -*- coding: utf-8 -*-
"""Modelos del companion de impersonación (viven en el Odoo del cliente).

Registro auditable POR EL CLIENTE + nonces consumidos (anti-replay) + helpers de
configuración (todo por ``ir.config_parameter``, escritos por PCM al desplegar).
La clave PRIVADA nunca está acá: solo la pública, para verificar la firma.
"""
from odoo import api, fields, models

PARAM_ENABLED = "pcm.impersonate.enabled"
PARAM_PUBKEY = "pcm.impersonate.public_key"
PARAM_EPOCH = "pcm.impersonate.epoch"          # se sube para cortar sesiones vivas
PARAM_BANNER = "pcm.impersonate.banner"        # gancho del banner (off por default)
PARAM_MAX_HOURS = "pcm.impersonate.session_max_hours"
# R4-B3 (v2): ref inmutable de la instancia dueña de este companion. El token
# debe declararla (claim ``instance_ref``); si no coincide → 403. Es defensa
# en profundidad del aislamiento cruzado: aunque por error operativo dos
# instancias compartieran clave, el claim corta un token de A usado en B.
PARAM_INSTANCE_REF = "pcm.impersonate.instance_ref"


class PcmImpersonationLog(models.Model):
    _name = "pcm.impersonation.log"
    _description = "Registro de impersonaciones de soporte (PCM)"
    _order = "create_date desc, id desc"

    target_login = fields.Char("Usuario impersonado", readonly=True)
    target_uid = fields.Integer("UID impersonado", readonly=True)
    source_admin = fields.Char("Operador PCM", readonly=True,
                               help="Quién pidió la impersonación desde PCM.")
    is_admin_target = fields.Boolean("Destino con rol admin", readonly=True)
    database = fields.Char("Base", readonly=True)
    session_sid = fields.Char("Sesión", readonly=True, index=True)
    ended_reason = fields.Char("Fin", readonly=True,
                               help="disabled / epoch / expired / logout.")

    @api.model
    def _param(self, key, default=None):
        # Lectura FRESCA (search, no get_param): get_param está ormcacheado y un
        # cambio desde OTRO proceso (PCM por SSM) no invalida la cache del server
        # corriendo. El kill-switch y el enabled tienen que verse al instante.
        rec = self.env["ir.config_parameter"].sudo().search(
            [("key", "=", key)], limit=1)
        return rec.value if rec else default

    @api.model
    def _is_enabled(self):
        return self._param(PARAM_ENABLED, "False") == "True"

    @api.model
    def _epoch(self):
        try:
            return int(self._param(PARAM_EPOCH, "0"))
        except (TypeError, ValueError):
            return 0

    @api.model
    def _max_hours(self):
        try:
            return int(self._param(PARAM_MAX_HOURS, "8"))
        except (TypeError, ValueError):
            return 8

    @api.model
    def _public_key(self):
        return self._param(PARAM_PUBKEY, "") or ""

    @api.model
    def _instance_ref(self):
        # Fresco (search): PCM lo escribe por SSM al desplegar; identifica de
        # qué instancia es este companion.
        return self._param(PARAM_INSTANCE_REF, "") or ""

    @api.model
    def _banner_on(self):
        return self._param(PARAM_BANNER, "False") == "True"


class PcmImpersonationNonce(models.Model):
    _name = "pcm.impersonation.nonce"
    _description = "Nonces de impersonación consumidos (anti-replay)"

    nonce = fields.Char(required=True, index=True)
    _sql_constraints = [
        ("nonce_uniq", "unique(nonce)", "Nonce ya usado."),
    ]

    @api.model
    def _consume(self, nonce):
        """Marca el nonce como usado. Devuelve False si ya estaba (replay)."""
        if not nonce:
            return False
        if self.sudo().search_count([("nonce", "=", nonce)]):
            return False
        try:
            self.sudo().create({"nonce": nonce})
        except Exception:  # noqa: BLE001 - carrera: otro request lo creó primero
            return False
        return True
