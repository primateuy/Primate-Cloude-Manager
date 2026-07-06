# -*- coding: utf-8 -*-
"""Endpoint /pcm/impersonate — la pieza más crítica del companion.

Recibe un token firmado por PCM (Ed25519; la privada NUNCA está acá), lo valida
en orden estricto y, solo si todo cierra, abre una sesión como el usuario destino
SIN leer ni tocar su contraseña. Deja traza en el registro auditable del cliente.
"""
import base64
import json
import logging
import time

from odoo import SUPERUSER_ID, api
from odoo.http import Controller, request, route
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


def _b64url_decode(data):
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


class PcmImpersonateController(Controller):

    @route("/pcm/impersonate", type="http", auth="none", csrf=False,
           methods=["GET"])
    def impersonate(self, token=None, **kw):
        """Valida el token y abre la sesión. Cualquier fallo → 403, sin sesión."""
        if not token or "." not in token:
            return request.make_response("forbidden", status=403)
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload_bytes = _b64url_decode(payload_b64)
            signature = _b64url_decode(sig_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
            db = payload["db"]
            uid = int(payload["uid"])
        except Exception:  # noqa: BLE001
            return request.make_response("forbidden", status=403)

        # Toda la validación ORM corre en la BD DESTINO (registro explícito).
        try:
            registry = Registry(db)
        except Exception:  # noqa: BLE001 - db inexistente
            return request.make_response("forbidden", status=403)

        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            Log = env["pcm.impersonation.log"].sudo()
            # 1) enabled → 2) firma → 3) exp → 4) nonce → 5) usuario ok
            if not Log._is_enabled():
                return request.make_response("forbidden", status=403)
            if not self._verify(Log._public_key(), payload_bytes, signature):
                return request.make_response("forbidden", status=403)
            if float(payload.get("exp", 0)) < time.time():
                return request.make_response("forbidden", status=403)
            if not env["pcm.impersonation.nonce"]._consume(payload.get("nonce")):
                return request.make_response("forbidden", status=403)
            user = env["res.users"].browse(uid)
            if not user.exists() or not user.active or user.share:
                return request.make_response("forbidden", status=403)

            is_admin = self._is_admin(user)
            # El token debe declarar explícitamente que el destino es admin (el
            # lado PCM lo obliga con un paso extra). Si no coincide → 403.
            if is_admin and not payload.get("admin_ack"):
                return request.make_response("forbidden", status=403)

            session_token = user._compute_session_token(request.session.sid)
            log = Log.create({
                "target_login": user.login,
                "target_uid": uid,
                "source_admin": payload.get("admin") or "PCM",
                "is_admin_target": is_admin,
                "database": db,
                "session_sid": request.session.sid,
            })
            epoch = Log._epoch()
            cr.commit()

        # Sesión same-origin (el navegador queda logueado en ESTE dominio).
        request.session.db = db
        request.session.uid = uid
        request.session.login = payload.get("login") or user.login
        request.session.session_token = session_token
        # Marca de impersonación: la usa el kill-switch (ir_http) y el banner.
        request.session["pcm_imp"] = {
            "epoch": epoch, "created": time.time(), "log_id": log.id,
        }
        return request.redirect("/web")

    @staticmethod
    def _verify(pubkey_b64, payload_bytes, signature):
        """Verifica la firma Ed25519 con la clave PÚBLICA. False si no valida."""
        if not pubkey_b64:
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey)
            pub = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pubkey_b64))
            pub.verify(signature, payload_bytes)
            return True
        except Exception:  # noqa: BLE001 - firma inválida / lib ausente
            return False

    @staticmethod
    def _is_admin(user):
        """True si el usuario tiene rol de administración (Settings)."""
        try:
            return user.has_group("base.group_system")
        except Exception:  # noqa: BLE001
            return False
