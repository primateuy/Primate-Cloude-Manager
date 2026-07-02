# -*- coding: utf-8 -*-
"""Panel (dashboard) de Primate Cloud Manager.

Modelo abstracto que agrega los datos que consume el componente OWL del panel:
KPIs, últimos despliegues y alertas. No persiste nada; solo lee el inventario.
"""
from odoo import _, api, fields, models


class PrimateCloudDashboard(models.AbstractModel):
    """Fuente de datos del panel de inicio del módulo."""

    _name = "primate.cloud.dashboard"
    _description = "Panel Cloud"

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
        )
        kpis = [
            {"key": "env_active", "label": "Entornos activos", "icon": "fa-cubes",
             "value": Environment.search_count([("state", "=", "active")])},
            {"key": "ec2_running", "label": "Servidores activos", "icon": "fa-server",
             "value": Ec2.search_count([("instance_state", "=", "running")])},
            {"key": "cost", "label": "Costo mensual", "icon": "fa-line-chart",
             "value": "—", "hint": "Disponible con la fase de costos"},
            {"key": "alerts", "label": "Alertas", "icon": "fa-bell", "value": alerts},
        ]

        state_labels = dict(Environment._fields["state"].selection)
        type_labels = dict(Environment._fields["env_type"].selection)
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
        type_labels = dict(Environment._fields["env_type"].selection)
        edition_labels = dict(Environment._fields["odoo_edition"].selection)
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
        type_labels = dict(Env._fields["env_type"].selection)
        edition_labels = dict(Env._fields["odoo_edition"].selection)
        ec2_labels = dict(Ec2._fields["instance_state"].selection)
        db_labels = dict(Db._fields["state"].selection)
        db_type_labels = dict(Db._fields["db_type"].selection)
        sync_labels = dict(Repo._fields["sync_state"].selection)
        repo_type_labels = dict(Repo._fields["repo_type"].selection)
        mod_labels = dict(Mod._fields["db_state"].selection)
        dns_state_labels = dict(Dns._fields["state"].selection)
        dep_state_labels = dict(Dep._fields["state"].selection)
        dep_type_labels = dict(Dep._fields["deployment_type"].selection)
        Backup = self.env["primate.cloud.backup"]
        compliance_labels = dict(Env._fields["backup_compliance"].selection)
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
        return {
            "id": inst.id,
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
            "aws_created_at": fields.Datetime.to_string(inst.aws_created_at) or "",
            "last_sync_date": fields.Datetime.to_string(inst.last_sync_date) or "",
            "account_id": inst.account_id.id,
            "account_name": inst.account_id.display_name or "",
            "environment_id": inst.environment_id.id,
            "environment_name": inst.environment_id.display_name or "",
            "databases": [{
                "id": db.id, "name": db.display_name,
                "db_type": db.db_type,
            } for db in self.env["primate.cloud.database"].search(
                [("ec2_instance_id", "=", inst.id)])],
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
        }

    @api.model
    def get_dns_detail(self, dns_id):
        """Serializa el detalle de un registro DNS para la app (read-only)."""
        rec = self.env["primate.cloud.dns.record"].browse(dns_id).exists()
        if not rec:
            return {}
        Dns = self.env["primate.cloud.dns.record"]
        state_labels = dict(Dns._fields["state"].selection)
        return {
            "id": rec.id,
            "name": rec.display_name,
            "state": rec.state,
            "state_label": state_labels.get(rec.state, rec.state or ""),
            "record_type": rec.record_type,
            "record_value": rec.record_value or "",
            "ttl": rec.ttl or 0,
            "hosted_zone_id": rec.hosted_zone_id or "",
            "last_sync_date": fields.Datetime.to_string(rec.last_sync_date) or "",
            "account_id": rec.account_id.id or False,
            "account_name": rec.account_id.display_name or "",
            "environment_id": rec.environment_id.id or False,
            "environment_name": rec.environment_id.display_name or "",
        }

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
        }

    @api.model
    def get_project_detail(self, project_id):
        """Serializa el detalle de un proyecto para la app (read-only)."""
        proj = self.env["primate.cloud.project"].browse(project_id).exists()
        if not proj:
            return {}
        Env = self.env["primate.cloud.environment"]
        state_labels = dict(Env._fields["state"].selection)
        type_labels = dict(Env._fields["env_type"].selection)
        return {
            "id": proj.id,
            "name": proj.display_name,
            "account_id": proj.account_id.id or False,
            "account_name": proj.account_id.display_name or "",
            "partner_name": proj.partner_id.display_name or "",
            "notes": proj.notes or "",
            "environment_count": proj.environment_count,
            "environments": [{
                "id": env.id, "name": env.display_name,
                "env_type_label": type_labels.get(env.env_type, ""),
                "state": env.state,
                "state_label": state_labels.get(env.state, env.state or ""),
            } for env in proj.environment_ids],
        }
