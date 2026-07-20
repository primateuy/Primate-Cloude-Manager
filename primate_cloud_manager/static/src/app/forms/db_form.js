/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

/**
 * Formulario OWL para crear/editar una base de datos (reemplaza el form nativo,
 * incluido el usado para "asociar a una instancia"). Escribe el modelo por ORM.
 *
 * Condicional por MODALIDAD (como en Crear entorno): PostgreSQL local muestra el
 * select de instancia (en qué servidor vive); Amazon RDS muestra el bloque RDS
 * como informativo (esos campos los sincroniza AWS, no se editan acá). El
 * constraint de coherencia entorno↔instancia se valida server-side y su error
 * se muestra accionable, sin traceback.
 */
export class DbForm extends Component {
    static template = "primate_cloud_manager.DbForm";
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
        this.state = useState({
            loading: true,
            blocked: "",
            submitting: false,
            error: "",
            errors: {},
            servers: [],
            dbTypes: [],
            pgVersions: [],
            environmentName: "",
            accountName: "",
            environment_id: false,
            account_id: false,
            // valores del form
            name: "",
            db_type: "rds",
            pg_version: "",
            ec2_instance_id: false,
            // informativos (RDS / estado), read-only
            stateLabel: "",
            rds_identifier: "",
            rds_endpoint: "",
            rds_instance_class: "",
            rds_storage_gb: 0,
            rds_multi_az: false,
            backup_retention_days: 0,
        });
        onWillStart(() => this.load());
    }

    get isEdit() {
        return this.props.mode === "edit" && !!this.props.recordId;
    }

    get isLocal() {
        return this.state.db_type === "local_pg";
    }

    get isRds() {
        return this.state.db_type === "rds";
    }

    get instanceOptions() {
        return [{ value: "", label: "(sin asignar)" }, ...this.state.servers];
    }

    get pgOptions() {
        return [{ value: "", label: "(sin definir)" }, ...this.state.pgVersions];
    }

    async load() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_database_form_data",
            [this.props.environmentId || false, this.props.recordId || false]);
        this.state.loading = false;
        if (this.isEdit && !d.id) {
            this.state.blocked = "La base de datos ya no existe.";
            return;
        }
        Object.assign(this.state, {
            servers: d.servers || [],
            dbTypes: d.db_types || [],
            pgVersions: d.pg_versions || [],
            environmentName: d.environment_name || "",
            accountName: d.account_name || "",
            environment_id: d.environment_id || false,
            account_id: d.account_id || false,
            name: d.name || "",
            db_type: d.db_type || "rds",
            pg_version: d.pg_version || "",
            ec2_instance_id: d.ec2_instance_id || false,
            stateLabel: d.state_label || "",
            rds_identifier: d.rds_identifier || "",
            rds_endpoint: d.rds_endpoint || "",
            rds_instance_class: d.rds_instance_class || "",
            rds_storage_gb: d.rds_storage_gb || 0,
            rds_multi_az: d.rds_multi_az || false,
            backup_retention_days: d.backup_retention_days || 0,
        });
    }

    onInput(field, ev) {
        this.state[field] = ev.target.value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setType(value) {
        this.state.db_type = value;
    }

    setInstance(value) {
        this.state.ec2_instance_id = value ? parseInt(value, 10) : false;
    }

    setPgVersion(value) {
        this.state.pg_version = value || false;
    }

    _validateClient() {
        const errors = {};
        if (!(this.state.name || "").trim()) {
            errors.name = "Poné un nombre para la base de datos.";
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
                db_type: this.state.db_type,
                pg_version: this.state.pg_version || false,
                // La instancia solo aplica a local; en RDS se limpia.
                ec2_instance_id: this.isLocal
                    ? (this.state.ec2_instance_id || false) : false,
            };
            if (this.isEdit) {
                await this.orm.write(
                    "primate.cloud.database", [this.props.recordId], vals);
            } else {
                vals.environment_id = this.state.environment_id || false;
                vals.account_id = this.state.account_id || false;
                await this.orm.create("primate.cloud.database", [vals]);
            }
            this.env.pcm?.notify(
                this.isEdit ? "Base de datos actualizada." : "Base de datos creada.",
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
