/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { pcmErrorMessage } from "../pcm_actions";

/**
 * Confirmación de borrado de un registro DNS con FRICCIÓN ALTA (borrar el
 * registro equivocado tira un dominio): hay que escribir el nombre exacto y,
 * para registros productivos o de origen desconocido, marcar la conformidad.
 *
 * No agrega lógica: delega en el TransientModel existente
 * (`primate.cloud.dns.delete.wizard`), que revalida las salvaguardas
 * server-side y encola el borrado en Route 53.
 */
export class DnsDeleteForm extends Component {
    static template = "primate_cloud_manager.DnsDeleteForm";
    static components = { PcmField };
    static props = {
        recordId: { type: Number },
        onClose: { type: Function },
        onSaved: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            blocked: "",
            submitting: false,
            error: "",
            name: "",
            record_value: "",
            environment_name: "",
            needs_ack: false,
            confirm_name: "",
            acknowledge: false,
        });
        onWillStart(() => this.loadRecord());
    }

    async loadRecord() {
        const recs = await this.orm.read(
            "primate.cloud.dns.record", [this.props.recordId],
            ["name", "record_value", "delete_needs_ack", "environment_id", "state"]);
        this.state.loading = false;
        const rec = recs[0];
        if (!rec) {
            this.state.blocked = "El registro ya no existe.";
            return;
        }
        if (rec.state === "deleted") {
            this.state.blocked = "El registro ya está eliminado.";
            return;
        }
        this.state.name = rec.name || "";
        this.state.record_value = rec.record_value || "";
        this.state.environment_name = rec.environment_id ? rec.environment_id[1] : "";
        this.state.needs_ack = rec.delete_needs_ack;
    }

    get nameMatches() {
        return (this.state.confirm_name || "").trim() === (this.state.name || "").trim();
    }

    get canSubmit() {
        return this.nameMatches && (!this.state.needs_ack || this.state.acknowledge);
    }

    async submit() {
        if (this.state.submitting || !this.canSubmit) {
            return;
        }
        this.state.error = "";
        this.state.submitting = true;
        try {
            const ids = await this.orm.create(
                "primate.cloud.dns.delete.wizard", [{
                    record_id: this.props.recordId,
                    confirm_name: this.state.confirm_name,
                    acknowledge: this.state.acknowledge,
                }]);
            await this.orm.call(
                "primate.cloud.dns.delete.wizard", "action_confirm", [ids]);
            this.env.pcm?.notify("Borrado DNS encolado.", { type: "success" });
            if (this.props.onSaved) {
                this.props.onSaved();
            }
            this.props.onClose();
        } catch (error) {
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.submitting = false;
        }
    }
}
