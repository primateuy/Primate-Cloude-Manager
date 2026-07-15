# -*- coding: utf-8 -*-
"""Deployment: operación que modifica repos o módulos de un entorno (Fase 6).

Tipos: pull / checkout de rama / checkout de commit / actualización de módulos /
reinicio de servicios. Todo corre async (queue_job) vía SSM, registrando commit
origen/destino, el log completo y dejando traza en la bitácora. Los deploys
sobre git pueden revertirse (checkout al commit origen).
"""
import logging
import shlex

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import bus

_logger = logging.getLogger(__name__)

# Tipos de deployment (spec 8.1).
DEPLOYMENT_TYPES = [
    ("pull", "Pull"),
    ("checkout_branch", "Checkout de rama"),
    ("checkout_commit", "Checkout de commit"),
    ("module_update", "Actualización de módulos"),
    ("service_restart", "Reinicio de servicios"),
]

# Tipos que operan sobre git (capturan commit origen/destino y necesitan repo).
GIT_TYPES = {"pull", "checkout_branch", "checkout_commit"}

# Tipos considerados destructivos en producción (cambian el código desplegado).
RISKY_TYPES = {"checkout_branch", "checkout_commit", "module_update"}


class PrimateCloudDeployment(models.Model):
    """Registro y ejecución de un despliegue sobre un entorno."""

    _name = "primate.cloud.deployment"
    _description = "Deployment"
    _inherit = ["mail.thread", "primate.cloud.instance.linked"]
    _order = "execution_date desc, id desc"

    name = fields.Char(string="Referencia", required=True, readonly=True, copy=False,
                       default=lambda self: _("Nuevo"))
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno", required=True,
        ondelete="cascade", index=True,
    )
    repository_id = fields.Many2one(
        "primate.cloud.repository", string="Repositorio", ondelete="set null",
        domain="[('environment_id', '=', environment_id)]",
        help="Requerido para los tipos que operan sobre git.",
    )
    deployment_type = fields.Selection(
        DEPLOYMENT_TYPES, string="Tipo", required=True, default="pull",
    )
    origin_commit = fields.Char(string="Commit origen", readonly=True, copy=False)
    target_commit = fields.Char(string="Commit destino", copy=False)
    target_branch = fields.Char(string="Rama destino")
    module_names = fields.Char(
        string="Módulos", help="Lista separada por comas, o 'all'. Para actualización de módulos.",
    )
    triggered_by = fields.Many2one(
        "res.users", string="Ejecutado por", readonly=True, copy=False,
        default=lambda self: self.env.user,
    )
    execution_date = fields.Datetime(string="Fecha de ejecución", readonly=True, copy=False)
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("pending", "Pendiente"),
            ("running", "Ejecutando"),
            ("success", "Exitoso"),
            ("failed", "Fallido"),
            ("reverted", "Revertido"),
        ],
        string="Estado", default="draft", required=True, tracking=True, copy=False,
    )
    execution_log = fields.Text(string="Log de ejecución", readonly=True, copy=False)
    is_revert = fields.Boolean(string="Es una reversión", readonly=True, copy=False)
    revert_deployment_id = fields.Many2one(
        "primate.cloud.deployment", string="Revertido por", readonly=True, copy=False,
        help="Deploy que revirtió este.",
    )
    reverts_deployment_id = fields.Many2one(
        "primate.cloud.deployment", string="Revierte a", readonly=True, copy=False,
        help="Deploy original que esta reversión deshace.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Asigna la referencia secuencial 'Deploy #N - entorno' al crear."""
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("Nuevo"):
                seq = self.env["ir.sequence"].next_by_code("primate.cloud.deployment") or "0001"
                env = self.env["primate.cloud.environment"].browse(vals.get("environment_id"))
                vals["name"] = "Deploy #%s — %s" % (seq, env.display_name or "")
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Acciones (botón -> job)
    # ------------------------------------------------------------------
    def action_run(self):
        """Valida, marca pendiente y encola la ejecución del deploy."""
        self.ensure_one()
        self._validate()
        self.write({"state": "pending", "triggered_by": self.env.uid})
        self.with_delay(
            description=_("Deploy: %s") % self.name
        ).job_deploy()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info", "title": _("Desplegando"),
                "message": _("Deploy encolado. El estado y el log se actualizarán al terminar."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _validate(self):
        """Valida la coherencia del deploy según su tipo."""
        self.ensure_one()
        if self.state not in ("draft", "failed"):
            raise UserError(_("Solo se puede ejecutar un deploy en Borrador o Fallido."))
        if self.deployment_type in GIT_TYPES:
            if not self.repository_id:
                raise UserError(_("Este tipo de deploy requiere un repositorio."))
            if not self.repository_id.local_path:
                raise UserError(_("El repositorio no tiene ruta local en el servidor."))
        if self.deployment_type == "checkout_branch" and not self.target_branch:
            raise UserError(_("Indicá la rama destino."))
        if self.deployment_type == "checkout_commit" and not self.target_commit:
            raise UserError(_("Indicá el commit destino."))

    def action_revert(self):
        """Crea y ejecuta un deploy que revierte este (checkout al commit origen).

        Solo aplica a deploys git exitosos con commit origen conocido.
        """
        self.ensure_one()
        if self.state != "success" or self.deployment_type not in GIT_TYPES:
            raise UserError(_("Solo se pueden revertir deploys de git exitosos."))
        if not self.origin_commit:
            raise UserError(_("No hay commit origen registrado para revertir."))
        if self.revert_deployment_id:
            raise UserError(_("Este deploy ya fue revertido por %s.") % self.revert_deployment_id.name)
        revert = self.create({
            "environment_id": self.environment_id.id,
            "repository_id": self.repository_id.id,
            "deployment_type": "checkout_commit",
            "target_commit": self.origin_commit,
            "is_revert": True,
            "reverts_deployment_id": self.id,
        })
        self.revert_deployment_id = revert.id
        return revert.action_run()

    # ------------------------------------------------------------------
    # Job (queue_job)
    # ------------------------------------------------------------------
    def job_deploy(self):
        """Job: ejecuta el deploy por SSM, captura commits y log, fija el estado."""
        self.ensure_one()
        self.write({"state": "running", "execution_date": fields.Datetime.now()})
        instance = self._get_target_instance()
        script = self._build_deploy_script()
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id, script, region=instance.region,
                comment="pcm deploy: %s" % self.name, timeout=1200,
            )
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.write({"state": "failed", "execution_log": str(error)})
            self.message_post(body=_("Deploy fallido (error de ejecución): %s") % error)
            self._log(result="failed", error_message=str(error))
            bus.toast(self.env, _("Deploy «%(n)s» fallido: %(e)s") % {"n": self.name, "e": error},
                      title=_("Deploy"), ntype="danger", sticky=True, reload=True)
            return False

        log_text = self._format_log(output)
        if output.get("status") != "Success":
            self.write({"state": "failed", "execution_log": log_text})
            self.message_post(body=_("Deploy fallido (estado SSM: %s).") % output.get("status"))
            self._log(result="failed", error_message=output.get("stderr") or output.get("status"))
            bus.toast(self.env, _("Deploy «%(n)s» fallido (estado: %(s)s).")
                      % {"n": self.name, "s": output.get("status")},
                      title=_("Deploy"), ntype="danger", sticky=True, reload=True)
            return False

        origin, target = self._parse_commits(output.get("stdout"))
        vals = {"state": "success", "execution_log": log_text}
        if self.deployment_type in GIT_TYPES:
            vals["origin_commit"] = origin or self.origin_commit
            vals["target_commit"] = target or self.target_commit
        self.write(vals)
        # Reflejar el commit desplegado en el repositorio.
        if self.repository_id and (target := vals.get("target_commit")):
            self.repository_id.current_commit = target
        # Si es una reversión, marcar el deploy original como revertido.
        if self.reverts_deployment_id:
            self.reverts_deployment_id.state = "reverted"
        self.message_post(body=_("Deploy exitoso."))
        self._log(result="success")
        bus.toast(self.env, _("Deploy «%s» exitoso.") % self.name,
                  title=_("Deploy"), ntype="success", reload=True)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_target_instance(self):
        """Devuelve la EC2 del entorno sobre la que correr SSM (la primera)."""
        self.ensure_one()
        instance = self.environment_id.ec2_instance_ids[:1]
        if not instance:
            raise UserError(
                _("El entorno «%s» no tiene una instancia EC2 asociada.")
                % self.environment_id.display_name
            )
        return instance

    def _target_odoo_instance(self):
        """La instancia ODOO objetivo del deploy (R4-B1, mixin de R1).

        Prioridad: la del propio deploy → la del repo → la primaria del
        entorno. En un servidor multi-Odoo esto decide QUÉ unit se reinicia
        y con qué runtime/conf corre el ``-u`` — recablearlo acá evita el
        guard: el deploy opera la instancia correcta en ambos layouts.
        """
        self.ensure_one()
        instance = (self.instance_id or self.repository_id.instance_id
                    or self.environment_id.primary_instance_id)
        if not instance:
            raise UserError(_(
                "El deploy no tiene una instancia Odoo objetivo (ni el "
                "despliegue ni el repo ni el entorno la definen)."))
        return instance

    def _db_name(self):
        """Nombre de la base de la instancia objetivo (para -u de Odoo)."""
        self.ensure_one()
        target = self._target_odoo_instance()
        database = target.database_id or self.environment_id.database_ids[:1]
        if not database or not database.name:
            raise UserError(_("La instancia no tiene una base de datos asociada."))
        return database.name

    def _build_deploy_script(self):
        """Construye el script de shell a correr por SSM según el tipo.

        R4-B1: unit/runtime/conf salen de la INSTANCIA objetivo (las legacy
        llevan los valores viejos en sus campos → mismo script que antes).
        """
        self.ensure_one()
        deploy_type = self.deployment_type
        target = self._target_odoo_instance()
        service = shlex.quote(target.service_name or "odoo")
        if deploy_type in GIT_TYPES:
            path = shlex.quote(self.repository_id.local_path or "")
            lines = ["set -e", "cd %s" % path,
                     'echo "PCM_ORIGIN:$(git rev-parse HEAD)"', "git fetch --all --prune"]
            if deploy_type == "pull":
                lines.append("git pull --ff-only")
            elif deploy_type == "checkout_branch":
                lines += ["git checkout %s" % shlex.quote(self.target_branch or ""),
                          "git pull --ff-only"]
            elif deploy_type == "checkout_commit":
                lines.append("git checkout %s" % shlex.quote(self.target_commit or ""))
            lines.append('echo "PCM_TARGET:$(git rev-parse HEAD)"')
            if deploy_type in ("checkout_branch", "checkout_commit"):
                lines.append("sudo systemctl restart %s" % service)
            return "\n".join(lines)
        if deploy_type == "module_update":
            db = shlex.quote(self._db_name())
            mods = shlex.quote(self.module_names or "all")
            return "\n".join([
                "set -e",
                "sudo -u odoo %s %s -c %s -d %s -u %s --stop-after-init" % (
                    shlex.quote(target.python_bin or "/opt/odoo/venv/bin/python3"),
                    shlex.quote(target.odoo_bin or "/opt/odoo/odoo/odoo-bin"),
                    shlex.quote(target.conf_path or "/etc/odoo/odoo.conf"),
                    db, mods),
                "sudo systemctl restart %s" % service,
            ])
        # service_restart
        return ("set -e\nsudo systemctl restart %s\n"
                "sudo systemctl restart nginx || true" % service)

    @staticmethod
    def _parse_commits(stdout):
        """Extrae los hashes de los marcadores PCM_ORIGIN / PCM_TARGET del stdout."""
        origin = target = False
        for line in (stdout or "").splitlines():
            line = line.strip()
            if line.startswith("PCM_ORIGIN:"):
                origin = line.split(":", 1)[1].strip() or False
            elif line.startswith("PCM_TARGET:"):
                target = line.split(":", 1)[1].strip() or False
        return origin, target

    @staticmethod
    def _format_log(output):
        """Arma el texto de log a partir de la salida SSM (stdout + stderr)."""
        parts = []
        if output.get("stdout"):
            parts.append("STDOUT:\n%s" % output["stdout"].strip())
        if output.get("stderr"):
            parts.append("STDERR:\n%s" % output["stderr"].strip())
        parts.append("Estado SSM: %s" % output.get("status"))
        return "\n\n".join(parts)

    def _log(self, result="success", error_message=None):
        """Atajo para registrar el deploy en la bitácora."""
        return self.env["primate.cloud.operation.log"].log_operation(
            "deploy", name=self.name, record=self, result=result,
            error_message=error_message,
        )
