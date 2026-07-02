/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";

/**
 * Detalle de una base de datos: datos generales, almacenamiento/RDS y
 * asociación (entorno, cuenta, servidor). Solo lectura, sin acciones.
 * Datos de `primate.cloud.dashboard.get_database_detail`.
 */
export class BaseDatosDetalle extends Component {
    static template = "primate_cloud_manager.BaseDatosDetalle";
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
            "primate.cloud.dashboard", "get_database_detail", [id]
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
}
