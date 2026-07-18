/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Pantalla de una INSTANCIA Odoo (R4-B6). El panel de la Fase B —config,
 * logs, addons, login-as, backups— SIEMPRE fue de la instancia aunque
 * colgara de la máquina; acá por fin lo refleja. Opera SU instancia
 * explícita (props.instanceId), y las RUTAS vienen del backend
 * (`get_odoo_instance_detail`) — se acabó el `PCM_PATHS` hardcodeado.
 */
export class InstanciaDetalle extends Component {
    static template = "primate_cloud_manager.InstanciaDetalle";
    static components = { PcmStatusBadge };
    static props = {
        instanceId: { type: Number },
        initialTab: { type: String, optional: true },
        onTabChange: { type: Function, optional: true },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.instance";
        this.state = useState({
            loading: true, data: {},
            tab: this.props.initialTab || "dashboard",
        });
        this.logs = useState({
            source: "odoo", lines: 200, grep: "", text: "", loading: false,
            streaming: false, cursor: false,
        });
        this.config = useState({
            loaded: false, loading: false, status: "", error: "",
            editable: {}, readonly: {}, meta: [], hash: false, work: {},
            isProduction: false, envName: "", typedName: "", saving: false,
        });
        this.addon = useState({
            open: false, github_url: "", configured_branch: "",
            repo_type: "custom_client", github_token: "", saving: false,
        });
        this.loginas = useState({
            open: false, db: "", users: [], loading: false, typedName: "",
        });
        this._streamTimer = null;
        this._polling = false;
        this._onVisibility = () => this._handleVisibility();
        onWillStart(() => this.load(this.props.instanceId));
        onWillUpdateProps((next) => {
            if (next.instanceId !== this.props.instanceId) {
                this.stopStream();
                this.load(next.instanceId);
            }
        });
        onWillUnmount(() => this.stopStream());
    }

    async load(id) {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_odoo_instance_detail", [id]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    get iid() {
        return this.props.instanceId;
    }

    setTab(tab) {
        if (tab !== "logs" && this.logs.streaming) {
            this.stopStream();
        }
        this.state.tab = tab;
        this.props.onTabChange?.(tab);
        if (tab === "config" && !this.config.loaded) {
            this.loadConfig();
        }
    }

    // --- Config (odoo.conf de ESTA instancia) -------------------------------
    async loadConfig() {
        this.config.loading = true;
        this.config.error = "";
        const res = await this.orm.call(
            "primate.cloud.dashboard", "get_odoo_config", [this.iid]
        );
        this.config.loading = false;
        this.config.loaded = true;
        this.config.status = res.status;
        if (res.status !== "ok") {
            this.config.error = res.text || "No se pudo leer la configuración.";
            return;
        }
        this.config.editable = res.editable || {};
        this.config.readonly = res.readonly || {};
        this.config.meta = res.meta || [];
        this.config.hash = res.config_hash;
        this.config.isProduction = res.is_production;
        this.config.envName = res.environment_name || "";
        this.config.work = { ...(res.editable || {}) };
        this.config.typedName = "";
    }

    setConfigField(key, value) {
        this.config.work = { ...this.config.work, [key]: value };
    }

    get configChanged() {
        const out = {};
        for (const m of this.config.meta) {
            const cur = String(this.config.work[m.key] ?? "");
            const orig = String(this.config.editable[m.key] ?? "");
            if (cur !== orig) {
                out[m.key] = this.config.work[m.key];
            }
        }
        return out;
    }

    get hasConfigChanges() {
        return Object.keys(this.configChanged).length > 0;
    }

    get canSaveConfig() {
        if (!this.hasConfigChanges || this.config.saving) {
            return false;
        }
        if (this.config.isProduction) {
            return this.config.typedName.trim() === this.config.envName.trim();
        }
        return true;
    }

    async saveConfig() {
        const edits = this.configChanged;
        this.config.saving = true;
        const res = await this.orm.call(
            "primate.cloud.dashboard", "save_odoo_config",
            [this.iid, edits, this.config.hash, this.config.typedName || false]
        );
        this.config.saving = false;
        if (res.status === "ok") {
            this.env.pcm?.notify(
                "Guardado encolado; Odoo se reinicia unos segundos.",
                { type: "success" });
        } else {
            this.env.pcm?.notify(res.text || "No se pudo guardar",
                                 { type: "danger" });
        }
    }

    // --- Dashboard: comandos con las RUTAS del backend (no PCM_PATHS) --------
    get paths() {
        // Del serializer: las rutas del slug de ESTA instancia. Vacío si la
        // máquina no está aprovisionada por PCM (no se inventan paths).
        return this.d.provisioned_by_pcm ? this.d.paths : null;
    }

    get dbName() {
        return this.d.database ? this.d.database.name : "<db>";
    }

    // Hint de acceso por IP + header Host mientras el DNS está pendiente
    // (§8.1 condición 2). En el getter para no pelear con comillas en el QWeb.
    get dnsCurlHint() {
        if (!this.d.dns_pending || !this.d.public_ip) {
            return "";
        }
        return `curl -H "Host: ${this.d.main_url}" http://${this.d.public_ip}/`;
    }

    get shellCommands() {
        const p = this.paths;
        if (!p) {
            return [];
        }
        const db = this.dbName;
        return [
            { label: "Odoo shell",
              cmd: `sudo -u odoo ${p.python} ${p.odoobin} shell -c ${p.conf} -d ${db}` },
            { label: "Actualizar un módulo",
              cmd: `sudo -u odoo ${p.python} ${p.odoobin} -c ${p.conf} -d ${db} -u <modulo> --stop-after-init` },
            { label: "Actualizar todos",
              cmd: `sudo -u odoo ${p.python} ${p.odoobin} -c ${p.conf} -d ${db} -u all --stop-after-init` },
            { label: "Ver log en vivo",
              cmd: `sudo tail -f ${p.logfile}` },
            { label: "Reiniciar Odoo",
              cmd: `sudo systemctl restart ${p.service}` },
        ];
    }

    async copy(text) {
        try {
            await navigator.clipboard.writeText(text);
            this.env.pcm?.notify("Copiado al portapapeles", { type: "success" });
        } catch (_error) {
            this.env.pcm?.notify("No se pudo copiar", { type: "warning" });
        }
    }

    // --- Acciones sobre otros modelos (backup, restore, repo) ---------------
    async runOn(model, id, method, { confirm } = {}) {
        if (!id) {
            return;
        }
        const ran = await runPcmModelAction(
            this.env, this.orm, model, method, [id], { confirm }
        );
        if (ran) {
            await this.load(this.iid);
        }
    }

    backupNow() {
        // El respaldo gestionado corre a nivel entorno (incluye la BD de esta
        // instancia). El detalle de la instancia muestra SUS backups.
        return this.runOn("primate.cloud.environment", this.d.environment_id,
                          "action_run_backup",
                          { confirm: "¿Lanzar un respaldo ahora?" });
    }

    restoreBackup(backupId) {
        return this.runOn("primate.cloud.backup", backupId, "action_restore");
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    // --- Agregar addon (clona en ESTA instancia + registra) -----------------
    toggleAddonForm() {
        this.addon.open = !this.addon.open;
    }

    async submitAddon() {
        if (!this.addon.github_url.trim()) {
            this.env.pcm?.notify("Falta la URL del repositorio", { type: "warning" });
            return;
        }
        this.addon.saving = true;
        const res = await this.orm.call(
            "primate.cloud.dashboard", "add_odoo_addon",
            [this.iid, {
                github_url: this.addon.github_url,
                configured_branch: this.addon.configured_branch || false,
                repo_type: this.addon.repo_type,
                github_token: this.addon.github_token || false,
            }]
        );
        this.addon.saving = false;
        if (res.status === "ok") {
            this.env.pcm?.notify(
                "Addon encolado (clonando + verificando).", { type: "success" });
            Object.assign(this.addon, {
                open: false, github_url: "", configured_branch: "",
                github_token: "",
            });
            await this.load(this.iid);
        } else {
            this.env.pcm?.notify(res.text || "No se pudo agregar el addon",
                                 { type: "danger" });
        }
    }

    // --- Login as / impersonación (Bloque B5) --------------------------------
    toggleLoginAs() {
        this.loginas.open = !this.loginas.open;
        if (this.loginas.open && !this.loginas.db) {
            this.loginas.db = this.d.database ? this.d.database.name : "";
        }
    }

    async loadLoginAsUsers() {
        if (!this.loginas.db) {
            this.env.pcm?.notify("Elegí una base de datos", { type: "warning" });
            return;
        }
        this.loginas.loading = true;
        this.loginas.users = [];
        const res = await this.orm.call(
            "primate.cloud.dashboard", "list_odoo_db_users",
            [this.iid, this.loginas.db]
        );
        this.loginas.loading = false;
        if (res.status === "ok") {
            this.loginas.users = res.users || [];
        } else {
            this.env.pcm?.notify(res.text || "No se pudo listar usuarios",
                                 { type: "danger" });
        }
    }

    async doLoginAs(user) {
        let adminAck = false;
        if (user.is_admin) {
            adminAck = window.confirm(
                `"${user.login}" tiene rol de ADMINISTRACIÓN. ¿Confirmás ` +
                `impersonar un administrador? (queda marcado en la auditoría)`);
            if (!adminAck) {
                return;
            }
        }
        const res = await this.orm.call(
            "primate.cloud.dashboard", "odoo_login_as",
            [this.iid, this.loginas.db, user.id, user.login,
             user.is_admin, adminAck, this.loginas.typedName || false]
        );
        if (res.status === "ok") {
            window.open(res.url, "_blank", "noopener");
            this.env.pcm?.notify("Abriendo sesión como " + user.login,
                                 { type: "success" });
        } else {
            this.env.pcm?.notify(res.text || "No se pudo iniciar la sesión",
                                 { type: "danger" });
        }
    }

    // --- Logs on-demand (SSM) -----------------------------------------------
    async fetchLogs() {
        this.logs.loading = true;
        this.logs.text = "";
        const res = await this.orm.call(
            "primate.cloud.dashboard", "get_odoo_logs",
            [this.iid, this.logs.source, this.logs.lines, this.logs.grep || false]
        );
        this.logs.text = res.text || "(sin salida)";
        this.logs.loading = false;
    }

    downloadLogs() {
        const blob = new Blob([this.logs.text || ""], { type: "text/plain" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${this.d.name || "instancia"}-${this.logs.source}.log`;
        a.click();
        URL.revokeObjectURL(url);
    }

    toggleStream() {
        if (this.logs.streaming) {
            this.stopStream();
        } else {
            this.startStream();
        }
    }

    startStream() {
        this.logs.text = "";
        this.logs.cursor = false;
        this.logs.streaming = true;
        document.addEventListener("visibilitychange", this._onVisibility);
        this.pollStream();
        this._streamTimer = setInterval(() => this.pollStream(), 5000);
    }

    stopStream() {
        this.logs.streaming = false;
        if (this._streamTimer) {
            clearInterval(this._streamTimer);
            this._streamTimer = null;
        }
        document.removeEventListener("visibilitychange", this._onVisibility);
    }

    _handleVisibility() {
        if (document.hidden) {
            if (this._streamTimer) {
                clearInterval(this._streamTimer);
                this._streamTimer = null;
            }
        } else if (this.logs.streaming && !this._streamTimer) {
            this.pollStream();
            this._streamTimer = setInterval(() => this.pollStream(), 5000);
        }
    }

    async pollStream() {
        if (this._polling) {
            return;
        }
        this._polling = true;
        try {
            const res = await this.orm.call(
                "primate.cloud.dashboard", "get_odoo_logs_stream",
                [this.iid, this.logs.source, this.logs.cursor,
                 this.logs.lines, this.logs.grep || false]
            );
            if (res.cursor !== undefined) {
                this.logs.cursor = res.cursor;
            }
            if (res.rotated) {
                const sep = this.logs.text ? "\n" : "";
                this.logs.text += sep + "— el log rotó (archivo nuevo) —";
            }
            if (res.text) {
                const sep = this.logs.text ? "\n" : "";
                let combined = this.logs.text + sep + res.text;
                if (combined.length > 100000) {
                    combined = combined.slice(-100000);
                }
                this.logs.text = combined;
            }
            if (res.status === "error") {
                this.stopStream();
                this.env.pcm?.notify(res.text || "Error de streaming", { type: "warning" });
            }
        } finally {
            this._polling = false;
        }
    }

    money(amount) {
        return (amount || 0).toFixed(2);
    }
}
