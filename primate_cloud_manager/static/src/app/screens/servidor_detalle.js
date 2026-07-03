/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Detalle de un servidor EC2: datos + acciones (start/stop/restart/sync/comando/
 * terminar) que invocan los métodos existentes del modelo. Los wizards abren en
 * el drawer lateral; las notificaciones salen como toast (vía env.pcm). Drill-
 * through a entorno, cuenta y bases asociadas.
 * Datos de `primate.cloud.dashboard.get_server_detail` (solo lectura).
 */
export class ServidorDetalle extends Component {
    static template = "primate_cloud_manager.ServidorDetalle";
    static components = { PcmStatusBadge };
    static props = {
        serverId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.ec2.instance";
        this.state = useState({ loading: true, data: {} });
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

    // Ejecuta un método del modelo (opcionalmente confirmando) y recarga.
    async runMethod(method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, this.model, method, [this.props.serverId], { confirm }
        );
        if (ran) {
            await this.load(this.props.serverId);
        }
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    async refreshMetrics() {
        await this.runMethod("action_refresh_metrics");
    }

    // Trae logs por SSM (no persiste). El texto se muestra y se puede descargar.
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
