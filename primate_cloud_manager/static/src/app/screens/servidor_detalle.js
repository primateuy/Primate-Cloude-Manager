/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

// Layout estándar de una instancia aprovisionada por PCM (install_odoo.sh).
// Solo se usa si d.provisioned_by_pcm; para importadas se muestra "desconocido"
// en vez de inventar paths (criterio ausente≠inventado, igual que en métricas).
const PCM_PATHS = {
    home: "/opt/odoo",
    conf: "/etc/odoo/odoo.conf",
    python: "/opt/odoo/venv/bin/python3",
    pip: "/opt/odoo/venv/bin/pip",
    odoobin: "/opt/odoo/odoo/odoo-bin",
    logfile: "/var/log/odoo/odoo.log",
    service: "odoo",
};

/**
 * Panel de gestión de una instancia/servidor EC2 (estilo CloudPepper), con tabs:
 * Dashboard (info + paths/comandos + BD), Logs (visor on-demand), Backups (motor
 * Fase 8) y Addons (repos del entorno). La tab Config (editar odoo.conf) llega en
 * un bloque posterior. Todo es SOLO lectura + acciones ya existentes; sin backend
 * nuevo en este bloque. Datos de `get_server_detail` (read-only).
 */
export class ServidorDetalle extends Component {
    static template = "primate_cloud_manager.ServidorDetalle";
    static components = { PcmStatusBadge };
    static props = {
        serverId: { type: Number },
        initialTab: { type: String, optional: true },
        onTabChange: { type: Function, optional: true },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.ec2.instance";
        // La tab inicial viene del deep-link (hash) para sobrevivir al F5.
        this.state = useState({
            loading: true, data: {},
            tab: this.props.initialTab || "dashboard",
        });
        // Visor de logs: on-demand (fetch completo) + streaming incremental.
        this.logs = useState({
            source: "odoo", lines: 200, grep: "", text: "", loading: false,
            streaming: false, cursor: false,
        });
        // Editor del odoo.conf (tab Config, Bloque B3). Carga perezosa.
        this.config = useState({
            loaded: false, loading: false, status: "", error: "",
            editable: {}, readonly: {}, meta: [], hash: false, work: {},
            isProduction: false, envName: "", typedName: "", saving: false,
        });
        // Alta de addon (tab Addons, Bloque B4).
        this.addon = useState({
            open: false, github_url: "", configured_branch: "",
            repo_type: "custom_client", github_token: "", saving: false,
        });
        // Login as / impersonación (tab Dashboard, Bloque B5).
        this.loginas = useState({
            open: false, db: "", users: [], loading: false, typedName: "",
        });
        this._streamTimer = null;   // handle del setInterval (no reactivo)
        this._polling = false;      // evita solapar polls si uno tarda
        this._onVisibility = () => this._handleVisibility();
        onWillStart(() => this.load(this.props.serverId));
        onWillUpdateProps((next) => {
            if (next.serverId !== this.props.serverId) {
                this.stopStream();  // otra instancia → cortar el streaming viejo
                this.load(next.serverId);
            }
        });
        onWillUnmount(() => this.stopStream());
    }

    async load(id) {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_server_detail", [id]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    setTab(tab) {
        // Al salir de la tab Logs, cortar el streaming (no pollear en background).
        if (tab !== "logs" && this.logs.streaming) {
            this.stopStream();
        }
        this.state.tab = tab;
        // Refleja la tab en el hash del router (deep-link que sobrevive al F5).
        this.props.onTabChange?.(tab);
        // Carga perezosa del odoo.conf al abrir la tab Config (lectura SSM).
        if (tab === "config" && !this.config.loaded) {
            this.loadConfig();
        }
    }

    // --- Config (odoo.conf) -------------------------------------------------
    async loadConfig() {
        this.config.loading = true;
        this.config.error = "";
        const res = await this.orm.call(
            "primate.cloud.dashboard", "get_instance_config",
            [this.props.serverId]
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

    // Escribe un campo editado en la copia de trabajo (bool como "True"/"False").
    setConfigField(key, value) {
        this.config.work = { ...this.config.work, [key]: value };
    }

    // Solo los campos realmente cambiados (compara trabajo vs original).
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

    // En prod el guardado exige tipear el nombre exacto del entorno.
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
            "primate.cloud.dashboard", "save_instance_config",
            [this.props.serverId, edits, this.config.hash,
             this.config.typedName || false]
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

    // --- Dashboard: paths + comandos listos para copiar -----------------------
    // Solo para instancias aprovisionadas por PCM (layout garantizado). Para las
    // importadas devuelve null → la UI muestra "desconocido (no aprovisionada)".
    get paths() {
        return this.d.provisioned_by_pcm ? PCM_PATHS : null;
    }

    get dbName() {
        const dbs = this.d.databases || [];
        return dbs.length ? dbs[0].name : "<db>";
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
            { label: "pip install",
              cmd: `sudo -u odoo ${p.pip} install <paquete>` },
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

    // --- Acciones ------------------------------------------------------------
    // Ejecuta un método del modelo de la instancia (opcionalmente confirmando).
    async runMethod(method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, this.model, method, [this.props.serverId], { confirm }
        );
        if (ran) {
            await this.load(this.props.serverId);
        }
    }

    // Ejecuta un método sobre otro modelo/registro (backup a nivel entorno,
    // acciones de repo, restore de un backup) reusando el mismo interceptor.
    async runOn(model, id, method, { confirm } = {}) {
        if (!id) {
            return;
        }
        const ran = await runPcmModelAction(
            this.env, this.orm, model, method, [id], { confirm }
        );
        if (ran) {
            await this.load(this.props.serverId);
        }
    }

    // Backups (motor Fase 8): respaldar es a nivel ENTORNO.
    backupNow() {
        return this.runOn("primate.cloud.environment", this.d.environment_id,
                          "action_run_backup",
                          { confirm: "¿Lanzar un respaldo ahora de este entorno?" });
    }

    restoreBackup(backupId) {
        // action_restore abre el wizard de restore en el drawer.
        return this.runOn("primate.cloud.backup", backupId, "action_restore");
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    // --- Agregar addon (clona en la instancia + registra) -------------------
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
            "primate.cloud.dashboard", "add_instance_addon",
            [this.props.serverId, {
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
            await this.load(this.props.serverId);   // refresca la lista de repos
        } else {
            this.env.pcm?.notify(res.text || "No se pudo agregar el addon",
                                 { type: "danger" });
        }
    }

    async refreshMetrics() {
        await this.runMethod("action_refresh_metrics");
    }

    // --- Login as / impersonación (Bloque B5) --------------------------------
    toggleLoginAs() {
        this.loginas.open = !this.loginas.open;
        if (this.loginas.open && !this.loginas.db) {
            const dbs = this.d.databases || [];
            this.loginas.db = dbs.length ? dbs[0].name : "";
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
            "primate.cloud.dashboard", "list_instance_db_users",
            [this.props.serverId, this.loginas.db]
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
        // Destino con rol admin → paso extra explícito, nunca un click más.
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
            "primate.cloud.dashboard", "login_as",
            [this.props.serverId, this.loginas.db, user.id, user.login,
             user.is_admin, adminAck, this.loginas.typedName || false]
        );
        if (res.status === "ok") {
            // Sesión same-origin: se abre el Odoo del cliente ya logueado.
            window.open(res.url, "_blank", "noopener");
            this.env.pcm?.notify("Abriendo sesión como " + user.login,
                                 { type: "success" });
        } else {
            this.env.pcm?.notify(res.text || "No se pudo iniciar la sesión",
                                 { type: "danger" });
        }
    }

    // --- Logs on-demand (SSM). No persiste; se muestra y se puede descargar. ---
    async fetchLogs() {
        this.logs.loading = true;
        this.logs.text = "";
        const res = await this.orm.call(
            "primate.cloud.dashboard", "get_instance_logs",
            [this.props.serverId, this.logs.source, this.logs.lines,
             this.logs.grep || false]
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

    // --- Streaming incremental (polla cada ~5s solo mientras la tab está viva) ---
    toggleStream() {
        if (this.logs.streaming) {
            this.stopStream();
        } else {
            this.startStream();
        }
    }

    startStream() {
        // Arranca limpio: el primer poll (sin cursor) trae la cola actual.
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

    // Pausa el polling con la pestaña oculta; reanuda al volver (ahorra SSM).
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
            return;   // no solapar si un poll tarda más que el intervalo
        }
        this._polling = true;
        try {
            const res = await this.orm.call(
                "primate.cloud.dashboard", "get_instance_logs_stream",
                [this.props.serverId, this.logs.source, this.logs.cursor,
                 this.logs.lines, this.logs.grep || false]
            );
            if (res.cursor !== undefined) {
                this.logs.cursor = res.cursor;
            }
            // El log rotó (logrotate por slug de R3): el backend ya reseteó el
            // offset y leyó desde el archivo nuevo; se avisa en el propio hilo.
            if (res.rotated) {
                const sep = this.logs.text ? "\n" : "";
                this.logs.text += sep + "— el log rotó (archivo nuevo) —";
            }
            if (res.text) {
                const sep = this.logs.text ? "\n" : "";
                let combined = this.logs.text + sep + res.text;
                // Cota de memoria: conservar los últimos ~100k caracteres.
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
}
