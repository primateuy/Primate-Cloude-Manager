/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Detalle de un despliegue: datos generales, asociación y log de ejecución.
 * Acciones: ejecutar (en borrador) y revertir (si fue exitoso y no es reversión).
 * Datos de `primate.cloud.dashboard.get_deployment_detail`.
 */
export class DespliegueDetalle extends Component {
    static template = "primate_cloud_manager.DespliegueDetalle";
    static components = { PcmStatusBadge };
    static props = {
        recordId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.deployment";
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
            "primate.cloud.dashboard", "get_deployment_detail", [id]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    // Ejecuta un método del modelo (opcionalmente confirmando) y recarga.
    async runMethod(method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, this.model, method, [this.props.recordId], { confirm }
        );
        if (ran) {
            await this.load(this.props.recordId);
        }
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }
}
