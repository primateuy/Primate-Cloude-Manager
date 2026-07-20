/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

const GIT_TYPES = ["pull", "checkout_branch", "checkout_commit"];

/**
 * Formulario OWL de "Nuevo despliegue": crea el deploy en estado borrador (la
 * EJECUCIÓN vive en la pantalla del deploy, con su botón Ejecutar). Deja
 * INEQUÍVOCA la instancia destino (un deploy al servidor equivocado es un
 * problema): se elige explícitamente, con la instancia primaria por defecto.
 *
 * Condicional por tipo (como en Crear entorno): los tipos git muestran el repo;
 * checkout de rama/commit muestran su campo; actualización de módulos, la lista.
 * Errores del servidor accionables inline, sin traceback.
 */
export class DeployForm extends Component {
    static template = "primate_cloud_manager.DeployForm";
    static components = { PcmField, PcmSelect };
    static props = {
        environmentId: { type: Number },
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
            environmentName: "",
            instances: [],
            repositories: [],
            deploymentTypes: [],
            // valores
            instance_id: false,
            deployment_type: "pull",
            repository_id: false,
            target_branch: "",
            target_commit: "",
            module_names: "",
        });
        onWillStart(() => this.load());
    }

    async load() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_deployment_form_data",
            [this.props.environmentId]);
        this.state.loading = false;
        if (!d || !d.environment_id) {
            this.state.blocked = "El entorno ya no existe.";
            return;
        }
        Object.assign(this.state, {
            environmentName: d.environment_name || "",
            instances: d.instances || [],
            repositories: d.repositories || [],
            deploymentTypes: d.deployment_types || [],
            instance_id: d.primary_instance_id || false,
        });
    }

    get isGit() { return GIT_TYPES.includes(this.state.deployment_type); }
    get isCheckoutBranch() { return this.state.deployment_type === "checkout_branch"; }
    get isCheckoutCommit() { return this.state.deployment_type === "checkout_commit"; }
    get isModuleUpdate() { return this.state.deployment_type === "module_update"; }

    get instanceOptions() {
        return [{ value: "", label: "(instancia primaria del entorno)" },
                ...this.state.instances];
    }

    get repoOptions() {
        return [{ value: "", label: "(elegí un repositorio)" },
                ...this.state.repositories];
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setType(value) {
        this.state.deployment_type = value;
        this.state.errors = {};
    }

    setInstance(value) {
        this.state.instance_id = value ? parseInt(value, 10) : false;
    }

    setRepo(value) {
        this.state.repository_id = value ? parseInt(value, 10) : false;
        if (this.state.errors.repository_id) {
            this.state.errors = { ...this.state.errors, repository_id: "" };
        }
    }

    // Validación de forma en cliente (misma lógica que action_run), accionable.
    _validateClient() {
        const errors = {};
        if (this.isGit && !this.state.repository_id) {
            errors.repository_id = "Elegí el repositorio sobre el que corre el deploy.";
        }
        if (this.isCheckoutBranch && !(this.state.target_branch || "").trim()) {
            errors.target_branch = "Indicá la rama a la que hacer checkout.";
        }
        if (this.isCheckoutCommit && !(this.state.target_commit || "").trim()) {
            errors.target_commit = "Indicá el commit a la que hacer checkout.";
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
                environment_id: this.props.environmentId,
                instance_id: this.state.instance_id || false,
                deployment_type: this.state.deployment_type,
                repository_id: this.isGit ? (this.state.repository_id || false) : false,
                target_branch: this.isCheckoutBranch ? this.state.target_branch : false,
                target_commit: this.isCheckoutCommit ? this.state.target_commit : false,
                module_names: this.isModuleUpdate ? this.state.module_names : false,
            };
            const ids = await this.orm.create("primate.cloud.deployment", [vals]);
            this.env.pcm?.notify(
                "Despliegue creado (borrador). Revisá el destino y ejecutalo " +
                "desde su pantalla.", { type: "success" });
            if (this.props.onSaved) {
                this.props.onSaved(ids[0]);
            }
            this.props.onClose();
        } catch (error) {
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.submitting = false;
        }
    }
}
