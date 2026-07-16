/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Pantalla de un SERVIDOR (la MÁQUINA EC2). R4-B6: SOLO lo de la máquina —
 * IP, tipo, estado, red, métricas, ciclo de vida (start/stop/restart/
 * terminate) y la LISTA de instancias que hospeda (cada una con su cliente).
 * Config, logs de Odoo, addons, login-as y backups NO viven acá: son de la
 * INSTANCIA (pantalla propia). El panel siempre fue de la instancia; ahora
 * el modelo por fin lo refleja y la máquina deja de fingir que "es" un Odoo.
 */
export class ServidorDetalle extends Component {
    static template = "primate_cloud_manager.ServidorDetalle";
    static components = { PcmStatusBadge };
    static props = {
        serverId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
        onOpenInstance: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.ec2.instance";
        this.state = useState({ loading: true, data: {} });
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

    // Acción del ciclo de vida de la MÁQUINA (start/stop/restart/terminate/sync).
    async runMethod(method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, this.model, method, [this.props.serverId], { confirm }
        );
        if (ran) {
            await this.load(this.props.serverId);
        }
    }

    async refreshMetrics() {
        await this.runMethod("action_refresh_metrics");
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    // Abre la pantalla de una INSTANCIA hospedada (el panel Odoo vive ahí).
    openInstance(instanceId, name) {
        if (this.props.onOpenInstance && instanceId) {
            this.props.onOpenInstance(instanceId, name);
        }
    }
}
