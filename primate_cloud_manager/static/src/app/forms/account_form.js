/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

/**
 * Formulario OWL de cuenta AWS (crear/editar) — la pantalla MÁS sensible: maneja
 * las credenciales que dan acceso a toda la infraestructura del cliente.
 *
 * SECRETO (Secret Access Key): mismo patrón que el token de Repos. El input
 * arranca VACÍO; el valor tipeado viaja solo en el guardado (se cifra
 * server-side); en edición se conserva el guardado si se deja vacío. Nunca se
 * precarga (ni la máscara); el serializer sólo informa si HAY uno (secret_set).
 *
 * Access Key ID: identificador (inútil sin el secreto) → se muestra en CLARO,
 * solo-admin. Se precarga en edición y se reenvía tal cual (re-guardar sin
 * tocarlo conserva el mismo valor).
 *
 * Credenciales: solo para group_cloud_admin (mismo gate que el modelo).
 */
export class AccountForm extends Component {
    static template = "primate_cloud_manager.AccountForm";
    static components = { PcmField, PcmSelect };
    static props = {
        mode: { type: String, optional: true }, // "create" | "edit"
        recordId: { type: Number, optional: true },
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
            errors: {},
            isCloudAdmin: false,
            secretSet: false,
            regions: [],
            authMethods: [],
            // valores
            name: "",
            aws_account_id: "",
            default_region: "",
            auth_method: "access_key",
            notes: "",
            iam_access_key_id: "",
            iam_secret_access_key: "",  // nunca se precarga
            role_arn: "",
            external_id: "",
        });
        onWillStart(() => this.load());
    }

    get isEdit() {
        return this.props.mode === "edit" && !!this.props.recordId;
    }

    get isAccessKey() { return this.state.auth_method === "access_key"; }
    get isAssumeRole() { return this.state.auth_method === "assume_role"; }

    async load() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_account_form_data",
            [this.props.recordId || false]);
        this.state.loading = false;
        if (this.isEdit && !d.id) {
            this.state.blocked = "La cuenta ya no existe.";
            return;
        }
        Object.assign(this.state, {
            isCloudAdmin: d.is_cloud_admin,
            secretSet: d.secret_set,
            regions: d.regions || [],
            authMethods: d.auth_methods || [],
            name: d.name || "",
            aws_account_id: d.aws_account_id || "",
            default_region: d.default_region || "",
            auth_method: d.auth_method || "access_key",
            notes: d.notes || "",
            iam_access_key_id: d.iam_access_key_id || "",  // claro (identificador)
            iam_secret_access_key: "",                      // SIEMPRE vacío
            role_arn: d.role_arn || "",
            external_id: d.external_id || "",
        });
    }

    get secretHelp() {
        if (this.isEdit && this.state.secretSet) {
            return "Ya hay un Secret guardado (cifrado). Dejalo vacío para " +
                "conservarlo; escribí uno nuevo para reemplazarlo.";
        }
        return "Se guarda cifrado, nunca se muestra en claro.";
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setSelect(field, value) {
        this.state[field] = value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    _validateClient() {
        const errors = {};
        if (!(this.state.name || "").trim()) {
            errors.name = "Poné un nombre para la cuenta.";
        }
        if (!this.state.default_region) {
            errors.default_region = "Elegí la región por defecto de la cuenta.";
        }
        if (this.state.isCloudAdmin && this.isAssumeRole
                && !(this.state.role_arn || "").trim()) {
            errors.role_arn = "Indicá el Role ARN a asumir.";
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
                name: this.state.name,
                aws_account_id: this.state.aws_account_id,
                default_region: this.state.default_region,
                auth_method: this.state.auth_method,
                notes: this.state.notes,
            };
            // Credenciales: solo admin (el gate del modelo también las protege).
            if (this.state.isCloudAdmin) {
                if (this.isAccessKey) {
                    // Access Key ID: identificador, se reenvía tal cual (claro).
                    vals.iam_access_key_id = this.state.iam_access_key_id;
                    // Secret: SOLO si el usuario escribió uno (así no se pisa el
                    // guardado en edición, y nunca se reenvía la máscara).
                    if ((this.state.iam_secret_access_key || "").trim()) {
                        vals.iam_secret_access_key =
                            this.state.iam_secret_access_key.trim();
                    }
                } else if (this.isAssumeRole) {
                    vals.role_arn = this.state.role_arn;
                    vals.external_id = this.state.external_id;
                }
            }
            let savedId = this.props.recordId;
            if (this.isEdit) {
                await this.orm.write(
                    "primate.cloud.account", [this.props.recordId], vals);
            } else {
                const ids = await this.orm.create("primate.cloud.account", [vals]);
                savedId = ids[0];
            }
            this.env.pcm?.notify(
                this.isEdit ? "Cuenta actualizada." : "Cuenta creada.",
                { type: "success" });
            if (this.props.onSaved) {
                this.props.onSaved(savedId);
            }
            this.props.onClose();
        } catch (error) {
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.submitting = false;
        }
    }
}
