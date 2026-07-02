# -*- coding: utf-8 -*-
"""Repositorio Git de un entorno: trazabilidad de commits y módulos (Fase 5).

Cruza el historial remoto (GitHub API) y el estado real del servidor (commit
desplegado y módulos en disco/base, vía SSM) para detectar desfasajes.
"""
import json
import logging
import shlex

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import github_api
from ..tools import bus, crypto

_logger = logging.getLogger(__name__)

# Máscara mostrada en lugar del token (nunca se devuelve en claro).
TOKEN_MASK = "********"

# Cantidad de commits que se traen por sincronización.
COMMITS_PER_SYNC = 50

# Script remoto (python) que escanea los módulos Odoo bajo una ruta y emite JSON.
# Se corre vía SSM; lee cada __manifest__.py con ast.literal_eval (sin ejecutar).
_SCAN_MODULES_PY = r"""
import ast, json, os
base = {path!r}
mods = []
for root, dirs, files in os.walk(base):
    if "__manifest__.py" in files:
        try:
            with open(os.path.join(root, "__manifest__.py")) as fh:
                data = ast.literal_eval(fh.read())
        except Exception:
            data = {{}}
        if not isinstance(data, dict):
            data = {{}}
        mods.append({{
            "technical_name": os.path.basename(root),
            "functional_name": data.get("name"),
            "author": data.get("author"),
            "category": data.get("category"),
            "version": data.get("version"),
            "depends": ",".join(data.get("depends") or []),
        }})
        dirs[:] = []  # los módulos no se anidan: no descender más
print(json.dumps(mods))
"""


class PrimateCloudRepository(models.Model):
    """Repositorio Git asociado a un entorno, con historial y módulos."""

    _name = "primate.cloud.repository"
    _description = "Repositorio Git"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno",
        required=True,
        ondelete="cascade",
        index=True,
    )
    repo_type = fields.Selection(
        [
            ("odoo_core", "Odoo Core"),
            ("odoo_enterprise", "Odoo Enterprise"),
            ("oca", "OCA"),
            ("custom_primate", "Custom Primate"),
            ("custom_client", "Custom Cliente"),
        ],
        string="Tipo",
        default="custom_primate",
    )
    github_url = fields.Char(string="URL GitHub", help="Ej.: https://github.com/odoo/odoo")
    organization = fields.Char(string="Organización")
    configured_branch = fields.Char(string="Rama configurada", help="Ej.: 19.0, main")
    local_path = fields.Char(string="Ruta local", help="Ruta en la EC2 (ej.: /opt/odoo/addons).")
    current_commit = fields.Char(string="Commit actual", help="Hash desplegado en el servidor.")
    latest_commit = fields.Char(string="Último commit", help="Último hash en la rama remota.")
    sync_state = fields.Selection(
        [
            ("unknown", "Sin verificar"),
            ("updated", "Actualizado"),
            ("outdated", "Desactualizado"),
            ("divergent", "Divergente"),
            ("error", "Error"),
        ],
        string="Estado de sincronización",
        default="unknown",
        tracking=True,
    )
    last_sync_date = fields.Datetime(string="Última verificación", readonly=True)
    commit_ids = fields.One2many("primate.cloud.commit", "repository_id", string="Commits")
    module_ids = fields.One2many("primate.cloud.module", "repository_id", string="Módulos")
    commit_count = fields.Integer(string="Nº de commits", compute="_compute_counts")
    module_count = fields.Integer(string="Nº de módulos", compute="_compute_counts")

    # --- Token de acceso (repos privados): columna cifrada + campo de UI ---
    github_token_encrypted = fields.Char(
        string="Token GitHub (cifrado)",
        copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )
    github_token = fields.Char(
        string="Token GitHub",
        compute="_compute_github_token",
        inverse="_inverse_github_token",
        groups="primate_cloud_manager.group_cloud_admin",
        help="Personal Access Token para repos privados (opcional). Se guarda cifrado.",
    )

    @api.depends("commit_ids", "module_ids")
    def _compute_counts(self):
        for repo in self:
            repo.commit_count = len(repo.commit_ids)
            repo.module_count = len(repo.module_ids)

    # ------------------------------------------------------------------
    # Token cifrado (reusa la clave Fernet de la cuenta)
    # ------------------------------------------------------------------
    def _encryption_key(self):
        """Clave Fernet compartida con el cifrado de credenciales de la cuenta."""
        return self.env["primate.cloud.account"]._get_encryption_key()

    @api.depends("github_token_encrypted")
    def _compute_github_token(self):
        """Enmascara el token: nunca se devuelve en claro a la UI."""
        for repo in self:
            repo.github_token = TOKEN_MASK if repo.github_token_encrypted else False

    def _inverse_github_token(self):
        """Cifra y almacena el token ingresado (ignora la máscara)."""
        key = self._encryption_key()
        for repo in self:
            value = repo.github_token
            if value and value != TOKEN_MASK:
                repo.github_token_encrypted = crypto.encrypt(key, value)

    def _get_github_token(self):
        """Devuelve el token descifrado (o None). Nunca se loguea."""
        self.ensure_one()
        encrypted = self.sudo().github_token_encrypted
        return crypto.decrypt(self._encryption_key(), encrypted) or None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, action_type, result="success", error_message=None, name=None):
        """Atajo para registrar en la bitácora sobre este repositorio."""
        return self.env["primate.cloud.operation.log"].log_operation(
            action_type, name=name, record=self, result=result,
            error_message=error_message,
        )

    def _get_target_instance(self):
        """Devuelve la EC2 del entorno sobre la que correr SSM (la primera).

        Raises:
            UserError: si el entorno no tiene ninguna instancia EC2 asociada.
        """
        self.ensure_one()
        instance = self.environment_id.ec2_instance_ids[:1]
        if not instance:
            raise UserError(
                _("El entorno «%s» no tiene una instancia EC2 asociada para ejecutar SSM.")
                % self.environment_id.display_name
            )
        return instance

    def _repo_slug(self):
        """Deriva ``owner/repo`` de la URL configurada."""
        self.ensure_one()
        return github_api.GithubApiService.parse_repo_slug(
            self.github_url, self.organization
        )

    # ------------------------------------------------------------------
    # Sincronización de commits (GitHub API)
    # ------------------------------------------------------------------
    def action_sync_commits(self):
        """Encola la sincronización del historial de commits desde GitHub."""
        for repo in self:
            repo.with_delay(
                description=_("Sincronizar commits: %s") % repo.name
            ).job_sync_commits()
        return self._notify(_("Sincronización de commits encolada."))

    def job_sync_commits(self):
        """Job: trae commits desde GitHub y hace upsert; fija el último commit."""
        self.ensure_one()
        try:
            slug = self._repo_slug()
            service = github_api.GithubApiService(token=self._get_github_token())
            commits = service.list_commits(
                slug, branch=self.configured_branch or None, limit=COMMITS_PER_SYNC
            )
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.sync_state = "error"
            self.message_post(body=_("Error al sincronizar commits: %s") % error)
            self._log("commit_sync", result="failed", error_message=str(error),
                      name=_("Sincronizar commits: %s") % self.name)
            bus.toast(self.env, _("Error al sincronizar commits de «%(n)s»: %(e)s")
                      % {"n": self.name, "e": error}, title=_("Commits"),
                      ntype="danger", sticky=True, reload=True)
            return False

        created = self._upsert_commits(commits)
        if commits:
            self.latest_commit = commits[0]["commit_hash"]
        self.last_sync_date = fields.Datetime.now()
        self._recompute_sync_state()
        msg = _("Commits sincronizados: %(t)s (%(n)s nuevos).") % {"t": len(commits), "n": created}
        self.message_post(body=msg)
        self._log("commit_sync", name=_("Sincronizar commits: %s") % self.name)
        bus.toast(self.env, msg, title=_("Commits: %s") % self.name,
                  ntype="success", reload=True)
        return True

    def _upsert_commits(self, commits):
        """Crea/actualiza registros de commit. Devuelve cuántos se crearon."""
        Commit = self.env["primate.cloud.commit"]
        created = 0
        for data in commits:
            vals = {
                "message": data.get("message"),
                "author": data.get("author"),
                "commit_date": data.get("commit_date"),
                "branch": data.get("branch"),
            }
            existing = Commit.search(
                [("repository_id", "=", self.id),
                 ("commit_hash", "=", data["commit_hash"])], limit=1
            )
            if existing:
                existing.write(vals)
            else:
                vals.update({"repository_id": self.id, "commit_hash": data["commit_hash"]})
                Commit.create(vals)
                created += 1
        return created

    # ------------------------------------------------------------------
    # Estado de sincronización (commit desplegado vs remoto, vía SSM)
    # ------------------------------------------------------------------
    def action_check_sync_state(self):
        """Encola la verificación del commit desplegado en el servidor."""
        for repo in self:
            repo.with_delay(
                description=_("Verificar estado: %s") % repo.name
            ).job_check_sync_state()
        return self._notify(_("Verificación de estado encolada."))

    def job_check_sync_state(self):
        """Job: lee el commit desplegado (git rev-parse vía SSM) y calcula el estado."""
        self.ensure_one()
        if not self.local_path:
            raise UserError(_("Indicá la ruta local del repositorio en el servidor."))
        instance = self._get_target_instance()
        command = "git -C %s rev-parse HEAD" % shlex.quote(self.local_path)
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id, command, region=instance.region,
                comment="pcm check_sync: %s" % self.name,
            )
        except Exception as error:  # noqa: BLE001
            self.sync_state = "error"
            self.message_post(body=_("Error al verificar estado: %s") % error)
            self._log("repo_check", result="failed", error_message=str(error),
                      name=_("Verificar estado: %s") % self.name)
            return False

        if output.get("status") != "Success":
            self.sync_state = "error"
            self.message_post(body=_("git rev-parse falló: %s") % (output.get("stderr") or output.get("status")))
            self._log("repo_check", result="failed",
                      error_message=output.get("stderr") or output.get("status"),
                      name=_("Verificar estado: %s") % self.name)
            return False

        self.current_commit = (output.get("stdout") or "").strip() or False
        self._mark_current_commit()
        self._recompute_sync_state()
        self.last_sync_date = fields.Datetime.now()
        msg = _("Commit desplegado: %s — estado: %s.") % (self.current_commit or "?", self.sync_state)
        self.message_post(body=msg)
        self._log("repo_check", name=_("Verificar estado: %s") % self.name)
        bus.toast(self.env, msg, title=_("Estado: %s") % self.name,
                  ntype="success", reload=True)
        return True

    def _mark_current_commit(self):
        """Marca is_current en el commit que coincide con current_commit."""
        self.commit_ids.filtered("is_current").is_current = False
        if self.current_commit:
            match = self.commit_ids.filtered(
                lambda c: c.commit_hash == self.current_commit
            )
            match.is_current = True

    def _recompute_sync_state(self):
        """Calcula sync_state cruzando commit actual, último y historial conocido.

        - updated: el commit desplegado es el último remoto.
        - outdated: el commit desplegado es un commit remoto anterior.
        - divergent: el commit desplegado no está en el historial remoto
          (cambios manuales en el servidor no trackeados).
        - unknown: falta información para decidir.
        """
        self.ensure_one()
        if self.sync_state == "error":
            return
        if not self.current_commit or not self.latest_commit:
            self.sync_state = "unknown"
            return
        if self.current_commit == self.latest_commit:
            self.sync_state = "updated"
        elif self.current_commit in set(self.commit_ids.mapped("commit_hash")):
            self.sync_state = "outdated"
        else:
            self.sync_state = "divergent"

    # ------------------------------------------------------------------
    # Detección de módulos (disco vía SSM + cruce contra la base remota)
    # ------------------------------------------------------------------
    def action_detect_modules(self):
        """Encola la detección de módulos del repositorio y su cruce con la base."""
        for repo in self:
            repo.with_delay(
                description=_("Detectar módulos: %s") % repo.name
            ).job_detect_modules()
        return self._notify(_("Detección de módulos encolada."))

    def job_detect_modules(self):
        """Job: escanea los manifests del repo (SSM) y cruza con ir_module_module."""
        self.ensure_one()
        if not self.local_path:
            raise UserError(_("Indicá la ruta local del repositorio en el servidor."))
        instance = self._get_target_instance()
        ssm = instance._get_ssm_service()
        try:
            modules = self._scan_modules(ssm, instance)
            db_states = self._fetch_db_module_states(ssm, instance)
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Error al detectar módulos: %s") % error)
            self._log("module_detect", result="failed", error_message=str(error),
                      name=_("Detectar módulos: %s") % self.name)
            bus.toast(self.env, _("Error al detectar módulos de «%(n)s»: %(e)s")
                      % {"n": self.name, "e": error}, title=_("Módulos"),
                      ntype="danger", sticky=True, reload=True)
            return False

        created = self._upsert_modules(modules, db_states)
        self.last_sync_date = fields.Datetime.now()
        msg = _("Módulos detectados: %(t)s (%(n)s nuevos).") % {"t": len(modules), "n": created}
        self.message_post(body=msg)
        self._log("module_detect", name=_("Detectar módulos: %s") % self.name)
        bus.toast(self.env, msg, title=_("Módulos: %s") % self.name,
                  ntype="success", reload=True)
        return True

    def _scan_modules(self, ssm, instance):
        """Corre el escáner remoto y devuelve la lista de módulos del disco."""
        script = "python3 - <<'PCMEOF'\n%s\nPCMEOF" % _SCAN_MODULES_PY.format(
            path=self.local_path
        )
        output = ssm.run_script(
            instance.aws_instance_id, script, region=instance.region,
            comment="pcm scan_modules: %s" % self.name,
        )
        if output.get("status") != "Success":
            raise UserError(_("El escaneo de módulos falló: %s")
                            % (output.get("stderr") or output.get("status")))
        return json.loads((output.get("stdout") or "[]").strip() or "[]")

    def _fetch_db_module_states(self, ssm, instance):
        """Consulta ir_module_module de la base remota. Devuelve {name: (state, ver)}.

        Si no hay una base asociada al entorno, devuelve {} (sin cruce: los
        módulos quedan como 'not_found').
        """
        database = self.environment_id.database_ids[:1]
        if not database or not database.name:
            return {}
        query = ("SELECT name, state, latest_version FROM ir_module_module")
        command = "sudo -u postgres psql -d %s -tAF'|' -c %s" % (
            shlex.quote(database.name), shlex.quote(query),
        )
        output = ssm.run_script(
            instance.aws_instance_id, command, region=instance.region,
            comment="pcm db_modules: %s" % self.name,
        )
        if output.get("status") != "Success":
            # No es fatal: se reporta y se sigue sin cruce.
            self.message_post(body=_("No se pudo leer ir_module_module: %s")
                              % (output.get("stderr") or output.get("status")))
            return {}
        states = {}
        for line in (output.get("stdout") or "").splitlines():
            parts = line.split("|")
            if len(parts) >= 2 and parts[0].strip():
                states[parts[0].strip()] = (
                    parts[1].strip(),
                    parts[2].strip() if len(parts) > 2 else "",
                )
        return states

    def _upsert_modules(self, modules, db_states):
        """Crea/actualiza registros de módulo cruzando el estado en base."""
        Module = self.env["primate.cloud.module"]
        now = fields.Datetime.now()
        created = 0
        for data in modules:
            tech = data.get("technical_name")
            if not tech:
                continue
            db_state, db_version = db_states.get(tech, (None, ""))
            vals = {
                "functional_name": data.get("functional_name"),
                "author": data.get("author"),
                "category": data.get("category"),
                "version": data.get("version"),
                "depends": data.get("depends"),
                "db_state": self._map_db_state(db_state),
                "db_version": db_version,
                "last_sync_date": now,
            }
            existing = Module.search(
                [("repository_id", "=", self.id), ("technical_name", "=", tech)], limit=1
            )
            if existing:
                existing.write(vals)
            else:
                vals.update({"repository_id": self.id, "technical_name": tech})
                Module.create(vals)
                created += 1
        return created

    @staticmethod
    def _map_db_state(db_state):
        """Mapea el state de ir_module_module al campo del modelo."""
        mapping = {
            "installed": "installed",
            "uninstalled": "uninstalled",
            "to upgrade": "to_upgrade",
            "to install": "to_install",
            "to remove": "to_remove",
        }
        if db_state is None:
            return "not_found"
        return mapping.get(db_state, "uninstalled")

    # ------------------------------------------------------------------
    def _notify(self, message):
        """Notificación no bloqueante para los botones de acción."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info", "message": message,
                       "next": {"type": "ir.actions.act_window_close"}},
        }
