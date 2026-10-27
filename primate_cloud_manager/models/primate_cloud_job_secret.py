# -*- coding: utf-8 -*-
"""Secretos de los jobs, fuera de queue_job (decisión 2026-10-27).

queue_job persiste los argumentos de cada job en claro (args, kwargs y el
``func_string`` que se muestra en la UI). Ahí viajaban la contraseña PG
transitoria, la del admin del odoo.conf y los tokens de un solo uso de las
acciones destructivas.

Ahora el job lleva solo una REFERENCIA (``{"pcm_secret_ref": "<uuid>"}``) y
el secreto vive acá, cifrado con la misma clave Fernet que las credenciales
de AWS. El job lo descifra al ejecutarse. Un requeue reusa la misma
referencia (por eso no se borra al leerlo); el cron purga lo viejo.

La guarda de ``queue.job.create`` rechaza cualquier job que intente guardar
uno de estos secretos en claro: si alguien agrega un job nuevo y se olvida
de ``stash``, falla enseguida y no en una auditoría.
"""
import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import crypto

_logger = logging.getLogger(__name__)

# Claves que NUNCA viajan en claro en un job (en cualquier nivel del dict).
SECRET_KEYS = frozenset({
    "db_password", "admin_password", "pg_password", "github_token",
    "job_token", "destruction_job_token",
})
REF_KEY = "pcm_secret_ref"
# Un job fallido se puede reencolar días después; pasado esto, se relanza
# desde el wizard (que vuelve a pedir o generar el secreto).
RETENTION_DAYS = 30


def _is_ref(value):
    return isinstance(value, dict) and set(value) == {REF_KEY}


def find_plaintext(value, path=""):
    """Rutas donde una clave secreta guarda un valor en claro."""
    found = []
    if isinstance(value, dict):
        for key, val in value.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if key in SECRET_KEYS and val and not _is_ref(val):
                found.append(here)
            else:
                found.extend(find_plaintext(val, here))
    elif isinstance(value, (list, tuple)):
        for i, val in enumerate(value):
            found.extend(find_plaintext(val, "%s[%s]" % (path, i)))
    return found


class PrimateCloudJobSecret(models.Model):
    _name = "primate.cloud.job.secret"
    _description = "Secreto transitorio de un job (cifrado)"
    _order = "id desc"

    ref = fields.Char(required=True, index=True, readonly=True, copy=False)
    value_encrypted = fields.Text(required=True, readonly=True, copy=False)

    _ref_uniq = models.Constraint("UNIQUE(ref)", "La referencia ya existe.")

    @api.model
    def _key(self):
        return self.env["primate.cloud.account"]._get_encryption_key()

    @api.model
    def stash(self, value):
        """Copia de ``value`` con cada secreto reemplazado por su referencia
        (los dicts/listas se recorren enteros; lo demás queda igual)."""
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                if key in SECRET_KEYS and val and not _is_ref(val):
                    ref = uuid.uuid4().hex
                    self.sudo().create({
                        "ref": ref,
                        "value_encrypted": crypto.encrypt(self._key(), str(val)),
                    })
                    out[key] = {REF_KEY: ref}
                else:
                    out[key] = self.stash(val)
            return out
        if isinstance(value, list):
            return [self.stash(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.stash(v) for v in value)
        return value

    @api.model
    def reveal(self, value):
        """Inverso de ``stash``: las referencias vuelven a ser el secreto."""
        if _is_ref(value):
            rec = self.sudo().search([("ref", "=", value[REF_KEY])], limit=1)
            if not rec:
                raise UserError(_(
                    "El secreto de este job ya no existe (se purga a los %s "
                    "días): relanzá la operación desde su wizard.")
                    % RETENTION_DAYS)
            plano = crypto.decrypt(self._key(), rec.value_encrypted)
            if plano is False:
                raise UserError(_(
                    "No pude descifrar el secreto del job: ¿cambió la clave "
                    "de cifrado (PRIMATE_CLOUD_FERNET_KEY) de esta base?"))
            return plano
        if isinstance(value, dict):
            return {k: self.reveal(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.reveal(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.reveal(v) for v in value)
        return value

    @api.model
    def _cron_purge(self):
        limite = fields.Datetime.now() - timedelta(days=RETENTION_DAYS)
        viejos = self.sudo().search([("create_date", "<", limite)])
        viejos.unlink()
        return len(viejos)


class QueueJob(models.Model):
    """Guarda: ningún job guarda un secreto en claro."""

    _inherit = "queue.job"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            rutas = (find_plaintext(vals.get("args") or [], "args")
                     + find_plaintext(vals.get("kwargs") or {}, "kwargs"))
            if rutas:
                raise UserError(_(
                    "Un job intentó guardar un secreto en claro (%(r)s) en "
                    "«%(m)s.%(f)s». Los secretos van por "
                    "primate.cloud.job.secret.stash().", r=", ".join(rutas),
                    m=vals.get("model_name"), f=vals.get("method_name")))
        return super().create(vals_list)
