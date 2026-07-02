/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";

/**
 * Detalle de un repositorio: datos generales, asociación, módulos detectados y
 * commits sincronizados. Acciones: verificar estado, sincronizar commits y
 * detectar módulos. Datos de `primate.cloud.dashboard.get_repository_detail`.
 */
export class RepositorioDetalle extends Component {
    static template = "primate_cloud_manager.RepositorioDetalle";
    static components = { PcmStatusBadge };
    static props = {
        recordId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.repository";
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
            "primate.cloud.dashboard", "get_repository_detail", [id]
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
