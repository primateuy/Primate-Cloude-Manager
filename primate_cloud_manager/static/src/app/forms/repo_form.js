/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

const TYPE_OPTIONS = [
    { value: "odoo_core", label: "Odoo Core" },
    { value: "odoo_enterprise", label: "Odoo Enterprise" },
    { value: "oca", label: "OCA" },
    { value: "custom_primate", label: "Custom Primate" },
    { value: "custom_client", label: "Custom Cliente" },
];
// Máscara del token (igual que el modelo): nunca llega el token en claro al front.
const TOKEN_MASK = "********";

/**
 * Formulario OWL para crear/editar un repositorio Git (reemplaza el form nativo
 * inline). Escribe el modelo directo por ORM (no hay wizard). Errores del
 * servidor accionables inline, sin traceback.
 *
 * SECRETO: el token de GitHub nunca viaja en claro. En edición el input arranca
 * VACÍO (no se precarga ni la máscara); si el usuario lo deja vacío se conserva
 * el guardado, si escribe uno nuevo lo reemplaza (el modelo lo cifra e ignora la
 * máscara).
 */
export class RepoForm extends Component {
    static template = "primate_cloud_manager.RepoForm";
    static components = { PcmField, PcmSelect };
    static props = {
        mode: { type: String, optional: true }, // "create" | "edit"
        recordId: { type: Number, optional: true },
        environmentId: { type: [Number, Boolean], optional: true },
        onClose: { type: Function },
        onSaved: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.typeOptions = TYPE_OPTIONS;
        this.state = useState({
            loading: false,
            blocked: "",
            submitting: false,
            error: "",
            errors: {},
            tokenAlreadySet: false,
            name: "",
            repo_type: "custom_primate",
            github_url: "",
            organization: "",
            configured_branch: "",
            local_path: "",
            github_token: "",
        });
        if (this.isEdit) {
            onWillStart(() => this.loadRecord());
        }
    }

    get isEdit() {
        return this.props.mode === "edit" && !!this.props.recordId;
    }

    async loadRecord() {
        this.state.loading = true;
        // github_token se lee ENMASCARADO (el compute del modelo nunca lo
        // devuelve en claro): sólo sirve para saber si hay uno guardado.
        const recs = await this.orm.read(
            "primate.cloud.repository", [this.props.recordId],
            ["name", "repo_type", "github_url", "organization",
             "configured_branch", "local_path", "github_token"]);
        this.state.loading = false;
        const r = recs[0];
        if (!r) {
            this.state.blocked = "El repositorio ya no existe.";
            return;
        }
        Object.assign(this.state, {
            name: r.name || "",
            repo_type: r.repo_type || "custom_primate",
            github_url: r.github_url || "",
            organization: r.organization || "",
            configured_branch: r.configured_branch || "",
            local_path: r.local_path || "",
            tokenAlreadySet: r.github_token === TOKEN_MASK,
            github_token: "",  // nunca se precarga (ni la máscara)
        });
    }

    get tokenHelp() {
        if (this.isEdit && this.state.tokenAlreadySet) {
            return "Ya hay un token guardado (cifrado). Dejalo vacío para " +
                "conservarlo; escribí uno nuevo para reemplazarlo.";
        }
        return "Opcional, para repos privados. Se guarda cifrado, nunca se " +
            "muestra en claro.";
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setType(value) {
        this.state.repo_type = value;
    }

    _validateClient() {
        const errors = {};
        if (!(this.state.name || "").trim()) {
            errors.name = "Poné un nombre para el repositorio.";
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
                repo_type: this.state.repo_type,
                github_url: this.state.github_url,
                organization: this.state.organization,
                configured_branch: this.state.configured_branch,
                local_path: this.state.local_path,
            };
            // El token sólo se manda si el usuario escribió uno (así en edición
            // no se pisa el guardado, y nunca se reenvía la máscara).
            if ((this.state.github_token || "").trim()) {
                vals.github_token = this.state.github_token.trim();
            }
            if (this.isEdit) {
                await this.orm.write(
                    "primate.cloud.repository", [this.props.recordId], vals);
            } else {
                vals.environment_id = this.props.environmentId || false;
                await this.orm.create("primate.cloud.repository", [vals]);
            }
            this.env.pcm?.notify(
                this.isEdit ? "Repositorio actualizado." : "Repositorio creado.",
                { type: "success" });
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
