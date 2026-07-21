/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmField } from "../components/pcm_field";
import { PcmSelect } from "../components/pcm_select";
import { pcmErrorMessage } from "../pcm_actions";

/**
 * Formulario OWL de "Crear instancia EC2" (lanza una MÁQUINA nueva en AWS). Era
 * el último form nativo residual; se migra al kit para que se vea bien en los 3
 * temas y en dark (un form nativo no conoce los tokens del modo oscuro).
 *
 * Como Crear entorno: semáforo de región + Resolver AMI + campos de red gateados
 * por admin (el usuario final ve lo esencial; el admin, lo avanzado). Diferencia:
 * acá el semáforo es ADVISORY (job_create_ec2 no hace auto-discovery; la red
 * vacía cae en los defaults de AWS). Delega en el ec2.create.wizard existente.
 */
export class Ec2Form extends Component {
    static template = "primate_cloud_manager.Ec2Form";
    static components = { PcmField, PcmSelect };
    static props = {
        accountId: { type: [Number, Boolean], optional: true },
        envId: { type: [Number, Boolean], optional: true },
        onClose: { type: Function },
        onSaved: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            submitting: false,
            busyRegion: false,
            busyAmi: false,
            error: "",
            errors: {},
            isCloudAdmin: false,
            accounts: [],
            regions: [],
            osTypes: [],
            environmentName: "",
            environment_id: false,
            regionStatus: "draft",
            regionStatusLabel: "",
            regionDetail: "",
            // valores
            name: "",
            account_id: false,
            region: "",
            instance_type: "t3.medium",
            os_type: "ubuntu_24",
            image_id: "",
            disk_size_gb: 30,
            key_name: "",
            security_group_ids: "",
            subnet_id: "",
            instance_profile: "",
        });
        onWillStart(() => this.load());
    }

    async load() {
        const d = await this.orm.call(
            "primate.cloud.dashboard", "get_ec2_form_data",
            [this.props.accountId || false, this.props.envId || false]);
        this.state.loading = false;
        Object.assign(this.state, {
            isCloudAdmin: d.is_cloud_admin,
            accounts: d.accounts || [],
            regions: d.regions || [],
            osTypes: d.os_types || [],
            environmentName: d.environment_name || "",
            environment_id: d.environment_id || false,
            account_id: d.account_id || false,
            region: d.region || "",
            regionStatus: d.region_status || "draft",
            regionStatusLabel: d.region_status_label || "",
        });
    }

    get accountOptions() {
        return [{ value: "", label: "(elegí la cuenta AWS)" }, ...this.state.accounts];
    }

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

    setSelect(field, value) {
        this.state[field] = value;
        if (this.state.errors[field]) {
            this.state.errors = { ...this.state.errors, [field]: "" };
        }
    }

    setAccount(value) {
        this.state.account_id = value ? parseInt(value, 10) : false;
        if (this.state.errors.account_id) {
            this.state.errors = { ...this.state.errors, account_id: "" };
        }
        // Al cambiar de cuenta, el semáforo cacheado ya no aplica.
        this.state.regionStatus = "draft";
        this.state.regionStatusLabel = "";
        this.state.regionDetail = "";
    }

    async verifyRegion() {
        if (this.state.busyRegion || !this.state.account_id || !this.state.region) {
            this.state.regionDetail = "Elegí cuenta y región antes de verificar.";
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

    _validateClient() {
        const errors = {};
        if (!(this.state.name || "").trim()) {
            errors.name = "Poné un nombre (tag Name de la instancia).";
        }
        if (!this.state.account_id) {
            errors.account_id = "Elegí la cuenta AWS donde lanzar la máquina.";
        }
        if (!this.state.region) {
            errors.region = "Elegí la región.";
        }
        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    async submit() {
        if (this.state.submitting) {
            return;
        }
        this.state.error = "";
        if (!this._validateClient()) {
            return;
        }
        // Confirmación: crea un recurso facturable en AWS.
        const ok = await this.env.pcm.confirm(
            "Esto lanzará una máquina EC2 facturable en AWS. ¿Continuar?");
        if (!ok) {
            return;
        }
        this.state.submitting = true;
        try {
            const vals = {
                name: this.state.name,
                account_id: this.state.account_id,
                environment_id: this.state.environment_id || false,
                region: this.state.region,
                instance_type: this.state.instance_type,
                os_type: this.state.os_type,
                image_id: this.state.image_id,
                disk_size_gb: this.state.disk_size_gb || 30,
                key_name: this.state.key_name,
                security_group_ids: this.state.security_group_ids,
                subnet_id: this.state.subnet_id,
                instance_profile: this.state.instance_profile,
            };
            const ids = await this.orm.create(
                "primate.cloud.ec2.create.wizard", [vals]);
            await this.orm.call(
                "primate.cloud.ec2.create.wizard", "action_create_instance", [ids]);
            this.env.pcm?.notify(
                "Creación de instancia EC2 encolada. Se registrará al terminar.",
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
