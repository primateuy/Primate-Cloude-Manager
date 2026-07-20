/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

/**
 * Formulario OWL de "Agregar Odoo" (instance.create.wizard): monta OTRO Odoo en
 * un servidor EXISTENTE (nunca lanza EC2). Delega en el TransientModel, que
 * valida (base única, slug, layout) y asigna el slot de puertos atómicamente al
 * confirmar; errores accionables inline.
 *
 * Muestra lo que pidió el diseño R3: la advertencia ADVISORIA de RAM (~1 Odoo
 * por GB) y el preview de puertos con su nota "se asignará al confirmar".
 */
export class InstanceForm extends Component {
    static template = "primate_cloud_manager.InstanceForm";
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
            serverName: "",
            projects: [],
            odooVersions: [],
            odooEditions: [],
            httpPort: 0,
            geventPort: 0,
            ramWarning: "",
            // valores
            project_id: false,
            name: "",
            odoo_version: "19",
            odoo_edition: "community",
            domain: "",
            db_name: "",
            admin_password: "",
            workers: 0,
            create_dns: false,
            hosted_zone_id: "",
            ttl: 300,
        });
        onWillStart(() => this.load());
    }

    async load() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_instance_form_data",
            [this.props.environmentId]);
        this.state.loading = false;
        if (!d || (!d.environment_id && !d.blocked)) {
            this.state.blocked = "El servidor ya no existe.";
            return;
        }
        if (d.blocked) {
            this.state.blocked = d.blocked;
            return;
        }
        Object.assign(this.state, {
            serverName: d.server_name || "",
            projects: d.projects || [],
            odooVersions: d.odoo_versions || [],
            odooEditions: d.odoo_editions || [],
            httpPort: d.http_port_preview || 0,
            geventPort: d.gevent_port_preview || 0,
            ramWarning: d.ram_warning || "",
        });
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    onIntInput(field, ev) {
        this.state[field] = parseInt(ev.target.value, 10) || 0;
    }

    setSelect(field, value) {
        this.state[field] = value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setProject(value) {
        this.state.project_id = value ? parseInt(value, 10) : false;
        if (this.state.errors.project_id) {
            this.state.errors = { ...this.state.errors, project_id: "" };
        }
    }

    toggle(field, ev) {
        this.state[field] = ev.target.checked;
    }

    get projectOptions() {
        return [{ value: "", label: "(elegí el cliente)" }, ...this.state.projects];
    }

    // Validación de forma en cliente, accionable. La autoritativa (regex de base,
    // unicidad, layout) corre server-side en el wizard.
    _validateClient() {
        const errors = {};
        if (!this.state.project_id) {
            errors.project_id = "Elegí el proyecto (cliente) dueño de este Odoo.";
        }
        if (!(this.state.name || "").trim()) {
            errors.name = "Poné un nombre para la instancia.";
        }
        if (!(this.state.domain || "").trim()) {
            errors.domain = "Indicá el dominio (server_name de nginx).";
        }
        if (!(this.state.db_name || "").trim()) {
            errors.db_name = "Indicá el nombre de la base (única en el servidor).";
        }
        if (!(this.state.admin_password || "").trim()) {
            errors.admin_password = "Poné la contraseña admin del Odoo nuevo.";
        }
        if (this.state.create_dns && !(this.state.hosted_zone_id || "").trim()) {
            errors.hosted_zone_id = "Para crear DNS, indicá el Hosted Zone ID.";
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
                project_id: this.state.project_id,
                name: this.state.name,
                odoo_version: this.state.odoo_version,
                odoo_edition: this.state.odoo_edition,
                domain: this.state.domain,
                db_name: this.state.db_name,
                admin_password: this.state.admin_password,
                workers: this.state.workers || 0,
                create_dns: this.state.create_dns,
                hosted_zone_id: this.state.hosted_zone_id,
                ttl: this.state.ttl || 300,
            };
            const ids = await this.orm.create(
                "primate.cloud.instance.create.wizard", [vals]);
            await this.orm.call(
                "primate.cloud.instance.create.wizard", "action_add_instance", [ids]);
            this.env.pcm?.notify(
                "Instalación encolada. Los Odoo vivos del servidor no se tocan.",
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
