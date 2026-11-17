# -*- coding: utf-8 -*-
"""Deployment: operación que modifica repos o módulos de un entorno (Fase 6).

Tipos: pull / checkout de rama / checkout de commit / actualización de módulos /
reinicio de servicios. Todo corre async (queue_job) vía SSM, registrando commit
origen/destino, el log completo y dejando traza en la bitácora. Los deploys
sobre git pueden revertirse (checkout al commit origen).
"""
import base64
import logging
import shlex

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import bus

from .primate_cloud_destruction import exigir_primera_corrida

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
        self.environment_id._pcm_assert_env_not_imported("deploy")
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
        self.environment_id._pcm_assert_env_not_imported("deploy")
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
        exigir_primera_corrida(self, _('Deploy'))
        self.environment_id._pcm_assert_env_not_imported("deploy")
        self.write({"state": "running", "execution_date": fields.Datetime.now()})
        instance = self._get_target_instance()
        script = self._build_deploy_script()
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id, script, region=instance.region,
                comment="pcm deploy: %s" % self.name, timeout=1200,
                check=False,   # el except cubre SSM caído; status+centinelas → estado
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
        # Un checkout de rama exitoso ES el cambio de rama del repo: la ficha
        # refleja la rama que quedó desplegada (fetch+checkout, sin reinstalar).
        if (self.deployment_type == "checkout_branch" and self.repository_id
                and self.target_branch):
            self.repository_id.configured_branch = self.target_branch
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

    def _auto_update_lines(self, target):
        """Bloque shell de detección + ``-u`` de módulos con versión nueva.

        Compara, módulo por módulo del clon (primer nivel con manifest), la
        ``version`` del ``__manifest__.py`` recién traído contra la
        ``latest_version`` instalada en la base. Deja la lista en
        ``$CAMBIADOS`` (vacía = nada que actualizar) y corre el ``-u`` con
        ``--no-http`` + log propio. Sin base resoluble, la detección se
        omite y el deploy queda como siempre (también degrada a vacío si
        psql falla — p. ej. base RDS sin postgres local).
        """
        self.ensure_one()
        try:
            db = self._db_name()
        except UserError:
            return ['CAMBIADOS=""']
        repo_path = self.repository_id.local_path or ""
        qdb = shlex.quote(db)
        qpath = shlex.quote(repo_path)
        py = shlex.quote(target.python_bin or "/opt/odoo/venv/bin/python3")
        odoo_bin = shlex.quote(target.odoo_bin or "/opt/odoo/odoo/odoo-bin")
        conf = shlex.quote(target.conf_path or "/etc/odoo/odoo.conf")
        deteccion = "\n".join([
            "import ast, os, sys",
            "ruta = sys.argv[1]",
            "inst = {}",
            'for linea in os.environ.get("INSTALADOS", "").splitlines():',
            '    if "|" in linea:',
            '        n, v = linea.split("|", 1)',
            "        inst[n] = v",
            "cambiados = []",
            "try:",
            "    hijos = sorted(os.listdir(ruta))",
            "except OSError:",
            "    hijos = []",
            "for d in hijos:",
            '    mf = os.path.join(ruta, d, "__manifest__.py")',
            "    if d in inst and os.path.isfile(mf):",
            "        try:",
            "            datos = ast.literal_eval(open(mf).read())",
            "        except Exception:",
            "            continue",
            '        version = str(datos.get("version") or "")',
            "        if version and version != inst[d]:",
            "            cambiados.append(d)",
            'print(" ".join(cambiados))',
        ])
        return [
            # psql puede no existir/fallar (BD remota): la detección degrada
            # a vacío y el deploy sigue como siempre.
            'INSTALADOS=$(sudo -u postgres psql -d %s -At -F"|" -c '
            '"select name, coalesce(latest_version,\'\') from '
            'ir_module_module where state=\'installed\'" 2>/dev/null '
            '|| true)' % qdb,
            "export INSTALADOS",
            "CAMBIADOS=$(python3 - %s <<'PYU'\n%s\nPYU\n) || CAMBIADOS=\"\""
            % (qpath, deteccion),
            'MODS=$(echo "$CAMBIADOS" | tr " " ",")',
        ] + self._lineas_update_detenido(
            shlex.quote(target.service_name or "odoo"),
            'sudo -u odoo %s %s -c %s -d %s -u "$MODS" --no-http '
            "--stop-after-init >>/var/log/pcm-deploy-update.log 2>&1"
            % (py, odoo_bin, conf, qdb),
            bool(target.is_self),
            guardia='[ -n "$CAMBIADOS" ]',
            avisos=('echo "PCM_UPDATE:$CAMBIADOS"',))

    @staticmethod
    def _lineas_update_detenido(service, update_cmd, is_self, guardia=None,
                                avisos=()):
        """``stop → -u → start``: el DDL del -u JAMÁS compite con el server
        vivo (lección 2026-11-17: ni 120 s de lock_timeout alcanzan contra
        jobs de 20 s encadenados + el panel refrescando — LockNotAvailable
        intermitente). El servicio SIEMPRE vuelve a arrancar (falle o no el
        -u) y el fallo recién entonces corta el deploy. Para la instancia
        self, el ciclo entero va DIFERIDO en una unit transitoria (el job
        reporta antes de que PCM se apague) con marcador PCM_UPDATE_DIFERIDO.
        ``guardia``: condición shell que gatilla la ejecución (el archivo se
        escribe siempre; sin guardia, se ejecuta siempre).
        """
        # Heredoc SIN comillas: "$MODS" se expande al escribir el archivo;
        # \\$PCM_U_OK queda literal para la ejecución.
        interno = ("cat > /var/tmp/pcm_update_deploy.sh <<PCMUEOF\n"
                   "systemctl stop %s\n"
                   "if %s; then PCM_U_OK=1; else PCM_U_OK=0; fi\n"
                   "systemctl start %s\n"
                   '[ "\\$PCM_U_OK" = 1 ] || exit 1\n'
                   "PCMUEOF" % (service, update_cmd, service))
        pasos = list(avisos)
        if is_self:
            pasos.append('echo "PCM_UPDATE_DIFERIDO"')
            pasos.append("sudo systemd-run --collect --on-active=5 "
                         "bash /var/tmp/pcm_update_deploy.sh")
        else:
            pasos.append("bash /var/tmp/pcm_update_deploy.sh")
        accion = "; ".join(pasos)
        if guardia:
            accion = "if %s; then %s; fi" % (guardia, accion)
        return [interno, accion]

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

        def reinicio(unit):
            # Instancia que hospeda a ESTE PCM: el restart va desacoplado y
            # con delay (systemd-run) para que el comando TERMINE y PCM
            # alcance a registrar el resultado antes de reiniciarse a sí
            # mismo (si no, el job muere a mitad del poll).
            if target.is_self:
                return ("sudo systemd-run --collect --on-active=10 "
                        "systemctl restart %s" % unit)
            return "sudo systemctl restart %s" % unit

        if deploy_type in GIT_TYPES:
            path = shlex.quote(self.repository_id.local_path or "")
            lines = ["set -e", "cd %s" % path]
            # El git corre como «odoo» (dueño del clone: git rechaza operar
            # repos de otro dueño) y, si el repo tiene token, va por askpass
            # temporal — NUNCA en la URL/git-config/argv (misma técnica que
            # el clone), con trap que lo borra pase lo que pase.
            token = self.repository_id._get_github_token()
            if token:
                token_b64 = base64.b64encode(
                    token.encode("utf-8")).decode("ascii")
                lines += [
                    'TF=$(mktemp); AK=$(mktemp)',
                    'trap \'rm -f "$TF" "$AK"\' EXIT',
                    "printf '%%s' %s | base64 -d > \"$TF\""
                    % shlex.quote(token_b64),
                    'printf \'#!/bin/sh\\ncat "%s"\\n\' "$TF" > "$AK"',
                    'chown odoo:odoo "$TF" "$AK"; chmod 600 "$TF"; '
                    'chmod 700 "$AK"',
                    'GITRUN="sudo -u odoo env GIT_ASKPASS=$AK '
                    'GIT_TERMINAL_PROMPT=0 git -c safe.directory=%s"' % path,
                ]
            else:
                lines.append(
                    'GITRUN="sudo -u odoo env GIT_TERMINAL_PROMPT=0 '
                    'git -c safe.directory=%s"' % path)
            lines += ['echo "PCM_ORIGIN:$($GITRUN rev-parse HEAD)"',
                      "$GITRUN fetch --all --prune"]
            if deploy_type == "pull":
                lines.append("$GITRUN pull --ff-only")
            elif deploy_type == "checkout_branch":
                lines += ["$GITRUN checkout %s" % shlex.quote(self.target_branch or ""),
                          "$GITRUN pull --ff-only"]
            elif deploy_type == "checkout_commit":
                lines.append("$GITRUN checkout %s" % shlex.quote(self.target_commit or ""))
            lines.append('echo "PCM_TARGET:$($GITRUN rev-parse HEAD)"')
            # -u AUTOMÁTICO tras traer código: un Pull que cambia campos o
            # versión de manifest SIN -u deja la base con columnas viejas
            # (el UndefinedColumn de pcm-self, 2026-11-17). Se detectan los
            # módulos instalados del clon cuya versión de manifest ya no
            # coincide con la instalada y se actualizan con --no-http (sin
            # eso el subproceso choca con el puerto del Odoo vivo).
            lines += self._auto_update_lines(target)
            if deploy_type in ("checkout_branch", "checkout_commit"):
                # Con cambios de módulos, el bloque del -u ya hizo su propio
                # stop→start; sin cambios, el checkout reinicia como siempre.
                lines.append('if [ -z "$CAMBIADOS" ]; then %s; fi'
                             % reinicio(service))
            # pull sin cambios: sin corte, como siempre (el bloque del -u
            # ya reinició cuando hubo módulos que actualizar).
            return "\n".join(lines)
        if deploy_type == "module_update":
            db = shlex.quote(self._db_name())
            mods = shlex.quote(self.module_names or "all")
            # --no-http: sin esto el subproceso del -u intenta bindear el
            # puerto del Odoo VIVO y muere con «Address already in use».
            return "\n".join(["set -e"] + self._lineas_update_detenido(
                service,
                "sudo -u odoo %s %s -c %s -d %s -u %s --no-http "
                "--stop-after-init >>/var/log/pcm-deploy-update.log 2>&1" % (
                    shlex.quote(target.python_bin or "/opt/odoo/venv/bin/python3"),
                    shlex.quote(target.odoo_bin or "/opt/odoo/odoo/odoo-bin"),
                    shlex.quote(target.conf_path or "/etc/odoo/odoo.conf"),
                    db, mods),
                bool(target.is_self)))
        # service_restart
        return ("set -e\n%s\n"
                "sudo systemctl restart nginx || true" % reinicio(service))

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
