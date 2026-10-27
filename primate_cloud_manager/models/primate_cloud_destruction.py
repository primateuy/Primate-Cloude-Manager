# -*- coding: utf-8 -*-
"""Tickets de destrucción (regla dura 2026-10-22, punto 2b del encargo).

Una acción DESTRUCTIVA sobre un recurso montado por PCM (terminar EC2,
desmontar una instancia Odoo, borrar una base, borrar un registro DNS
remoto, un restore que pisa datos, borrar un backup con su objeto S3) exige:

  1) el grupo «PCM: operaciones destructivas» (vacío por defecto: ni el
     admin lo tiene; lo asigna Daryl a mano);
  2) la TRIPLE confirmación, validada EN EL SERVIDOR con tokens de un solo
     uso atados a usuario + recurso + acción, con vencimiento corto:
       paso 1 — diálogo de impacto (qué se destruye, cuenta, región,
                backups recientes, irreversibilidad) → token_1
       paso 2 — escribir a mano el nombre EXACTO del recurso → token_2
       paso 3 — re-autenticación (contraseña y, si hay 2FA, TOTP) → token_3
  3) el método destructivo consume token_3; sin token válido NO arranca,
     venga de la UI, de RPC, de otro job o de un cron (los crons y
     reintentos automáticos no tienen cómo fabricar un token).

Cada intento —completado, cancelado, vencido o fallido— queda en la
bitácora inmutable con usuario, fecha, recurso, acción y el paso alcanzado.
Los recursos IMPORTADOS ni siquiera llegan acá: el guard de solo-lectura
corta antes (ni el grupo destructivo los habilita).
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

TICKET_TTL_MINUTES = 5
GROUP_DESTRUCTIVE = "primate_cloud_manager.group_cloud_destructive"


def durable_log(env, vals):
    """Bitácora que SOBREVIVE al rollback del intento que se registra.

    Un intento bloqueado o rechazado termina en UserError → rollback de su
    transacción: si el registro de auditoría viviera ahí, desaparecería con
    él. Va en un cursor propio que commitea ya. En tests (current_test) se
    escribe en la transacción del test: abrir una conexión real escribiría
    fuera del sandbox de la suite.
    """
    from odoo.modules import module as odoo_module
    if odoo_module.current_test:
        return env["primate.cloud.operation.log"].sudo().create(vals).id
    with env.registry.cursor() as cr:
        new_env = api.Environment(cr, env.uid, dict(env.context))
        return new_env["primate.cloud.operation.log"].sudo().create(vals).id


class PrimateCloudDestructionTicket(models.Model):
    _name = "primate.cloud.destruction.ticket"
    _description = "Ticket de confirmación de acción destructiva"
    _order = "id desc"

    user_id = fields.Many2one("res.users", string="Usuario", required=True,
                              readonly=True, ondelete="cascade")
    res_model = fields.Char(string="Modelo", required=True, readonly=True)
    res_id = fields.Integer(string="Registro", required=True, readonly=True)
    action = fields.Char(string="Acción", required=True, readonly=True)
    resource_name = fields.Char(string="Nombre exacto del recurso",
                                required=True, readonly=True)
    step = fields.Selection(
        [("impact", "1/3 — impacto mostrado"),
         ("name_ok", "2/3 — nombre confirmado"),
         ("armed", "3/3 — re-autenticado (armado)"),
         ("consumed", "Ejecutado"),
         ("cancelled", "Cancelado")],
        string="Paso", default="impact", readonly=True, required=True)
    token_hash = fields.Char(
        string="Hash del token vigente", readonly=True, copy=False,
        groups="base.group_system",
        help="Solo el HASH: el token viaja una vez al cliente y no se "
             "persiste en claro.")
    expires_at = fields.Datetime(string="Vence", readonly=True, required=True)
    # Para acciones destructivas que corren por queue_job (restore): el consume
    # emite un job_token de un solo uso que el JOB quema al arrancar — el token
    # no viaja en claro por la cola más que una vez y no es re-ejecutable.
    job_token_hash = fields.Char(
        string="Hash del job token", readonly=True, copy=False,
        groups="base.group_system")
    job_token_expires = fields.Datetime(readonly=True, copy=False)

    # ------------------------------------------------------------------
    @api.model
    def _hash(self, token):
        return hashlib.sha256(("pcm-destruction:%s" % token).encode()).hexdigest()

    @api.model
    def _new_token(self, ticket):
        token = secrets.token_urlsafe(24)
        ticket.sudo().write({
            "token_hash": self._hash(token),
            "expires_at": fields.Datetime.now()
            + timedelta(minutes=TICKET_TTL_MINUTES),
        })
        return token

    def _check_token(self, token, expected_step):
        """Valida token + paso + vigencia + dueño. Un fallo audita y corta."""
        self.ensure_one()
        me = self.sudo()
        why = None
        if me.step != expected_step:
            why = _("el ticket está en el paso «%s», no en «%s»",
                    me.step, expected_step)
        elif me.user_id.id != self.env.uid:
            why = _("el ticket pertenece a otro usuario")
        elif fields.Datetime.now() > me.expires_at:
            why = _("el token venció (%s min)", TICKET_TTL_MINUTES)
        elif not token or not hmac.compare_digest(
                me.token_hash or "", self._hash(token)):
            why = _("el token no es válido (¿reusado o de otro recurso?)")
        if why:
            self._audit("failed", _("Validación de token fallida: %s", why))
            raise UserError(_(
                "Confirmación destructiva rechazada: %s. Empezá de nuevo "
                "desde el diálogo de impacto.", why))
        return True

    def _audit(self, result, detail):
        self.ensure_one()
        me = self.sudo()
        durable_log(self.env, {
            "action_type": "destruction_flow",
            "name": _("Destrucción %(action)s de %(name)s — %(detail)s",
                      action=me.action, name=me.resource_name, detail=detail),
            "result": result,
            "user_id": self.env.uid,
            "resource_model": me.res_model,
            "resource_id": me.res_id,
            "error_message": False if result == "success" else detail,
        })

    # ------------------------------------------------------------------
    @api.model
    def _require_group(self):
        if not self.env.user.has_group(GROUP_DESTRUCTIVE):
            raise AccessError(_(
                "Las operaciones destructivas requieren el grupo «PCM: "
                "operaciones destructivas», que se asigna a mano. Ni los "
                "administradores lo tienen por defecto."))

    @api.model
    def _target(self, res_model, res_id):
        record = self.env[res_model].browse(res_id).exists()
        if not record:
            raise UserError(_("El recurso a destruir ya no existe."))
        return record

    @api.model
    def destruction_start(self, res_model, res_id, action):
        """Paso 1: arma el ticket y devuelve el IMPACTO + token_1."""
        self._require_group()
        record = self._target(res_model, res_id)
        # Un recurso importado NUNCA llega a tener ticket: doble cerrojo.
        if hasattr(record, "pcm_is_imported") and record.pcm_is_imported():
            raise UserError(_(
                "«%s» es un recurso IMPORTADO (solo lectura): las acciones "
                "destructivas están deshabilitadas sin excepción, también "
                "para el grupo destructivo.", record.display_name))
        impact = record._destruction_impact(action) \
            if hasattr(record, "_destruction_impact") else {}
        ticket = self.sudo().create({
            "user_id": self.env.uid, "res_model": res_model, "res_id": res_id,
            "action": action,
            "resource_name": impact.get("resource_name")
            or record.display_name,
            "expires_at": fields.Datetime.now()
            + timedelta(minutes=TICKET_TTL_MINUTES),
        })
        token = self._new_token(ticket)
        ticket._audit("partial", _("Paso 1/3: impacto mostrado"))
        return {"ticket_id": ticket.id, "token": token, "impact": impact,
                "ttl_minutes": TICKET_TTL_MINUTES}

    @api.model
    def destruction_confirm_name(self, ticket_id, token, typed_name):
        """Paso 2: el nombre EXACTO escrito a mano, comparado EN EL SERVIDOR."""
        self._require_group()
        ticket = self.browse(int(ticket_id)).exists()
        if not ticket:
            raise UserError(_("Ticket inexistente."))
        ticket._check_token(token, "impact")
        if (typed_name or "").strip() != ticket.sudo().resource_name:
            ticket._audit("failed", _(
                "Paso 2/3: el nombre tipeado no coincide"))
            raise UserError(_(
                "El nombre no coincide: escribí exactamente «%s».",
                ticket.sudo().resource_name))
        ticket.sudo().step = "name_ok"
        token = self._new_token(ticket)
        ticket._audit("partial", _("Paso 2/3: nombre confirmado"))
        return {"ticket_id": ticket.id, "token": token}

    @api.model
    def destruction_reauth(self, ticket_id, token, password, totp_code=None):
        """Paso 3: contraseña (y TOTP si el usuario tiene 2FA) → token FINAL."""
        self._require_group()
        ticket = self.browse(int(ticket_id)).exists()
        if not ticket:
            raise UserError(_("Ticket inexistente."))
        ticket._check_token(token, "name_ok")
        user = self.env.user
        try:
            user._check_credentials(
                {"type": "password", "password": password or ""}, {
                    "interactive": True})
        except Exception:  # noqa: BLE001 — cualquier fallo = credencial mala
            ticket._audit("failed", _("Paso 3/3: contraseña incorrecta"))
            raise UserError(_("Contraseña incorrecta."))
        if getattr(user.sudo(), "totp_enabled", False):
            try:
                user.sudo()._totp_check(int(totp_code or 0))
            except Exception:  # noqa: BLE001
                ticket._audit("failed", _("Paso 3/3: TOTP inválido"))
                raise UserError(_("Código TOTP inválido."))
        ticket.sudo().step = "armed"
        token = self._new_token(ticket)
        ticket._audit("partial", _("Paso 3/3: re-autenticado — ARMADO"))
        return {"ticket_id": ticket.id, "token": token}

    @api.model
    def destruction_cancel(self, ticket_id):
        ticket = self.browse(int(ticket_id)).exists()
        if ticket and ticket.sudo().step not in ("consumed", "cancelled"):
            ticket._audit("partial", _(
                "Cancelado por el usuario en el paso «%s»", ticket.step))
            ticket.sudo().step = "cancelled"
        return True

    # ------------------------------------------------------------------
    @api.model
    def consume(self, record, action, ticket_id, token):
        """El cerrojo final: lo llama el MÉTODO destructivo antes de actuar.

        Valida grupo + ticket armado + token final + que el ticket sea de
        ESTE usuario, ESTE recurso y ESTA acción, y lo consume (un solo uso).
        Sin esto no hay destrucción: ni por RPC, ni desde un job, ni desde
        un cron."""
        self._require_group()
        ticket = self.browse(int(ticket_id or 0)).exists()
        if not ticket:
            raise UserError(_(
                "Acción destructiva sin ticket: completá la triple "
                "confirmación primero."))
        ticket._check_token(token, "armed")
        me = ticket.sudo()
        if (me.res_model, me.res_id, me.action) != (
                record._name, record.id, action):
            ticket._audit("failed", _(
                "Token de OTRO recurso/acción (%(m)s,%(i)s,%(a)s)",
                m=record._name, i=record.id, a=action))
            raise UserError(_(
                "El ticket no corresponde a este recurso/acción: empezá la "
                "confirmación de nuevo."))
        me.step = "consumed"
        me.token_hash = False
        ticket._audit("success", _("Confirmación completa: acción ejecutada"))
        return True

    @api.model
    def consume_for_job(self, record, action, ticket_id, token,
                        job_ttl_minutes=20):
        """Como :meth:`consume`, pero la acción corre por queue_job: emite un
        ``job_token`` de un solo uso que el job debe quemar al arrancar."""
        self.consume(record, action, ticket_id, token)
        ticket = self.browse(int(ticket_id)).sudo()
        job_token = secrets.token_urlsafe(24)
        ticket.write({
            "job_token_hash": self._hash(job_token),
            "job_token_expires": fields.Datetime.now()
            + timedelta(minutes=job_ttl_minutes),
        })
        return job_token

    @api.model
    def burn_job_token(self, record, action, ticket_id, job_token):
        """Lo llama el JOB destructivo al arrancar. Un solo uso, vencimiento
        corto, atado a recurso+acción: un job re-encolado o un cron no pueden
        re-ejecutar la destrucción."""
        # El token llega como referencia al depósito de secretos (nunca en
        # claro en queue_job). Si ya fue purgado, cae en «job token inválido».
        try:
            job_token = self.env["primate.cloud.job.secret"].reveal(job_token)
        except UserError:
            job_token = None
        ticket = self.browse(int(ticket_id or 0)).exists().sudo()
        why = None
        if not ticket or ticket.step != "consumed":
            why = _("ticket inexistente o sin confirmación completa")
        elif (ticket.res_model, ticket.res_id, ticket.action) != (
                record._name, record.id, action):
            why = _("el ticket no corresponde a este recurso/acción")
        elif not ticket.job_token_hash:
            why = _("el job token ya fue usado")
        elif fields.Datetime.now() > (ticket.job_token_expires
                                      or fields.Datetime.now()):
            why = _("el job token venció")
        elif not hmac.compare_digest(ticket.job_token_hash,
                                     self._hash(job_token or "")):
            why = _("job token inválido")
        if why:
            if ticket:
                ticket._audit("failed", _("Job destructivo rechazado: %s", why))
            raise UserError(_(
                "El job destructivo no arranca: %s.", why))
        ticket.job_token_hash = False
        ticket._audit("success", _("Job destructivo autorizado y ejecutando"))
        return True

    @api.model
    def _gc_expired(self):
        """Cron liviano: los tickets vencidos sin consumir quedan auditados."""
        stale = self.sudo().search([
            ("step", "in", ("impact", "name_ok", "armed")),
            ("expires_at", "<", fields.Datetime.now())])
        for ticket in stale:
            ticket._audit("partial", _("Vencido sin completar (paso «%s»)",
                                       ticket.step))
            ticket.step = "cancelled"
        return True
