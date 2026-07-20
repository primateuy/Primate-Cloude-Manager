/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

// Campos que se mandan al provision.wizard al aprovisionar (el mismo set que
// persiste el wizard). El form OWL sólo los recolecta; validar/encolar es del
// wizard (cero lógica de negocio nueva).
const WIZARD_FIELDS = [
    "account_id", "region", "domain", "admin_password", "instance_name",
    "instance_type", "os_type", "image_id", "disk_size_gb", "key_name",
    "security_group_ids", "subnet_id", "instance_profile", "db_mode", "db_name",
    "db_user", "db_password", "pg_version", "rds_identifier", "rds_instance_class",
    "rds_storage_gb", "rds_multi_az", "backup_retention_days", "create_dns",
    "hosted_zone_id", "ttl",
];

/**
 * Formulario OWL de "Crear entorno" (aprovisionar): cómputo EC2 + base de datos
 * + DNS, en secciones apiladas. El form más grande del rediseño — valida el kit.
 *
 * Delega en el provision.wizard existente: precarga con get_provision_defaults,
 * verifica región / resuelve AMI con métodos existentes, y aprovisiona con
 * action_provision (que valida, chequea el semáforo y encola). Errores
 * accionables inline, sin traceback ni diálogo "Oops".
 */
export class EnvForm extends Component {
    static template = "primate_cloud_manager.EnvForm";
    static components = { PcmField, PcmSelect };
    static props = {
        envId: { type: Number },
        onClose: { type: Function },
        onSaved: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            blocked: "",
            submitting: false,
            busyRegion: false,
            busyAmi: false,
            error: "",
            errors: {},
            isCloudAdmin: false,
            accountName: "",
            environmentName: "",
            regionStatus: "draft",
            regionStatusLabel: "",
            regionDetail: "",
            // opciones de selección
            regions: [],
            osTypes: [],
            dbModes: [],
            pgVersions: [],
            // valores del form (se completan en loadDefaults)
            account_id: false,
            region: "",
            domain: "",
            admin_password: "",
            instance_name: "",
            instance_type: "t3.medium",
            os_type: "ubuntu_24",
            image_id: "",
            disk_size_gb: 30,
            key_name: "",
            security_group_ids: "",
            subnet_id: "",
            instance_profile: "",
            db_mode: "local",
            db_name: "",
            db_user: "odoo",
            db_password: "",
            pg_version: "",
            rds_identifier: "",
            rds_instance_class: "db.t3.medium",
            rds_storage_gb: 20,
            rds_multi_az: false,
            backup_retention_days: 7,
            create_dns: false,
            hosted_zone_id: "",
            ttl: 300,
        });
        onWillStart(() => this.loadDefaults());
    }

    async loadDefaults() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_provision_defaults", [this.props.envId]);
        this.state.loading = false;
        if (!d || !d.environment_id) {
            this.state.blocked = "El entorno ya no existe.";
            return;
        }
        // Volcar todos los valores del wizard + metadatos.
        for (const f of WIZARD_FIELDS) {
            if (d[f] !== undefined) {
                this.state[f] = d[f] === false && typeof this.state[f] === "string"
                    ? "" : d[f];
            }
        }
        Object.assign(this.state, {
            isCloudAdmin: d.is_cloud_admin,
            accountName: d.account_name || "",
            environmentName: d.environment_name || "",
            regionStatus: d.region_status || "draft",
            regionStatusLabel: d.region_status_label || "",
            regionDetail: d.region_detail || "",
            regions: d.regions || [],
            osTypes: d.os_types || [],
            dbModes: d.db_modes || [],
            pgVersions: d.pg_versions || [],
        });
    }

    get isRds() { return this.state.db_mode === "rds"; }
    get dbNone() { return this.state.db_mode === "none"; }

    get semaphoreClass() {
        if (this.state.regionStatus === "ok") return "o_pcm_semaphore_ok";
        if (this.state.regionStatus === "draft") return "";
        return "o_pcm_semaphore_bad";
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

    onSelect(field, value) {
        this.state[field] = value;
    }

    toggle(field, ev) {
        this.state[field] = ev.target.checked;
    }

    async verifyRegion() {
        if (this.state.busyRegion || !this.state.account_id || !this.state.region) {
            if (!this.state.account_id || !this.state.region) {
                this.state.regionDetail = "Elegí una región antes de verificar.";
            }
            return;
        }
        this.state.busyRegion = true;
        try {
            const r = await this.orm.call(
                "primate.cloud.dashboard", "verify_region",
                [this.state.account_id, this.state.region]);
            this.state.regionStatus = r.status || "draft";
            this.state.regionStatusLabel = r.status_label || "";
            this.state.regionDetail = r.detail || "";
        } catch (error) {
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.busyRegion = false;
        }
    }

    async resolveAmi() {
        if (this.state.busyAmi || !this.state.account_id || !this.state.region) {
            return;
        }
        this.state.busyAmi = true;
        try {
            const ami = await this.orm.call(
                "primate.cloud.account", "resolve_ubuntu_ami",
                [[this.state.account_id], this.state.region]);
            this.state.image_id = ami || "";
        } catch (error) {
            this.state.error = pcmErrorMessage(error);
        } finally {
            this.state.busyAmi = false;
        }
    }

    // Validación de forma mínima en cliente (feedback inmediato, accionable). La
    // validación autoritativa (coherencia RDS/DNS + semáforo) la hace el wizard.
    _validateClient() {
        const errors = {};
        if (!(this.state.domain || "").trim()) {
            errors.domain = "Poné el dominio del entorno (ej.: forum.primate.cloud).";
        }
        if (this.isRds) {
            if (!(this.state.rds_identifier || "").trim()) {
                errors.rds_identifier = "RDS: indicá el identificador de la base.";
            }
            if (!(this.state.db_password || "").trim()) {
                errors.db_password = "RDS: indicá la contraseña maestra de la base.";
            }
        }
        if (this.state.create_dns && !(this.state.hosted_zone_id || "").trim()) {
            errors.hosted_zone_id =
                "Para crear DNS, indicá el Hosted Zone ID de Route 53.";
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
        // Confirmación explícita: esto crea recursos facturables en AWS.
        const ok = await this.env.pcm.confirm(
            "Esto creará recursos facturables en AWS (EC2, base de datos y DNS). " +
            "¿Continuar?");
        if (!ok) {
            return;
        }
        this.state.submitting = true;
        try {
            const vals = { environment_id: this.props.envId };
            for (const f of WIZARD_FIELDS) {
                vals[f] = this.state[f];
            }
            const ids = await this.orm.create(
                "primate.cloud.provision.wizard", [vals]);
            await this.orm.call(
                "primate.cloud.provision.wizard", "action_provision", [ids]);
            this.env.pcm?.notify(
                "Aprovisionamiento encolado. El entorno pasará a Activo al terminar.",
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
