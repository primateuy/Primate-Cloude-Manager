/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";

/**
 * Detalle de un proyecto = la vista del CLIENTE (eje cliente, R6): sus
 * instancias (estén en el servidor que estén) y su reparto de costo del mes
 * (cost.share de R5) con la honestidad obligatoria (método + "datos al").
 * Solo lectura. Datos de `primate.cloud.dashboard.get_project_detail`.
 */
export class ProyectoDetalle extends Component {
    static template = "primate_cloud_manager.ProyectoDetalle";
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
            "primate.cloud.dashboard", "get_project_detail", [id]
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

    money(amount) {
        return (amount || 0).toFixed(2);
    }
}
