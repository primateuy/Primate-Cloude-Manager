# -*- coding: utf-8 -*-
"""Panel (dashboard) de Primate Cloud Manager.

Modelo abstracto que agrega los datos que consume el componente OWL del panel:
KPIs, últimos despliegues y alertas. No persiste nada; solo lee el inventario.
"""
from datetime import timedelta

from odoo import _, api, fields, models


def _friendly_sync_error(raw):
    """Traduce un error crudo de proveedor (AWS/Route 53, SSM, GitHub) a copy
    ACCIONABLE en español.

    La pantalla OWL no debe mostrar el traceback de boto3/SSM/PyGithub: este
    helper reconoce las firmas de fallo más comunes —permisos, credenciales,
    recurso inexistente, conflicto, throttling, red— y devuelve qué hacer al
    respecto, sin atarse a un proveedor puntual (lo reusan DNS, repos y cuentas).
    El crudo se conserva aparte (detalle técnico plegable), nunca se pierde.

    Args:
        raw (str): mensaje de error tal cual lo devolvió el proveedor.

    Returns:
        str: mensaje orientado a la acción, en español.
    """
    text = (raw or "").strip()
    low = text.lower()
    if not text:
        return _("La sincronización falló. Revisá la bitácora para el detalle.")
    if any(k in low for k in ("accessdenied", "not authorized",
                              "unauthorizedoperation", "is not authorized")):
        return _("No hay permisos para esta operación. Revisá los permisos de la "
                 "cuenta (política IAM en AWS, o el token/acceso del repositorio "
                 "en GitHub) y reintentá.")
    if any(k in low for k in ("bad credentials", "401", "invalidclienttokenid",
                              "signaturedoesnotmatch", "authfailure",
                              "expiredtoken", "expired", "invalid credentials",
                              "authentication failed")):
        return _("Las credenciales o el token son inválidos o expiraron. "
                 "Actualizalos en la cuenta o el repositorio y reintentá.")
    if "nosuchhostedzone" in low or ("hosted zone" in low and "not" in low):
        return _("La zona alojada ya no existe en Route 53. Verificá el Hosted "
                 "Zone ID o volvé a sincronizar las zonas desde AWS.")
    if any(k in low for k in ("not found", "404", "could not resolve host",
                              "repository not found", "no such")):
        return _("El recurso no existe o no es accesible (repositorio, rama o "
                 "zona). Verificá el nombre/URL y el acceso, y reintentá.")
    if any(k in low for k in ("invalidchangebatch", "already exists",
                              "it already exists", "conflict")):
        return _("El proveedor rechazó el cambio: ya existe o entra en conflicto "
                 "con otro. Verificá los datos antes de reintentar.")
    if any(k in low for k in ("throttl", "rate exceeded", "toomanyrequests",
                              "slow down", "rate limit")):
        return _("El proveedor está limitando las solicitudes (throttling / rate "
                 "limit). Esperá unos segundos y reintentá.")
    if any(k in low for k in ("timed out", "timeout", "could not connect",
                              "connection", "endpointconnectionerror",
                              "network")):
        return _("No se pudo conectar (red o timeout). Reintentá; si persiste, "
                 "revisá la conectividad de red.")
    # Genérico: primer renglón, sin volcar el traceback entero en la UI.
    return _("La sincronización falló: %s") % text.splitlines()[0]


class PrimateCloudDashboard(models.AbstractModel):
    """Fuente de datos del panel de inicio del módulo."""

    _name = "primate.cloud.dashboard"
    _description = "Panel Cloud"

    def _last_sync_error(self, record, failed=None):
        """Último error de un registro: copy accionable + crudo.

        Busca la última entrada FALLIDA de la bitácora para el recurso y traduce
        su ``error_message`` a un mensaje accionable (sin traceback).

        Args:
            record (recordset): el registro (DNS, repo, deploy, ...) a inspeccionar.
            failed (bool, optional): si se pasa, decide si el registro está en
                error (para modelos con ``state='failed'`` en vez de
                ``sync_state='error'``, como deployment). Si es None, se usa
                ``sync_state == 'error'``.

        Returns:
            dict: ``{"sync_error": str, "sync_error_detail": str}``.
        """
        is_error = (failed if failed is not None
                    else getattr(record, "sync_state", False) == "error")
        if not is_error:
            return {"sync_error": "", "sync_error_detail": ""}
        log = self.env["primate.cloud.operation.log"].sudo().search([
            ("resource_model", "=", record._name),
            ("resource_id", "=", record.id),
            ("result", "=", "failed"),
        ], order="execution_date desc, id desc", limit=1)
        raw = log.error_message or ""
        return {"sync_error": _friendly_sync_error(raw), "sync_error_detail": raw}

    @api.model
    def get_dashboard_data(self):
        """Devuelve KPIs, últimos deploys y alertas para el panel.

        Returns:
            dict: ``{"kpis": [...], "recent_deploys": [...], "alerts": [...]}``.
                Cada KPI y alerta trae los datos para abrir la vista relacionada
                al hacer click (``model`` + ``domain``).
        """
        Environment = self.env["primate.cloud.environment"]
        Ec2 = self.env["primate.cloud.ec2.instance"]
        Account = self.env["primate.cloud.account"]
        Deployment = self.env["primate.cloud.deployment"]
        Repository = self.env["primate.cloud.repository"]
        today = fields.Date.context_today(self)

        kpis = [
            {
                "key": "env_active", "label": _("Entornos activos"), "icon": "fa-cubes",
                "color": "success",
                "value": Environment.search_count([("state", "=", "active")]),
                "model": "primate.cloud.environment",
                "domain": [("state", "=", "active")],
            },
            {
                "key": "ec2_running", "label": _("EC2 corriendo"), "icon": "fa-server",
                "color": "info",
                "value": Ec2.search_count([("instance_state", "=", "running")]),
                "model": "primate.cloud.ec2.instance",
                "domain": [("instance_state", "=", "running")],
            },
            {
                "key": "accounts_connected", "label": _("Cuentas conectadas"),
                "icon": "fa-key", "color": "primary",
                "value": Account.search_count([("connection_state", "=", "connected")]),
                "model": "primate.cloud.account",
                "domain": [("connection_state", "=", "connected")],
            },
            {
                "key": "deploys_today", "label": _("Deploys de hoy"), "icon": "fa-rocket",
                "color": "warning",
                "value": Deployment.search_count([("execution_date", ">=", today)]),
                "model": "primate.cloud.deployment",
                "domain": [("execution_date", ">=", today)],
            },
        ]

        recent_deploys = [
            {
                "id": d.id, "name": d.name, "state": d.state,
                "state_label": dict(d._fields["state"].selection).get(d.state, d.state),
                "env": d.environment_id.display_name or "",
                "date": fields.Datetime.to_string(d.execution_date) or "",
            }
            for d in Deployment.search([("execution_date", "!=", False)], limit=6)
        ]

        alerts = []
        for repo in Repository.search([("sync_state", "=", "divergent")], limit=10):
            alerts.append({
                "type": "warning", "icon": "fa-code-fork",
                "text": _("Repo divergente: %s") % repo.display_name,
                "model": "primate.cloud.repository", "res_id": repo.id,
            })
        for environment in Environment.search([("state", "=", "error")], limit=10):
            alerts.append({
                "type": "danger", "icon": "fa-exclamation-triangle",
                "text": _("Entorno en error: %s") % environment.display_name,
                "model": "primate.cloud.environment", "res_id": environment.id,
            })
        for account in Account.search([("connection_state", "=", "error")], limit=10):
            alerts.append({
                "type": "danger", "icon": "fa-unlink",
                "text": _("Cuenta con conexión fallida: %s") % account.display_name,
                "model": "primate.cloud.account", "res_id": account.id,
            })
        # Fase 9: status check fallido (señal de error REAL de EC2).
        Ec2 = self.env["primate.cloud.ec2.instance"]
        for inst in Ec2.search([("last_status_check_failed", ">=", 1)], limit=10):
            alerts.append({
                "type": "danger", "icon": "fa-heartbeat",
                "text": _("Status check fallido: %s") % inst.display_name,
                "model": "primate.cloud.ec2.instance", "res_id": inst.id,
            })
        # Fase 8: entornos que NO cumplen su política de respaldo.
        for environment in Environment.search(
                [("backup_compliance", "=", "non_compliant")], limit=10):
            alerts.append({
                "type": "warning", "icon": "fa-database",
                "text": _("Respaldo no cumple: %s") % environment.display_name,
                "model": "primate.cloud.environment", "res_id": environment.id,
            })
        # Fase 8.5: registros DNS divergentes (AWS ≠ PCM).
        Dns = self.env["primate.cloud.dns.record"]
        for rec in Dns.search([("sync_state", "=", "divergent")], limit=10):
            alerts.append({
                "type": "warning", "icon": "fa-globe",
                "text": _("DNS divergente: %s") % rec.display_name,
                "model": "primate.cloud.dns.record", "res_id": rec.id,
            })

        return {"kpis": kpis, "recent_deploys": recent_deploys, "alerts": alerts}

    @api.model
    def get_home_data(self):
        """Datos de la pantalla Inicio de la app PCM (solo lectura, sin efectos).

        Returns:
            dict: ``{"kpis": [...], "recent_environments": [...]}``.
        """
        Environment = self.env["primate.cloud.environment"]
        Ec2 = self.env["primate.cloud.ec2.instance"]
        Account = self.env["primate.cloud.account"]
        Repository = self.env["primate.cloud.repository"]

        alerts = (
            Repository.search_count([("sync_state", "=", "divergent")])
            + Environment.search_count([("state", "=", "error")])
            + Account.search_count([("connection_state", "=", "error")])
            # Fase 9: status check fallido + respaldo no cumple + DNS divergente.
            + Ec2.search_count([("last_status_check_failed", ">=", 1)])
            + Environment.search_count([("backup_compliance", "=", "non_compliant")])
            + self.env["primate.cloud.dns.record"].search_count(
                [("sync_state", "=", "divergent")])
        )
        # Costo mensual GLOBAL = el CRUDO de todas las cuentas (lo que factura
        # AWS), NO la suma de shares (misma plata contada por otro lado). En un
        # mundo de servidores compartidos, el número del dashboard es la factura.
        Entry = self.env["primate.cloud.cost.entry"]
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        month_entries = Entry.search([
            ("period_start", "=", month_start),
            ("granularity", "=", "monthly"),
            ("is_forecast", "=", False),
        ])
        crudo_total = sum(month_entries.mapped("amount"))
        currency = month_entries[:1].currency or "USD"
        pulled_dates = [d for d in Account.search([]).mapped("cost_pulled_at")
                        if d]
        pulled = max(pulled_dates) if pulled_dates else None
        kpis = [
            {"key": "env_active", "label": "Entornos activos", "icon": "fa-cubes",
             "value": Environment.search_count([("state", "=", "active")])},
            {"key": "ec2_running", "label": "Servidores activos", "icon": "fa-server",
             "value": Ec2.search_count([("instance_state", "=", "running")])},
            {"key": "cost", "label": "Costo mensual (AWS)", "icon": "fa-line-chart",
             "value": ("%.2f %s" % (crudo_total, currency)
                       if month_entries else "—"),
             "hint": ("datos al %s (UTC)" % fields.Datetime.to_string(pulled)
                      if pulled else "sin datos de costo aún")},
            {"key": "alerts", "label": "Alertas", "icon": "fa-bell", "value": alerts},
        ]

        state_labels = dict(Environment._fields["state"].selection)
        type_labels = dict(self.env["primate.cloud.instance"]._fields["env_type"].selection)
        recent_environments = [
            {
                "id": environment.id,
                "name": environment.display_name,
                "project": environment.project_id.display_name or "",
                "env_type": environment.env_type,
                "env_type_label": type_labels.get(environment.env_type, ""),
                "state": environment.state,
                "state_label": state_labels.get(environment.state, environment.state),
                "main_url": environment.main_url or "",
                "odoo_version": environment.odoo_version or "",
            }
            for environment in Environment.search([], limit=8)
        ]
        return {"kpis": kpis, "recent_environments": recent_environments}

    @api.model
    def get_environments(self):
        """Serializa los entornos para la pantalla Entornos de la app (read-only).

        Returns:
            list[dict]: un dict por entorno con datos para las cards.
        """
        Environment = self.env["primate.cloud.environment"]
        state_labels = dict(Environment._fields["state"].selection)
        type_labels = dict(self.env["primate.cloud.instance"]._fields["env_type"].selection)
        edition_labels = dict(self.env["primate.cloud.instance"]._fields["odoo_edition"].selection)
        result = []
        for environment in Environment.search([]):
            result.append({
                "id": environment.id,
                "name": environment.display_name,
                "project": environment.project_id.display_name or "",
                "env_type": environment.env_type,
                "env_type_label": type_labels.get(environment.env_type, ""),
                "state": environment.state,
                "state_label": state_labels.get(environment.state, environment.state),
                "main_url": environment.main_url or "",
                "odoo_version": environment.odoo_version or "",
                "odoo_edition": edition_labels.get(environment.odoo_edition, ""),
                "ec2_count": len(environment.ec2_instance_ids),
                "db_count": len(environment.database_ids),
                "repo_count": len(environment.repository_ids),
            })
        return result

    @api.model
    def get_environment_detail(self, env_id):
        """Serializa el detalle completo de un entorno para la app (read-only).

        Incluye infra (servidores/bases) y la trazabilidad: por repositorio, los
        módulos (versión repo vs versión en base + estado) y el historial de
        commits con el commit desplegado marcado.

        Returns:
            dict: datos del entorno, o ``{}`` si no existe.
        """
        environment = self.env["primate.cloud.environment"].browse(env_id).exists()
        if not environment:
            return {}

        Env = self.env["primate.cloud.environment"]
        Ec2 = self.env["primate.cloud.ec2.instance"]
        Db = self.env["primate.cloud.database"]
        Repo = self.env["primate.cloud.repository"]
        Mod = self.env["primate.cloud.module"]
        Dns = self.env["primate.cloud.dns.record"]
        Dep = self.env["primate.cloud.deployment"]
        state_labels = dict(Env._fields["state"].selection)
        type_labels = dict(self.env["primate.cloud.instance"]._fields["env_type"].selection)
        edition_labels = dict(self.env["primate.cloud.instance"]._fields["odoo_edition"].selection)
        ec2_labels = dict(Ec2._fields["instance_state"].selection)
        db_labels = dict(Db._fields["state"].selection)
        db_type_labels = dict(Db._fields["db_type"].selection)
        sync_labels = dict(Repo._fields["sync_state"].selection)
        repo_type_labels = dict(Repo._fields["repo_type"].selection)
        mod_labels = dict(Mod._fields["db_state"].selection)
        dns_state_labels = dict(Dns._fields["state"].selection)
        dns_sync_labels = dict(Dns._fields["sync_state"].selection)
        dep_state_labels = dict(Dep._fields["state"].selection)
        dep_type_labels = dict(Dep._fields["deployment_type"].selection)
        Backup = self.env["primate.cloud.backup"]
        compliance_labels = dict(self.env["primate.cloud.instance"]._fields["backup_compliance"].selection)
        backup_state_labels = dict(Backup._fields["state"].selection)
        backup_type_labels = dict(Backup._fields["backup_type"].selection)
        purpose_labels = dict(Backup._fields["purpose"].selection)

        def repo_dict(repo):
            modules = [{
                "technical_name": m.technical_name,
                "functional_name": m.functional_name or "",
                "version": m.version or "",
                "db_version": m.db_version or "",
                "db_state": m.db_state,
                "db_state_label": mod_labels.get(m.db_state, m.db_state or ""),
            } for m in repo.module_ids]
            commits = [{
                "short": (commit.commit_hash or "")[:9],
                "message": (commit.message or "").splitlines()[0] if commit.message else "",
                "author": commit.author or "",
                "commit_date": fields.Datetime.to_string(commit.commit_date) or "",
                "is_current": commit.is_current,
            } for commit in repo.commit_ids[:25]]
            return {
                "id": repo.id, "name": repo.display_name,
                "repo_type_label": repo_type_labels.get(repo.repo_type, ""),
                "branch": repo.configured_branch or "",
                "current_commit": repo.current_commit or "",
                "latest_commit": repo.latest_commit or "",
                "sync_state": repo.sync_state,
                "sync_state_label": sync_labels.get(repo.sync_state, repo.sync_state or ""),
                "module_count": len(repo.module_ids),
                "commit_count": len(repo.commit_ids),
                "modules": modules,
                "commits": commits,
            }

        return {
            "id": environment.id,
            "name": environment.display_name,
            "project": environment.project_id.display_name or "",
            "project_id": environment.project_id.id or False,
            "account": environment.account_id.display_name or "",
            "account_id": environment.account_id.id or False,
            "env_type": environment.env_type,
            "env_type_label": type_labels.get(environment.env_type, ""),
            "state": environment.state,
            "state_label": state_labels.get(environment.state, environment.state),
            "odoo_version": environment.odoo_version or "",
            "odoo_edition": edition_labels.get(environment.odoo_edition, ""),
            "main_url": environment.main_url or "",
            "origin": environment.origin_environment_id.display_name or "",
            "origin_id": environment.origin_environment_id.id or False,
            "servers": [{
                "id": inst.id, "name": inst.display_name,
                "state": inst.instance_state,
                "state_label": ec2_labels.get(inst.instance_state, inst.instance_state or ""),
                "public_ip": inst.public_ip or "",
                "instance_type": inst.instance_type or "",
            } for inst in environment.ec2_instance_ids],
            "databases": [{
                "id": db.id, "name": db.display_name, "db_type": db.db_type,
                "db_type_label": db_type_labels.get(db.db_type, db.db_type or ""),
                "server_id": db.ec2_instance_id.id or False,
                "state": db.state,
                "state_label": db_labels.get(db.state, db.state or ""),
                "pg_version": db.pg_version or "",
            } for db in environment.database_ids],
            "dns_records": [{
                "id": rec.id, "name": rec.display_name,
                "record_type": rec.record_type,
                "record_value": rec.record_value or "",
                "state": rec.state,
                "state_label": dns_state_labels.get(rec.state, rec.state or ""),
                "sync_state": rec.sync_state,
                "sync_state_label": dns_sync_labels.get(rec.sync_state, ""),
                "record_value_aws": rec.record_value_aws or "",
                "delete_needs_ack": rec.delete_needs_ack,
                "is_deleted": rec.state == "deleted",
                "hosted_zone_id": rec.hosted_zone_id or "",
            } for rec in environment.dns_record_ids],
            "deployments": [{
                "id": dep.id, "name": dep.display_name,
                "deployment_type_label": dep_type_labels.get(dep.deployment_type, ""),
                "state": dep.state,
                "state_label": dep_state_labels.get(dep.state, dep.state or ""),
                "date": fields.Datetime.to_string(dep.execution_date) or "",
            } for dep in environment.deployment_ids[:20]],
            "repositories": [repo_dict(repo) for repo in environment.repository_ids],
            "backup": {
                "policy": environment.backup_policy_id.display_name or "",
                "policy_id": environment.backup_policy_id.id or False,
                "managed": environment.backup_policy_id.managed_by_pcm,
                "compliance": environment.backup_compliance or "no_policy",
                "compliance_label": compliance_labels.get(
                    environment.backup_compliance, ""),
                "compliance_detail": environment.backup_compliance_detail or "",
                "last_check": fields.Datetime.to_string(
                    environment.last_backup_check) or "",
            },
            # El propósito distingue la historia real de la lista (programado /
            # manual / fuente de staging / pre-restore); para el validador
            # todos cuentan igual.
            "backups": [{
                "id": backup.id, "name": backup.name,
                "date": fields.Datetime.to_string(backup.backup_date) or "",
                "database": backup.database_id.display_name or "",
                "backup_type_label": backup_type_labels.get(
                    backup.backup_type, ""),
                "purpose": backup.purpose,
                "purpose_label": purpose_labels.get(backup.purpose, ""),
                "state": backup.state,
                "state_label": backup_state_labels.get(backup.state, ""),
                "size_mb": round(backup.size_mb or 0.0, 1),
            } for backup in environment.backup_ids[:15]],
        }

    @api.model
    def get_server_detail(self, server_id):
        """Serializa el detalle de un servidor EC2 para la app (read-only)."""
        inst = self.env["primate.cloud.ec2.instance"].browse(server_id).exists()
        if not inst:
            return {}
        Ec2 = self.env["primate.cloud.ec2.instance"]
        state_labels = dict(Ec2._fields["instance_state"].selection)
        os_labels = dict(Ec2._fields["os_type"].selection)
        env = inst.environment_id
        # R4-B6: instancias hospedadas (cada una con su CLIENTE y su env_type —
        # la verdad vive en la instancia, D-B6.1) + resumen honesto. El servidor
        # NO tiene "producción": es propiedad de cada instancia.
        Inst = self.env["primate.cloud.instance"]
        env_type_labels = dict(Inst._fields["env_type"].selection)
        inst_state_labels = dict(Inst._fields["state"].selection)
        hosted = env.instance_ids.filtered(
            lambda i: i.state != "archived") if env else Inst.browse()
        env_type_counts = {}
        for i in hosted:
            env_type_counts[i.env_type] = env_type_counts.get(i.env_type, 0) + 1
        hosted_summary = " · ".join(
            "%d %s" % (n, env_type_labels.get(t, t))
            for t, n in sorted(env_type_counts.items()))
        return {
            "id": inst.id,
            "hosted_instances": [{
                "id": i.id, "name": i.name,
                "env_type": i.env_type,
                "env_type_label": env_type_labels.get(i.env_type, ""),
                "state": i.state,
                "state_label": inst_state_labels.get(i.state, i.state or ""),
                "project_id": i.project_id.id,
                "project_name": i.project_id.display_name or "",
                "main_url": i.main_url or "",
                "http_port": i.http_port or 0,
            } for i in hosted],
            "hosted_summary": hosted_summary,
            "hosts_production": bool(env_type_counts.get("production")),
            "name": inst.display_name,
            "state": inst.instance_state,
            "state_label": state_labels.get(inst.instance_state, inst.instance_state or ""),
            "aws_instance_id": inst.aws_instance_id or "",
            "instance_type": inst.instance_type or "",
            "region": inst.region or "",
            "os_type": os_labels.get(inst.os_type, "") if inst.os_type else "",
            "public_ip": inst.public_ip or "",
            "private_ip": inst.private_ip or "",
            "disk_size_gb": inst.disk_size_gb or 0,
            "provisioned_by_pcm": inst.provisioned_by_pcm,
            "aws_created_at": fields.Datetime.to_string(inst.aws_created_at) or "",
            "last_sync_date": fields.Datetime.to_string(inst.last_sync_date) or "",
            "account_id": inst.account_id.id,
            "account_name": inst.account_id.display_name or "",
            "environment_id": env.id,
            "environment_name": env.display_name or "",
            "environment_real_name": env.name or "" if env else "",
            # R4-B6.3 (D-B6.1): el servidor NO tiene env_type/is_production ni
            # config/backups/repos/runtime — esos son de cada INSTANCIA y
            # viven en get_odoo_instance_detail. Acá solo la MÁQUINA + la
            # lista de instancias hospedadas (arriba). Se retiraron:
            # is_production, main_url, primary_instance_id, runtime,
            # backup_compliance*, databases, backups, repositories.
            # Métricas (Fase 9): las que PCM tiene sin agente + marca de las
            # que REQUIEREN agente (RAM/disco), para que la UI no muestre 0.
            "metrics": {
                "cpu": inst.last_cpu,
                "status_check_failed": inst.last_status_check_failed,
                "at": fields.Datetime.to_string(inst.last_metric_date) or "",
                "has_data": bool(inst.last_metric_date),
                # RAM/disco no se relevan sin agente CloudWatch.
                "ram_available": False,
                "disk_available": False,
            },
            # Costo del servidor: el CRUDO de la máquina + cómo se reparte (R6-B2).
            "cost": self._server_cost_breakdown(env),
        }

    def _server_cost_breakdown(self, env):
        """Costo de un servidor: el CRUDO (lo que AWS factura por LA MÁQUINA,
        indivisible) y cómo se reparte entre las instancias hospedadas.

        Son dos números CONCEPTUALMENTE distintos aunque el invariante de R5 los
        iguale (Σ shares del servidor = su crudo): el crudo es la máquina entera;
        cada share es la porción de un cliente. En un server compartido la
        diferencia importa — la UI dice "AWS factura $X por esta máquina; se
        reparte así". Con la honestidad de siempre: "datos al".
        """
        if not env:
            return False
        Entry = self.env["primate.cloud.cost.entry"]
        Share = self.env["primate.cloud.cost.share"]
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        entries = Entry.search([
            ("account_id", "=", env.account_id.id),
            ("period_start", "=", month_start),
            ("granularity", "=", "monthly"),
            ("is_forecast", "=", False),
            ("environment_ref", "=", env.pcm_ref),
        ])
        shares = Share.search([
            ("environment_id", "=", env.id),
            ("period_start", "=", month_start),
            ("granularity", "=", "monthly"),
        ])
        pulled = env.account_id.cost_pulled_at
        return {
            "crudo": sum(entries.mapped("amount")),
            "has_data": bool(entries),
            "currency": (entries[:1].currency or shares[:1].currency or "USD"),
            "shares": [{
                "instance_name": s.instance_name or _("(sin instancia)"),
                "client_name": s.client_name or "",
                "amount": s.amount,
                "unattributed": s.unattributed,
            } for s in shares],
            "shares_total": sum(shares.mapped("amount")),
            "pulled_at": (fields.Datetime.to_string(pulled) if pulled else ""),
        }

    @api.model
    def get_odoo_instance_detail(self, instance_id):
        """Detalle de UNA instancia Odoo para su pantalla (R4-B6, read-only).

        El panel B (config/logs/addons/login-as/backups) SIEMPRE fue de la
        instancia aunque colgara de la máquina; acá por fin lo refleja. Las
        **rutas salen del backend** (conf/service/runtime/log/addons del slug)
        — reemplazan el ``PCM_PATHS`` hardcodeado del JS (shim A muerto).
        """
        inst = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not inst:
            return {}
        Db = self.env["primate.cloud.database"]
        Repo = self.env["primate.cloud.repository"]
        Backup = self.env["primate.cloud.backup"]
        Inst = self.env["primate.cloud.instance"]
        env = inst.environment_id
        machine = inst._machine()
        env_type_labels = dict(Inst._fields["env_type"].selection)
        edition_labels = dict(Inst._fields["odoo_edition"].selection)
        db_type_labels = dict(Db._fields["db_type"].selection)
        repo_type_labels = dict(Repo._fields["repo_type"].selection)
        sync_labels = dict(Repo._fields["sync_state"].selection)
        bkp_state_labels = dict(Backup._fields["state"].selection)
        bkp_purpose_labels = dict(Backup._fields["purpose"].selection)
        state_labels = dict(Inst._fields["state"].selection)
        databases = inst.database_id | Db.search(
            [("instance_id", "=", inst.id)])
        backups = Backup.search(
            [("database_id", "in", databases.ids)],
            order="backup_date desc, id desc", limit=15
        ) if databases else Backup.browse()
        repos = Repo.search([("instance_id", "=", inst.id)])
        return {
            "id": inst.id,
            "name": inst.display_name,
            "state": inst.state,
            "state_label": state_labels.get(inst.state, inst.state or ""),
            "env_type": inst.env_type,
            "env_type_label": env_type_labels.get(inst.env_type, ""),
            # is_production ES de la instancia (fricción por instancia, B5-audit).
            "is_production": bool(inst.env_type == "production"),
            "odoo_version": inst.odoo_version or "",
            "odoo_edition": edition_labels.get(inst.odoo_edition, ""),
            "main_url": inst.main_url or "",
            "slug": inst.slug or "",
            # Cliente (project) e infraestructura (servidor + máquina).
            "project_id": inst.project_id.id,
            "project_name": inst.project_id.display_name or "",
            "environment_id": env.id,
            "environment_name": env.display_name or "",
            "environment_real_name": env.name or "",
            "server_machine_id": machine.id if machine else False,
            "server_running": bool(machine and machine.instance_state == "running"),
            "provisioned_by_pcm": bool(machine and machine.provisioned_by_pcm),
            "public_ip": machine.public_ip or "" if machine else "",
            # RUTAS del backend (reemplazan PCM_PATHS): las del slug de ESTA
            # instancia, no las legacy hardcodeadas.
            "paths": {
                "conf": inst.conf_path or "",
                "service": inst.service_name or "",
                "python": inst.python_bin or "",
                "odoobin": inst.odoo_bin or "",
                "logfile": inst.log_path or "",
                "addons": inst.addons_dir or "",
                "data": inst.data_dir or "",
                "pg_user": inst.pg_user or "",
                "http_port": inst.http_port or 0,
            },
            "database": {
                "id": inst.database_id.id,
                "name": inst.database_id.display_name or "",
            } if inst.database_id else False,
            "databases": [{
                "id": db.id, "name": db.display_name,
                "db_type_label": db_type_labels.get(db.db_type, db.db_type or ""),
            } for db in databases],
            "backups": [{
                "id": b.id, "name": b.display_name,
                "backup_date": fields.Datetime.to_string(b.backup_date) or "",
                "state": b.state,
                "state_label": bkp_state_labels.get(b.state, b.state or ""),
                "purpose_label": bkp_purpose_labels.get(b.purpose, b.purpose or ""),
                "size_mb": b.size_mb or 0.0,
                "database_name": b.database_id.display_name or "",
            } for b in backups],
            "repositories": [{
                "id": r.id, "name": r.display_name,
                "repo_type_label": repo_type_labels.get(r.repo_type, r.repo_type or ""),
                "configured_branch": r.configured_branch or "",
                "current_commit": (r.current_commit or "")[:10],
                "sync_state": r.sync_state,
                "sync_state_label": sync_labels.get(r.sync_state, r.sync_state or ""),
            } for r in repos],
            # DNS best-effort (R4-B6.4): motivo del DNS pendiente si lo hay.
            "dns_pending": inst._dns_pending_data() if hasattr(
                inst, "_dns_pending_data") else False,
            # Porción de costo de ESTA instancia (su share del mes, R6-B2), con
            # la misma honestidad que el proyecto: método + prorrateo + "datos
            # al" + si el servidor es compartido.
            "cost": self._instance_cost_portion(inst),
        }

    def _instance_cost_portion(self, inst):
        """Share del mes en curso de una instancia (o None). Método + fecha +
        flag de servidor compartido — ningún número sin su contexto."""
        Share = self.env["primate.cloud.cost.share"]
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        shares = Share.search([
            ("instance_id", "=", inst.id),
            ("period_start", "=", month_start),
            ("granularity", "=", "monthly"),
        ])
        if not shares:
            return False
        method_labels = dict(Share._fields["method"].selection)
        server = inst.environment_id
        shared = bool(server) and (
            server._dedicated_client_partner() != inst.project_id.partner_id)
        pulled = inst.environment_id.account_id.cost_pulled_at
        return {
            "amount": sum(shares.mapped("amount")),
            "currency": shares[:1].currency or "USD",
            "method_label": method_labels.get(
                shares[:1].method, shares[:1].method or ""),
            "shared_server": shared,
            "pulled_at": (fields.Datetime.to_string(pulled) if pulled else ""),
        }

    # ------------------------------------------------------------------
    # Wrappers de panel POR INSTANCIA (R4-B6, shim B retirado).
    # Reciben un id de ``primate.cloud.instance`` (NO de la EC2) y delegan en
    # la API canónica del modelo instancia (que resuelve su máquina y opera
    # SUS rutas). Antes recibían el id de la EC2 y resolvían "la primaria" —
    # la suposición que el recableo mató.
    # ------------------------------------------------------------------
    def _panel_instance(self, instance_id):
        """Resuelve la instancia del panel y valida que sea operable.

        Devuelve ``(instance, error_dict)``: si no se puede operar, ``error``
        trae un dict ``{"status":"error","text":...}`` para devolver tal cual.
        """
        inst = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not inst:
            return inst, {"status": "error", "text": _("Instancia no encontrada.")}
        machine = inst._machine()
        if not machine or machine.instance_state != "running":
            return inst, {"status": "error",
                          "text": _("El servidor de la instancia no está "
                                    "corriendo.")}
        return inst, None

    @api.model
    def get_odoo_logs(self, instance_id, source, lines=200, grep=None,
                      since=None, until=None):
        """Logs de UNA instancia Odoo por SSM (on-demand, sin persistir)."""
        inst, error = self._panel_instance(instance_id)
        if error:
            return error
        try:
            return inst.fetch_logs(source, lines=lines, grep=grep,
                                   since=since, until=until)
        except Exception as error:  # noqa: BLE001 - se muestra, no rompe
            return {"status": "error", "text": str(error)}

    @api.model
    def get_odoo_logs_stream(self, instance_id, source, from_cursor=False,
                             lines=200, grep=None):
        """Streaming incremental de logs de UNA instancia (sin persistir)."""
        inst, error = self._panel_instance(instance_id)
        if error:
            return dict(error, cursor=from_cursor)
        try:
            return inst.fetch_logs_stream(source, from_cursor=from_cursor,
                                          lines=lines, grep=grep)
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "text": str(error), "cursor": from_cursor}

    @api.model
    def get_odoo_config(self, instance_id):
        """Lee el odoo.conf DE LA INSTANCIA (allowlist + hash) para la tab Config.

        ``is_production`` sale del ``env_type`` de LA INSTANCIA (no del
        servidor): la fricción de prod es propiedad de la instancia (B5-audit).
        """
        inst, error = self._panel_instance(instance_id)
        if error:
            return error
        machine = inst._machine()
        if not machine.provisioned_by_pcm:
            return {"status": "error",
                    "text": _("Requiere una instancia aprovisionada por PCM.")}
        try:
            data = inst.fetch_config()
            data["status"] = "ok"
            data["meta"] = machine._config_field_meta()
            data["is_production"] = bool(inst.env_type == "production")
            data["environment_name"] = inst.environment_id.name or ""
            return data
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "text": str(error)}

    @api.model
    def list_odoo_db_users(self, instance_id, db):
        """Usuarios internos activos de una BD de la instancia (para Login as)."""
        inst, error = self._panel_instance(instance_id)
        if error:
            return error
        try:
            return {"status": "ok", "users": inst.list_db_users(db)}
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "text": str(error)}

    @api.model
    def odoo_login_as(self, instance_id, db, uid, login, is_admin_target=False,
                      admin_ack=False, typed_name=None):
        """Genera el enlace de impersonación (con fricción + auditoría). B5."""
        inst = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not inst:
            return {"status": "error", "text": _("Instancia no encontrada.")}
        try:
            res = inst.action_login_as(
                db, uid, login, is_admin_target=is_admin_target,
                admin_ack=admin_ack, typed_name=typed_name)
            return {"status": "ok", "url": res["url"]}
        except Exception as error:  # noqa: BLE001 - error legible a la UI
            return {"status": "error", "text": str(error)}

    @api.model
    def add_odoo_addon(self, instance_id, vals):
        """Agrega un repo/addon a LA instancia (clona + registra). B4.

        El addon se ata a esta instancia (``instance_id`` en el repo) para que
        el clone use SU ``addons_dir`` (no el de la primaria).
        """
        inst, error = self._panel_instance(instance_id)
        if error:
            return error
        machine = inst._machine()
        if not machine.provisioned_by_pcm:
            return {"status": "error",
                    "text": _("Requiere una instancia aprovisionada por PCM.")}
        try:
            repo = inst.environment_id.add_addon(dict(vals, instance_id=inst.id))
            return {"status": "ok", "repo_id": repo.id}
        except Exception as error:  # noqa: BLE001 - error legible a la UI
            return {"status": "error", "text": str(error)}

    @api.model
    def save_odoo_config(self, instance_id, edits, expected_hash,
                         typed_name=None):
        """Valida y encola el guardado del odoo.conf DE LA INSTANCIA (reinicia)."""
        inst = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not inst:
            return {"status": "error", "text": _("Instancia no encontrada.")}
        try:
            inst.action_save_config(edits, expected_hash, typed_name=typed_name)
            return {"status": "ok"}
        except Exception as error:  # noqa: BLE001 - error legible a la UI
            return {"status": "error", "text": str(error)}

    @api.model
    def get_cost_overview(self, account_id=None):
        """Resumen de costos para la pantalla de Costos (lee cost.entry).

        Siempre incluye ``pulled_at`` (leyenda 'datos al'): el dato de Cost
        Explorer tiene retardo, NO es tiempo real.
        """
        Entry = self.env["primate.cloud.cost.entry"]
        Account = self.env["primate.cloud.account"]
        domain = []
        if account_id:
            domain.append(("account_id", "=", account_id))
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        prev_start = (month_start - timedelta(days=1)).replace(day=1)

        def _agg(entries, key_field, label_field):
            out = {}
            for e in entries:
                k = e[key_field] or _("Sin atribuir")
                out.setdefault(k, 0.0)
                out[k] += e.amount
            return [{"label": k, "amount": round(v, 2)}
                    for k, v in sorted(out.items(), key=lambda x: -x[1])]

        current = Entry.search(domain + [("period_start", "=", month_start),
                                         ("is_forecast", "=", False)])
        previous = Entry.search(domain + [("period_start", "=", prev_start),
                                          ("is_forecast", "=", False)])
        forecast = Entry.search(domain + [("is_forecast", "=", True),
                                          ("period_start", "=", month_start)])
        accounts = Account.search(
            [("id", "=", account_id)] if account_id else [])
        # Solo fechas reales: con varias cuentas, unas con pull y otras sin
        # (cost_pulled_at False), max() sobre la mezcla compararía bool y datetime.
        pulled_dates = [d for d in accounts.mapped("cost_pulled_at") if d]
        pulled = max(pulled_dates) if pulled_dates else False
        currency = (current[:1].currency or "USD")
        # Por cliente = el REPARTO (cost.share), NO el tag crudo: en servidores
        # compartidos el tag primate:client_id se retira (una EC2 no tiene dueño
        # único) y el crudo caería en "Sin atribuir". El reparto sí atribuye la
        # porción de cada cliente. Suma el crudo (invariante R5).
        Share = self.env["primate.cloud.cost.share"]
        share_domain = ([("account_id", "=", account_id)] if account_id else [])
        shares_current = Share.search(share_domain + [
            ("period_start", "=", month_start), ("granularity", "=", "monthly")])
        share_by_client = {}
        for sh in shares_current:
            key = (sh.client_name if not sh.unattributed and sh.client_name
                   else _("Sin atribuir"))
            share_by_client.setdefault(key, 0.0)
            share_by_client[key] += sh.amount
        by_client_share = [{"label": k, "amount": round(v, 2)}
                           for k, v in sorted(share_by_client.items(),
                                              key=lambda x: -x[1])]
        return {
            "pulled_at": fields.Datetime.to_string(pulled) if pulled else "",
            "currency": currency,
            "current_total": round(sum(current.mapped("amount")), 2),
            "prev_total": round(sum(previous.mapped("amount")), 2),
            "forecast_total": round(sum(forecast.mapped("amount")), 2),
            "by_environment": _agg(current, "environment_name", None),
            "by_service": _agg(current, "service", None),
            # Reparto por cliente (la vista honesta en multi-tenant).
            "by_client_share": by_client_share,
        }

    @api.model
    def get_database_detail(self, db_id):
        """Serializa el detalle de una base de datos para la app (read-only)."""
        db = self.env["primate.cloud.database"].browse(db_id).exists()
        if not db:
            return {}
        Db = self.env["primate.cloud.database"]
        state_labels = dict(Db._fields["state"].selection)
        type_labels = dict(Db._fields["db_type"].selection)
        return {
            "id": db.id,
            "name": db.display_name,
            "state": db.state,
            "state_label": state_labels.get(db.state, db.state or ""),
            "db_type": db.db_type,
            "db_type_label": type_labels.get(db.db_type, db.db_type or ""),
            "pg_version": db.pg_version or "",
            "rds_identifier": db.rds_identifier or "",
            "rds_endpoint": db.rds_endpoint or "",
            "rds_instance_class": db.rds_instance_class or "",
            "rds_storage_gb": db.rds_storage_gb or 0,
            "rds_multi_az": db.rds_multi_az,
            "backup_retention_days": db.backup_retention_days or 0,
            "storage_used_gb": db.storage_used_gb or 0,
            "active_connections": db.active_connections or 0,
            "last_backup_date": fields.Datetime.to_string(db.last_backup_date) or "",
            "last_sync_date": fields.Datetime.to_string(db.last_sync_date) or "",
            "account_id": db.account_id.id or False,
            "account_name": db.account_id.display_name or "",
            "environment_id": db.environment_id.id or False,
            "environment_name": db.environment_id.display_name or "",
            "server_id": db.ec2_instance_id.id or False,
            "server_name": db.ec2_instance_id.display_name or "",
            "backups": [{
                "id": backup.id, "name": backup.name,
                "date": fields.Datetime.to_string(backup.backup_date) or "",
                "purpose_label": dict(
                    backup._fields["purpose"].selection).get(backup.purpose, ""),
                "state": backup.state,
                "state_label": dict(
                    backup._fields["state"].selection).get(backup.state, ""),
                "size_mb": round(backup.size_mb or 0.0, 1),
            } for backup in self.env["primate.cloud.backup"].search(
                [("database_id", "=", db.id)], limit=10)],
        }

    @api.model
    def get_repository_detail(self, repo_id):
        """Serializa el detalle de un repositorio para la app (read-only)."""
        repo = self.env["primate.cloud.repository"].browse(repo_id).exists()
        if not repo:
            return {}
        Repo = self.env["primate.cloud.repository"]
        Mod = self.env["primate.cloud.module"]
        sync_labels = dict(Repo._fields["sync_state"].selection)
        type_labels = dict(Repo._fields["repo_type"].selection)
        mod_labels = dict(Mod._fields["db_state"].selection)
        return {
            "id": repo.id,
            "name": repo.display_name,
            "sync_state": repo.sync_state,
            "sync_state_label": sync_labels.get(repo.sync_state, repo.sync_state or ""),
            "repo_type": repo.repo_type,
            "repo_type_label": type_labels.get(repo.repo_type, repo.repo_type or ""),
            "github_url": repo.github_url or "",
            "organization": repo.organization or "",
            "branch": repo.configured_branch or "",
            "local_path": repo.local_path or "",
            "current_commit": repo.current_commit or "",
            "latest_commit": repo.latest_commit or "",
            "last_sync_date": fields.Datetime.to_string(repo.last_sync_date) or "",
            "environment_id": repo.environment_id.id or False,
            "environment_name": repo.environment_id.display_name or "",
            # Error de sync accionable (GitHub/SSM) sin traceback + crudo plegable.
            **self._last_sync_error(repo),
            "module_count": len(repo.module_ids),
            "commit_count": len(repo.commit_ids),
            "modules": [{
                "technical_name": m.technical_name,
                "functional_name": m.functional_name or "",
                "version": m.version or "",
                "db_version": m.db_version or "",
                "db_state": m.db_state,
                "db_state_label": mod_labels.get(m.db_state, m.db_state or ""),
            } for m in repo.module_ids],
            "commits": [{
                "short": (c.commit_hash or "")[:9],
                "message": (c.message or "").splitlines()[0] if c.message else "",
                "author": c.author or "",
                "commit_date": fields.Datetime.to_string(c.commit_date) or "",
                "is_current": c.is_current,
            } for c in repo.commit_ids[:40]],
        }

    @api.model
    def get_deployment_detail(self, dep_id):
        """Serializa el detalle de un despliegue para la app (read-only)."""
        dep = self.env["primate.cloud.deployment"].browse(dep_id).exists()
        if not dep:
            return {}
        Dep = self.env["primate.cloud.deployment"]
        state_labels = dict(Dep._fields["state"].selection)
        type_labels = dict(Dep._fields["deployment_type"].selection)
        # Instancia DESTINO resuelta (mismo criterio que action_run): explícita,
        # o la del repo, o la primaria del entorno. Que el destino sea inequívoco.
        target = (dep.instance_id or dep.repository_id.instance_id
                  or dep.environment_id.primary_instance_id)
        return {
            "id": dep.id,
            "name": dep.display_name,
            "state": dep.state,
            "state_label": state_labels.get(dep.state, dep.state or ""),
            "deployment_type": dep.deployment_type,
            "deployment_type_label": type_labels.get(dep.deployment_type, ""),
            "origin_commit": dep.origin_commit or "",
            "target_commit": dep.target_commit or "",
            "target_branch": dep.target_branch or "",
            "module_names": dep.module_names or "",
            "is_revert": dep.is_revert,
            "execution_date": fields.Datetime.to_string(dep.execution_date) or "",
            "execution_log": dep.execution_log or "",
            "triggered_by": dep.triggered_by.display_name or "",
            "environment_id": dep.environment_id.id or False,
            "environment_name": dep.environment_id.display_name or "",
            "repository_id": dep.repository_id.id or False,
            "repository_name": dep.repository_id.display_name or "",
            "instance_id": target.id or False,
            "instance_name": target.display_name or "",
            # Error accionable si el deploy falló (SSM/git), con crudo plegable.
            **self._last_sync_error(dep, failed=(dep.state == "failed")),
        }

    @api.model
    def get_dns_detail(self, dns_id):
        """Serializa el detalle de un registro DNS para la app (read-only)."""
        rec = self.env["primate.cloud.dns.record"].browse(dns_id).exists()
        if not rec:
            return {}
        Dns = self.env["primate.cloud.dns.record"]
        state_labels = dict(Dns._fields["state"].selection)
        sync_labels = dict(Dns._fields["sync_state"].selection)
        return {
            "id": rec.id,
            "name": rec.display_name,
            "state": rec.state,
            "state_label": state_labels.get(rec.state, rec.state or ""),
            "sync_state": rec.sync_state,
            "sync_state_label": sync_labels.get(rec.sync_state, ""),
            "record_value_aws": rec.record_value_aws or "",
            "last_change_id": rec.last_change_id or "",
            "delete_needs_ack": rec.delete_needs_ack,
            "is_deleted": rec.state == "deleted",
            "is_alias": rec.is_alias,
            "record_type": rec.record_type,
            "record_value": rec.record_value or "",
            "ttl": rec.ttl or 0,
            "hosted_zone_id": rec.hosted_zone_id or "",
            "last_sync_date": fields.Datetime.to_string(rec.last_sync_date) or "",
            "account_id": rec.account_id.id or False,
            "account_name": rec.account_id.display_name or "",
            "environment_id": rec.environment_id.id or False,
            "environment_name": rec.environment_id.display_name or "",
            # Error de sync accionable (sin traceback) + crudo plegable.
            **self._last_sync_error(rec),
        }

    # ------------------------------------------------------------------
    # Aprovisionamiento (B3): datos para el formulario OWL de "Crear entorno".
    # No agrega lógica: reusa el default_get + onchanges del provision.wizard.
    # ------------------------------------------------------------------
    @api.model
    def get_provision_defaults(self, env_id):
        """Precarga del formulario de aprovisionamiento (Crear entorno).

        Reusa el ``default_get`` del wizard (que trae la config guardada de un
        intento previo) y sus onchanges (prefills derivados del entorno + estado
        cacheado de la región). Devuelve además el flag de admin (para los campos
        avanzados) y las opciones de los Selection. Todo server-side: el form OWL
        es pura presentación.
        """
        env = self.env["primate.cloud.environment"].browse(env_id).exists()
        if not env:
            return {}
        Wizard = self.env["primate.cloud.provision.wizard"]
        ctx = dict(self.env.context, default_environment_id=env_id)
        WizardCtx = Wizard.with_context(**ctx)
        defaults = WizardCtx.default_get(list(Wizard._fields))
        wiz = WizardCtx.new(dict(defaults, environment_id=env_id))
        wiz._onchange_environment_id()   # prefills derivados del entorno
        wiz._onchange_region_status()    # semáforo cacheado (no golpea AWS)

        char_int_bool = [
            "region", "domain", "admin_password", "instance_name", "instance_type",
            "os_type", "image_id", "disk_size_gb", "key_name", "security_group_ids",
            "subnet_id", "instance_profile", "db_mode", "db_name", "db_user",
            "db_password", "pg_version", "rds_identifier", "rds_instance_class",
            "rds_storage_gb", "rds_multi_az", "backup_retention_days", "create_dns",
            "hosted_zone_id", "ttl", "region_status", "region_detail",
        ]
        data = {f: (wiz[f] if wiz[f] not in (None,) else False) for f in char_int_bool}
        region_labels = dict(Wizard._fields["region_status"].selection)

        def options(field):
            return [{"value": v, "label": l}
                    for v, l in Wizard._fields[field].selection]

        data.update({
            "environment_id": env_id,
            "environment_name": env.display_name,
            "account_id": wiz.account_id.id or False,
            "account_name": wiz.account_id.display_name or "",
            "region_status_label": region_labels.get(wiz.region_status, ""),
            "is_cloud_admin": self.env.user.has_group(
                "primate_cloud_manager.group_cloud_admin"),
            "os_types": options("os_type"),
            "db_modes": options("db_mode"),
            "pg_versions": options("pg_version"),
            "regions": options("region"),
        })
        return data

    @api.model
    def verify_region(self, account_id, region):
        """Corre el descubrimiento de la región y devuelve el semáforo.

        Read-only interactivo (misma política que ``action_verify_region``):
        alimenta el semáforo del form sin encolar. Reusa ``get_or_discover``.
        """
        account = self.env["primate.cloud.account"].browse(account_id).exists()
        if not account or not region:
            return {"status": "draft", "detail": _("Elegí cuenta y región."),
                    "status_label": ""}
        setup = self.env["primate.cloud.region.setup"].get_or_discover(
            account, region)
        labels = dict(
            self.env["primate.cloud.region.setup"]._fields["status"].selection)
        return {
            "status": setup.status,
            "detail": setup.detail or "",
            "status_label": labels.get(setup.status, setup.status or ""),
        }

    @api.model
    def get_database_form_data(self, env_id=False, db_id=False):
        """Datos para el formulario OWL de base de datos (crear/editar).

        Devuelve los valores actuales (edición), las opciones de Selection y los
        servidores del entorno (para el select de instancia en modalidad local).
        Los campos RDS van como informativos: los sincroniza AWS, no se editan.
        """
        Db = self.env["primate.cloud.database"]
        db = Db.browse(db_id).exists() if db_id else None
        environment = (db.environment_id if db else
                       self.env["primate.cloud.environment"].browse(env_id).exists())
        state_labels = dict(Db._fields["state"].selection)

        def options(field):
            return [{"value": v, "label": l}
                    for v, l in Db._fields[field].selection]

        servers = [{"value": s.id, "label": s.display_name}
                   for s in (environment.ec2_instance_ids if environment else [])]
        data = {
            "servers": servers,
            "db_types": options("db_type"),
            "pg_versions": options("pg_version"),
            "environment_id": environment.id if environment else False,
            "environment_name": environment.display_name if environment else "",
            "account_id": (db.account_id.id if db else
                           (environment.account_id.id if environment else False)),
            "account_name": (db.account_id.display_name if db else
                             (environment.account_id.display_name if environment else "")),
        }
        if db:
            data.update({
                "id": db.id, "name": db.name, "db_type": db.db_type,
                "pg_version": db.pg_version or "",
                "ec2_instance_id": db.ec2_instance_id.id or False,
                "state": db.state, "state_label": state_labels.get(db.state, ""),
                "rds_identifier": db.rds_identifier or "",
                "rds_endpoint": db.rds_endpoint or "",
                "rds_instance_class": db.rds_instance_class or "",
                "rds_storage_gb": db.rds_storage_gb or 0,
                "rds_multi_az": db.rds_multi_az,
                "backup_retention_days": db.backup_retention_days or 0,
            })
        else:
            data.update({
                "id": False, "name": "", "db_type": "rds", "pg_version": "",
                "ec2_instance_id": False, "state": False, "state_label": "",
            })
        return data

    @api.model
    def get_deployment_form_data(self, env_id):
        """Datos para el formulario OWL de nuevo despliegue.

        Devuelve las instancias del entorno (destino inequívoco), sus
        repositorios, los tipos de deploy y la instancia primaria por defecto.
        """
        Dep = self.env["primate.cloud.deployment"]
        environment = self.env["primate.cloud.environment"].browse(env_id).exists()
        if not environment:
            return {}
        instances = [{"value": i.id, "label": i.display_name}
                     for i in environment.instance_ids]
        repositories = [{"value": r.id, "label": r.display_name}
                        for r in environment.repository_ids]
        return {
            "environment_id": environment.id,
            "environment_name": environment.display_name,
            "instances": instances,
            "repositories": repositories,
            "deployment_types": [{"value": v, "label": l}
                                 for v, l in Dep._fields["deployment_type"].selection],
            "primary_instance_id": environment.primary_instance_id.id or False,
        }

    @api.model
    def get_instance_form_data(self, env_id):
        """Datos para el formulario OWL de "Agregar Odoo" (otro Odoo en un
        servidor existente). Reusa los computes del instance.create.wizard para
        el preview de puertos y la advertencia de RAM, y valida las guardas de
        apertura (servidor activo, no legacy) devolviendo un motivo si bloquea.
        """
        server = self.env["primate.cloud.environment"].browse(env_id).exists()
        if not server:
            return {}
        if server.state != "active":
            return {"blocked": _("Solo se agregan instancias a un servidor activo.")}
        if server._is_legacy_layout():
            return {"blocked": _(
                "Este servidor tiene layout legacy (un solo Odoo pre-R3): "
                "montarle un segundo Odoo requiere adoptarlo al layout "
                "multi-Odoo (mini-fase pendiente). Para un Odoo nuevo hoy, "
                "creá un entorno.")}
        Wizard = self.env["primate.cloud.instance.create.wizard"]
        wiz = Wizard.new({"environment_id": env_id})  # dispara los computes

        def options(field):
            return [{"value": v, "label": l}
                    for v, l in Wizard._fields[field].selection]

        projects = [{"value": p.id, "label": p.display_name}
                    for p in self.env["primate.cloud.project"].search([])]
        return {
            "environment_id": env_id,
            "server_name": server.display_name,
            "projects": projects,
            "http_port_preview": wiz.http_port_preview or 0,
            "gevent_port_preview": wiz.gevent_port_preview or 0,
            "ram_warning": wiz.ram_warning or "",
            "odoo_versions": options("odoo_version"),
            "odoo_editions": options("odoo_edition"),
        }

    @api.model
    def get_account_form_data(self, account_id=False):
        """Datos para el formulario OWL de cuenta AWS (crear/editar).

        Las credenciales son solo-admin (mismo gate que el modelo): el Access Key
        ID va en CLARO (identificador, inútil sin el secreto); del Secret solo se
        informa si HAY uno guardado (``secret_set``), NUNCA el valor ni la máscara.
        """
        Account = self.env["primate.cloud.account"]
        acc = Account.browse(account_id).exists() if account_id else None
        is_admin = self.env.user.has_group(
            "primate_cloud_manager.group_cloud_admin")

        def options(field):
            return [{"value": v, "label": l}
                    for v, l in Account._fields[field].selection]

        data = {
            "id": acc.id if acc else False,
            "name": acc.name if acc else "",
            "aws_account_id": (acc.aws_account_id or "") if acc else "",
            "default_region": (acc.default_region or "") if acc else "",
            "auth_method": (acc.auth_method if acc else "access_key") or "access_key",
            "notes": (acc.notes or "") if acc else "",
            "regions": options("default_region"),
            "auth_methods": options("auth_method"),
            "is_cloud_admin": is_admin,
            # placeholders (se completan solo si admin)
            "iam_access_key_id": "", "secret_set": False,
            "role_arn": "", "external_id": "",
        }
        if is_admin and acc:
            data.update({
                # Access Key ID EN CLARO (identificador, solo-admin).
                "iam_access_key_id": acc.iam_access_key_id or "",
                # Solo si HAY secreto; nunca el valor ni la máscara.
                "secret_set": bool(acc.iam_secret_access_key),
                "role_arn": acc.role_arn or "",
                "external_id": acc.external_id or "",
            })
        return data

    @api.model
    def get_account_detail(self, account_id):
        """Serializa el detalle de una cuenta AWS para la app (read-only).

        No expone credenciales: solo metadatos y recursos asociados.
        """
        acc = self.env["primate.cloud.account"].browse(account_id).exists()
        if not acc:
            return {}
        Account = self.env["primate.cloud.account"]
        conn_labels = dict(Account._fields["connection_state"].selection)
        region_labels = dict(Account._fields["default_region"].selection)
        auth_labels = dict(Account._fields["auth_method"].selection)
        Env = self.env["primate.cloud.environment"]
        Ec2 = self.env["primate.cloud.ec2.instance"]
        return {
            "id": acc.id,
            "name": acc.display_name,
            "connection_state": acc.connection_state,
            "connection_state_label": conn_labels.get(
                acc.connection_state, acc.connection_state or ""),
            "aws_account_id": acc.aws_account_id or "",
            "default_region": region_labels.get(acc.default_region, acc.default_region or ""),
            "auth_method": auth_labels.get(acc.auth_method, acc.auth_method or ""),
            "last_sync_date": fields.Datetime.to_string(acc.last_sync_date) or "",
            "notes": acc.notes or "",
            "environments": [{
                "id": env.id, "name": env.display_name,
            } for env in Env.search([("account_id", "=", acc.id)])],
            "servers": [{
                "id": inst.id, "name": inst.display_name,
                "public_ip": inst.public_ip or "",
            } for inst in Ec2.search([("account_id", "=", acc.id)])],
            # Error accionable de conexión (AccessDenied → guía a IAM) + crudo.
            **self._last_sync_error(acc, failed=(acc.connection_state == "error")),
        }

    def _unassigned_partner_id(self):
        """Id del partner centinela "⚠ SIN CLIENTE" (0 si no hay)."""
        return int(self.env["ir.config_parameter"].sudo().get_param(
            "pcm.unassigned_partner_id") or 0)

    @api.model
    def get_project_detail(self, project_id):
        """Detalle del PROYECTO = la vista del CLIENTE (eje cliente, R6).

        Sus instancias estén en el servidor que estén (eje cliente cruzando el
        eje infraestructura en la instancia) + su reparto de costo del mes
        (``cost.share`` de R5), con la honestidad obligatoria: método + "datos
        al". El TOTAL se suma de LAS MISMAS shares que se listan (mismo
        recordset): la vista del proyecto filtra un subconjunto (las shares del
        cliente en varios servidores) donde el invariante de R5 (Σ = crudo del
        SERVIDOR) no aplica — un total calculado aparte podría descuadrar.
        """
        proj = self.env["primate.cloud.project"].browse(project_id).exists()
        if not proj:
            return {}
        Instance = self.env["primate.cloud.instance"]
        Share = self.env["primate.cloud.cost.share"]
        type_labels = dict(Instance._fields["env_type"].selection)
        state_labels = dict(Instance._fields["state"].selection)
        method_labels = dict(Share._fields["method"].selection)

        # --- Instancias del cliente (donde sea que vivan) ---
        instances = Instance.search([("project_id", "=", proj.id)])
        inst_data = [{
            "id": inst.id, "name": inst.display_name,
            "server_id": inst.environment_id.id or False,
            "server_name": inst.environment_id.display_name or "",
            "env_type": inst.env_type,
            "env_type_label": type_labels.get(inst.env_type, ""),
            "is_staging": inst.env_type == "staging",
            "state": inst.state,
            "state_label": state_labels.get(inst.state, inst.state or ""),
            "main_url": inst.main_url or "",
        } for inst in instances]

        # --- Reparto del mes en curso (las MISMAS shares → total y desglose) ---
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        shares = Share.search([
            ("project_id", "=", proj.id),
            ("period_start", "=", month_start),
            ("granularity", "=", "monthly"),
        ])
        cost_lines = []
        cost_total = 0.0
        for share in shares:
            cost_total += share.amount           # total = suma de ESTAS shares
            server = share.environment_id
            shared = bool(server) and (
                server._dedicated_client_partner() != proj.partner_id)
            cost_lines.append({
                "instance_id": share.instance_id.id or False,
                "instance_name": (share.instance_name
                                  or _("(sin instancia)")),
                "server_name": share.environment_name or "",
                "amount": share.amount,
                "method_label": method_labels.get(share.method, share.method or ""),
                "shared_server": shared,
                "unattributed": share.unattributed,
            })
        pulled = proj.account_id.cost_pulled_at
        return {
            "id": proj.id, "name": proj.display_name,
            "account_id": proj.account_id.id or False,
            "account_name": proj.account_id.display_name or "",
            "partner_name": proj.partner_id.display_name or "",
            "is_unassigned": proj.partner_id.id == self._unassigned_partner_id(),
            "notes": proj.notes or "",
            "instances": inst_data,
            "instance_count": len(inst_data),
            "cost_total": cost_total,
            "cost_lines": cost_lines,
            "cost_currency": shares[:1].currency or "USD",
            # Honestidad: nunca un número sin su método y su fecha.
            "cost_pulled_at": (fields.Datetime.to_string(pulled)
                               if pulled else ""),
        }

    @api.model
    def get_projects(self):
        """Lista de PROYECTOS = el eje cliente (R6). Cada proyecto con su nº de
        instancias y su costo del mes (suma de sus shares). El proyecto centinela
        "⚠ SIN CLIENTE" se marca ``is_unassigned`` (bandeja de pendientes, no un
        cliente) y va al final."""
        Project = self.env["primate.cloud.project"]
        Instance = self.env["primate.cloud.instance"]
        Share = self.env["primate.cloud.cost.share"]
        sentinel_id = self._unassigned_partner_id()
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        out = []
        for proj in Project.search([]):
            shares = Share.search([
                ("project_id", "=", proj.id),
                ("period_start", "=", month_start),
                ("granularity", "=", "monthly"),
            ])
            out.append({
                "id": proj.id, "name": proj.display_name,
                "partner_name": proj.partner_id.display_name or "",
                "is_unassigned": proj.partner_id.id == sentinel_id,
                "instance_count": Instance.search_count(
                    [("project_id", "=", proj.id)]),
                "cost_total": sum(shares.mapped("amount")),
                "cost_currency": shares[:1].currency or "USD",
            })
        # Centinela (bandeja de pendientes) al final.
        out.sort(key=lambda p: (p["is_unassigned"], p["name"].lower()))
        return out
