/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
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
        // Visor de logs on-demand (no persiste; se trae por SSM al pedirlo).
        this.logs = useState({
            source: "odoo", lines: 200, grep: "", text: "", loading: false,
        });
        onWillStart(() => this.load(this.props.serverId));
        onWillUpdateProps((next) => {
            if (next.serverId !== this.props.serverId) {
                this.load(next.serverId);
            }
        });
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
        this.state.tab = tab;
        // Refleja la tab en el hash del router (deep-link que sobrevive al F5).
        this.props.onTabChange?.(tab);
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

    async refreshMetrics() {
        await this.runMethod("action_refresh_metrics");
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
}
