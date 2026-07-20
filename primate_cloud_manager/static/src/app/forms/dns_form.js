/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

// Tipos con una glosa de para-qué-sirve (DNS es "la confusa": el label ayuda).
const TYPE_OPTIONS = [
    { value: "A", label: "A — dirección IPv4" },
    { value: "CNAME", label: "CNAME — alias a otro host" },
    { value: "TXT", label: "TXT — texto" },
    { value: "MX", label: "MX — servidor de correo" },
];
const VALUE_HINTS = {
    A: "Una IPv4 por línea. Ej.: 203.0.113.10",
    CNAME: "Un único hostname. Ej.: destino.ejemplo.com",
    TXT: "Texto libre, uno por línea.",
    MX: "'prioridad host' por línea. Ej.: 10 mail.ejemplo.com",
};

/**
 * Formulario OWL para crear/editar un registro DNS (reemplaza el form nativo
 * del wizard embebido). Maneja validación de forma con copy accionable inline
 * y muestra el error del servidor sin traceback ni diálogo "Oops".
 *
 * No agrega lógica de negocio: delega en el TransientModel existente
 * (`primate.cloud.dns.record.wizard`), que valida por tipo y encola en
 * Route 53 server-side. En edición bloquea los registros alias (igual que el
 * wizard) con un mensaje claro.
 */
export class DnsForm extends Component {
    static template = "primate_cloud_manager.DnsForm";
    static components = { PcmField, PcmSelect };
    static props = {
        mode: { type: String, optional: true }, // "create" | "edit"
        recordId: { type: Number, optional: true },
        accountId: { type: [Number, Boolean], optional: true },
        environmentId: { type: [Number, Boolean], optional: true },
        hostedZoneId: { type: String, optional: true },
        onClose: { type: Function },
        onSaved: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.typeOptions = TYPE_OPTIONS;
        this.state = useState({
            loading: false,
            blocked: "",        // motivo por el que no se puede editar (alias)
            submitting: false,
            error: "",          // error accionable global (validación/servidor)
            errors: {},         // errores por campo
            name: "",
            record_type: "A",
            record_value: "",
            ttl: 300,
            hosted_zone_id: this.props.hostedZoneId || "",
            account_id: this.props.accountId || false,
            environment_id: this.props.environmentId || false,
        });
        if (this.isEdit) {
            onWillStart(() => this.loadRecord());
        }
    }

    get isEdit() {
        return this.props.mode === "edit" && !!this.props.recordId;
    }

    get valueHint() {
        return VALUE_HINTS[this.state.record_type] || "";
    }

    async loadRecord() {
        this.state.loading = true;
        const recs = await this.orm.read(
            "primate.cloud.dns.record", [this.props.recordId],
            ["name", "record_type", "record_value", "ttl", "hosted_zone_id",
             "is_alias", "account_id", "environment_id"]);
        this.state.loading = false;
        const rec = recs[0];
        if (!rec) {
            this.state.blocked = "El registro ya no existe.";
            return;
        }
        if (rec.is_alias) {
            this.state.blocked =
                "Este es un registro alias de Route 53 (apunta a un recurso " +
                "AWS: ELB, CloudFront, S3). Gestionalo en la consola de AWS; " +
                "PCM no lo edita para no romperlo.";
            return;
        }
        Object.assign(this.state, {
            name: rec.name || "",
            record_type: rec.record_type || "A",
            record_value: (rec.record_value || "").replace(/, /g, "\n"),
            ttl: rec.ttl || 300,
            hosted_zone_id: rec.hosted_zone_id || "",
            account_id: rec.account_id ? rec.account_id[0] : false,
            environment_id: rec.environment_id ? rec.environment_id[0] : false,
        });
    }

    setType(value) {
        this.state.record_type = value;
        this.state.errors = { ...this.state.errors, record_value: "" };
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    onTtlInput(ev) {
        this.state.ttl = parseInt(ev.target.value, 10) || 0;
    }

    // Validación de forma mínima en cliente: feedback inmediato y accionable
    // (no el "Missing required fields" nativo). La validación por tipo (IPv4,
    // hostname, MX...) la hace el servidor de forma autoritativa.
    _validateClient() {
        const errors = {};
        if (!this.isEdit) {
            if (!(this.state.name || "").trim()) {
                errors.name =
                    "Poné el nombre del registro (ej.: forum.primate.cloud).";
            }
            if (!(this.state.hosted_zone_id || "").trim()) {
                errors.hosted_zone_id =
                    "Falta el Hosted Zone ID de Route 53. Sincronizá las zonas " +
                    "desde AWS o pegalo de la consola.";
            }
        }
        if (!(this.state.record_value || "").trim()) {
            errors.record_value = "El registro necesita al menos un valor.";
        }
        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    async submit() {
        if (this.state.submitting || this.state.blocked) {
            return;
        }
        this.state.error = "";
        if (!this._validateClient()) {
            return;
        }
        this.state.submitting = true;
        try {
            const vals = {
                record_id: this.props.recordId || false,
                account_id: this.state.account_id || false,
                environment_id: this.state.environment_id || false,
                hosted_zone_id: this.state.hosted_zone_id,
                name: this.state.name,
                record_type: this.state.record_type,
                record_value: this.state.record_value,
                ttl: this.state.ttl || 300,
            };
            const ids = await this.orm.create(
                "primate.cloud.dns.record.wizard", [vals]);
            await this.orm.call(
                "primate.cloud.dns.record.wizard", "action_confirm", [ids]);
            this.env.pcm?.notify(
                this.isEdit
                    ? "Cambio DNS encolado (aplicando en Route 53)."
                    : "Registro DNS encolado (creando en Route 53).",
                { type: "success" });
            if (this.props.onSaved) {
                this.props.onSaved();
            }
            this.props.onClose();
        } catch (error) {
            // Copy accionable, sin traceback ni diálogo stock "Oops".
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.submitting = false;
        }
    }
}
