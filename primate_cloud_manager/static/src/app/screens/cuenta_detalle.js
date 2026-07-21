/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";
import { AccountForm } from "../forms/account_form";

/**
 * Detalle de una cuenta AWS: datos generales, notas y recursos asociados
 * (entornos y servidores). Acciones: validar conexión y sincronizar recursos.
 * Datos de `primate.cloud.dashboard.get_account_detail` (no expone credenciales).
 */
export class CuentaDetalle extends Component {
    static template = "primate_cloud_manager.CuentaDetalle";
    static components = { PcmStatusBadge };
    static props = {
        recordId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.account";
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
            "primate.cloud.dashboard", "get_account_detail", [id]
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

    // Editar la cuenta (credenciales) en el form OWL. Recarga al guardar.
    editAccount() {
        this.env.pcm.openForm(AccountForm, {
            mode: "edit", recordId: this.props.recordId,
            onSaved: () => this.load(this.props.recordId),
        }, "Editar cuenta AWS");
    }
}
