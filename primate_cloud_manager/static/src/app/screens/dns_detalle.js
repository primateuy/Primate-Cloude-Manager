/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Detalle de un registro DNS: datos generales y asociación (entorno, cuenta).
 * Solo lectura, sin acciones.
 * Datos de `primate.cloud.dashboard.get_dns_detail`.
 */
export class DnsDetalle extends Component {
    static template = "primate_cloud_manager.DnsDetalle";
    static components = { PcmStatusBadge };
    static props = {
        recordId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ loading: true, data: {} });
        onWillStart(() => this.load(this.props.recordId));
        onWillUpdateProps((next) => {
            if (next.recordId !== this.props.recordId) {
                this.load(next.recordId);
            }
        });
    }

    async load(id) {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_dns_detail", [id]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    async _run(method) {
        const ran = await runPcmModelAction(
            this.env, this.orm, "primate.cloud.dns.record", method,
            [this.props.recordId]);
        if (ran) {
            await this.load(this.props.recordId);
        }
    }

    editDns() { return this._run("action_open_edit"); }
    deleteDns() { return this._run("action_open_delete"); }
    checkDns() { return this._run("action_check_sync_state"); }
}
