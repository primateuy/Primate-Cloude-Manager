# -*- coding: utf-8 -*-
"""Tests de Fase 4: creación de EC2/RDS/DNS, wizards y flujo de aprovisionamiento.

Toda llamada AWS está mockeada (unittest.mock); los tests no tocan infra real.
El flujo de aprovisionamiento se ejercita end-to-end contra un cliente boto3
falso configurado para responder a EC2, RDS, SSM y Route 53.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..services import aws_ec2, aws_rds, aws_route53
from ..tools import bus


def _base_with_client():
    """AwsBaseService falso que devuelve siempre el mismo client mock."""
    base = mock.Mock()
    return base, base.get_client.return_value


def _running_instance(instance_id="i-new", name="forum-prod", public_ip="1.2.3.4"):
    """Respuesta cruda de describe_instances para una instancia 'running'."""
    return {
        "Reservations": [{"Instances": [{
            "InstanceId": instance_id,
            "State": {"Name": "running"},
            "InstanceType": "t3.medium",
            "PublicIpAddress": public_ip,
            "PrivateIpAddress": "10.0.0.5",
            "Tags": [{"Key": "Name", "Value": name}],
            "LaunchTime": None,
        }]}]
    }


def _available_db(identifier="forum-db", endpoint="forum-db.rds.amazonaws.com"):
    """Respuesta cruda de describe_db_instances para una RDS 'available'."""
    return {"DBInstances": [{
        "DBInstanceIdentifier": identifier,
        "DBInstanceClass": "db.t3.medium",
        "Engine": "postgres",
        "EngineVersion": "16.3",
        "AllocatedStorage": 20,
        "MultiAZ": False,
        "BackupRetentionPeriod": 7,
        "DBInstanceStatus": "available",
        "Endpoint": {"Address": endpoint},
    }]}


# ----------------------------------------------------------------------------
# Servicios: creación (mock de boto3)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestServiceCreate(TransactionCase):
    def test_ec2_create_instance_espera_running(self):
        base, client = _base_with_client()
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}
        client.describe_instances.return_value = _running_instance("i-new")
        service = aws_ec2.AwsEc2Service(base)
        data = service.create_instance(
            image_id="ami-1", instance_type="t3.medium",
            tags=[{"Key": "Name", "Value": "forum-prod"}],
            region="us-east-1", disk_size_gb=30, _sleep=lambda s: None,
        )
        self.assertEqual(data["aws_instance_id"], "i-new")
        self.assertEqual(data["instance_state"], "running")
        # Etiquetas en instancia y volumen.
        _, kwargs = client.run_instances.call_args
        types = {spec["ResourceType"] for spec in kwargs["TagSpecifications"]}
        self.assertEqual(types, {"instance", "volume"})
        self.assertEqual(kwargs["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 30)

    def test_ec2_create_instance_client_token(self):
        # ClientToken hace idempotente a RunInstances (no duplica en requeue).
        base, client = _base_with_client()
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}
        client.describe_instances.return_value = _running_instance("i-new")
        aws_ec2.AwsEc2Service(base).create_instance(
            image_id="ami-1", instance_type="t3.medium", tags=[],
            client_token="pcm-abc", _sleep=lambda s: None,
        )
        _, kwargs = client.run_instances.call_args
        self.assertEqual(kwargs["ClientToken"], "pcm-abc")

    def test_create_instance_normaliza_image_id(self):
        # Aunque llegue con corchetes, RunInstances recibe el id pelado.
        base, client = _base_with_client()
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-x"}]}
        client.describe_instances.return_value = _running_instance("i-x")
        aws_ec2.AwsEc2Service(base).create_instance(
            image_id="[ami-dirty]", instance_type="t3.small", tags=[],
            _sleep=lambda s: None,
        )
        _, kwargs = client.run_instances.call_args
        self.assertEqual(kwargs["ImageId"], "ami-dirty")

    def test_ec2_create_instance_estado_terminal_falla(self):
        base, client = _base_with_client()
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-bad"}]}
        client.describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-bad", "State": {"Name": "terminated"},
            }]}]
        }
        with self.assertRaises(RuntimeError):
            aws_ec2.AwsEc2Service(base).create_instance(
                image_id="ami-1", instance_type="t3.medium", tags=[],
                _sleep=lambda s: None,
            )

    def test_rds_create_instance_espera_available(self):
        base, client = _base_with_client()
        client.describe_db_instances.return_value = _available_db("forum-db")
        service = aws_rds.AwsRdsService(base)
        data = service.create_instance(
            identifier="forum-db", instance_class="db.t3.medium", storage_gb=20,
            master_username="odoo", master_password="secret", tags=[],
            engine_version="16.3", _sleep=lambda s: None,
        )
        self.assertEqual(data["rds_identifier"], "forum-db")
        self.assertEqual(data["status"], "available")
        _, kwargs = client.create_db_instance.call_args
        self.assertEqual(kwargs["Engine"], "postgres")
        self.assertEqual(kwargs["MasterUserPassword"], "secret")

    def test_route53_create_record_upsert(self):
        base, client = _base_with_client()
        client.change_resource_record_sets.return_value = {"ChangeInfo": {"Id": "/change/C1"}}
        change_id = aws_route53.AwsRoute53Service(base).create_record(
            "Z1", "forum.primate.cloud", "A", "1.2.3.4", ttl=300,
        )
        self.assertEqual(change_id, "/change/C1")
        _, kwargs = client.change_resource_record_sets.call_args
        change = kwargs["ChangeBatch"]["Changes"][0]
        self.assertEqual(change["Action"], "UPSERT")
        self.assertEqual(change["ResourceRecordSet"]["Name"], "forum.primate.cloud")


# ----------------------------------------------------------------------------
# Resolución automática de AMI
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestAmiResolution(TransactionCase):
    def test_normalize_ami_id(self):
        n = aws_ec2.normalize_ami_id
        self.assertEqual(n("ami-123"), "ami-123")
        self.assertEqual(n("[ami-123]"), "ami-123")
        self.assertEqual(n("  '[ami-123]'  "), "ami-123")
        self.assertEqual(n('"ami-123"'), "ami-123")
        self.assertEqual(n(""), "")
        self.assertIsNone(n(None))

    def test_resolve_ubuntu_ami_via_ssm(self):
        base, client = _base_with_client()
        client.get_parameter.return_value = {"Parameter": {"Value": "ami-ssm"}}
        ami = aws_ec2.AwsEc2Service(base).resolve_ubuntu_ami(region="us-east-2")
        self.assertEqual(ami, "ami-ssm")

    def test_resolve_ubuntu_ami_fallback_describe(self):
        # SSM sin permiso -> cae a DescribeImages y toma el más reciente.
        base, client = _base_with_client()
        client.get_parameter.side_effect = Exception("AccessDenied")
        client.describe_images.return_value = {"Images": [
            {"ImageId": "ami-old", "CreationDate": "2025-01-01"},
            {"ImageId": "ami-new", "CreationDate": "2026-01-01"},
        ]}
        ami = aws_ec2.AwsEc2Service(base).resolve_ubuntu_ami(region="us-east-2")
        self.assertEqual(ami, "ami-new")

    def test_resolve_ubuntu_ami_sin_resultados(self):
        base, client = _base_with_client()
        client.get_parameter.side_effect = Exception("AccessDenied")
        client.describe_images.return_value = {"Images": []}
        self.assertIsNone(aws_ec2.AwsEc2Service(base).resolve_ubuntu_ami())

    def test_account_resolve_ubuntu_ami_userror_si_no_hay(self):
        account = self.env["primate.cloud.account"].create(
            {"name": "C", "default_region": "us-east-1"}
        )
        fake = mock.Mock()
        fake.resolve_ubuntu_ami.return_value = None
        with mock.patch.object(aws_ec2, "AwsEc2Service", return_value=fake), \
             mock.patch.object(type(account), "_get_aws_service", return_value=mock.Mock()):
            with self.assertRaises(UserError):
                account.resolve_ubuntu_ami()


# ----------------------------------------------------------------------------
# Wizard ec2_create + account.job_create_ec2
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestEc2CreateWizard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })

    def test_wizard_encola_job(self):
        wiz = self.env["primate.cloud.ec2.create.wizard"].create({
            "name": "srv", "account_id": self.account.id, "region": "us-east-1",
            "instance_type": "t3.medium", "image_id": "ami-1", "disk_size_gb": 30,
        })
        with mock.patch.object(type(self.account), "with_delay") as wd:
            wiz.action_create_instance()
            wd.assert_called_once()

    def test_wizard_ami_vacio_se_encola(self):
        # AMI opcional: vacío ya NO falla; se resuelve al crear la EC2 (en el job).
        wiz = self.env["primate.cloud.ec2.create.wizard"].create({
            "name": "srv", "account_id": self.account.id, "region": "us-east-1",
            "instance_type": "t3.medium", "image_id": "",
        })
        with mock.patch.object(type(self.account), "with_delay") as wd:
            wiz.action_create_instance()
            wd.assert_called_once()

    def test_wizard_resolver_ami_autocompleta(self):
        wiz = self.env["primate.cloud.ec2.create.wizard"].create({
            "name": "srv", "account_id": self.account.id, "region": "us-east-1",
            "instance_type": "t3.medium",
        })
        with mock.patch.object(type(self.account), "resolve_ubuntu_ami",
                               return_value="ami-resolved"):
            wiz.action_resolve_ami()
        self.assertEqual(wiz.image_id, "ami-resolved")

    def test_job_create_ec2_registra_instancia(self):
        base, client = _base_with_client()
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}
        client.describe_instances.return_value = _running_instance("i-new", "srv")
        vals = {
            "name": "srv", "environment_id": False, "region": "us-east-1",
            "instance_type": "t3.medium", "os_type": "ubuntu_24", "image_id": "ami-1",
            "disk_size_gb": 30, "security_group_ids": [],
        }
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            instance = self.account.job_create_ec2(vals)
        self.assertTrue(instance)
        self.assertEqual(instance.aws_instance_id, "i-new")
        self.assertEqual(instance.os_type, "ubuntu_24")
        self.assertEqual(instance.disk_size_gb, 30)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", instance.id), ("action_type", "=", "ec2_create"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_create_ec2_falla_loguea(self):
        base, client = _base_with_client()
        client.run_instances.side_effect = Exception("AuthFailure")
        vals = {"name": "srv", "region": "us-east-1", "instance_type": "t3.medium",
                "image_id": "ami-1", "security_group_ids": []}
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            res = self.account.job_create_ec2(vals)
        self.assertFalse(res)
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_model", "=", "primate.cloud.account"),
             ("action_type", "=", "ec2_create"), ("result", "=", "failed")]
        )
        self.assertTrue(log)
        self.assertIn("AuthFailure", log.error_message)


# ----------------------------------------------------------------------------
# Aprovisionamiento del entorno (flujo 7.1)
# ----------------------------------------------------------------------------
def _provision_base():
    """Base falsa cuyo único client responde a EC2, RDS, SSM y Route 53."""
    base = mock.Mock()
    client = base.get_client.return_value
    client.run_instances.return_value = {"Instances": [{"InstanceId": "i-new"}]}
    client.describe_instances.return_value = _running_instance("i-new")
    client.create_db_instance.return_value = {}
    client.describe_db_instances.return_value = _available_db("forum-db")
    client.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    # La cadena R2 sobre layout multi-Odoo (R3-B2) exige los marcadores de
    # cada script además del Status (un run truncado no puede pasar por OK).
    client.get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": "PCM_BOOTSTRAP_OK\nPCM_INSTALL_OK",
        "ResponseCode": 0,
    }
    client.change_resource_record_sets.return_value = {"ChangeInfo": {"Id": "/change/C1"}}
    return base, client


@tagged("post_install", "-at_install", "primate_cloud")
class TestEnvironmentProvision(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test Phase4"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id, "partner_id": self.partner.id}
        )
        self.env_rec = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19", "odoo_edition": "community",
            "main_url": "forum.primate.cloud",
        })

    def _params(self, **overrides):
        params = {
            "region": "us-east-1", "domain": "forum.primate.cloud",
            "instance_name": "forum-prod", "instance_type": "t3.medium",
            "os_type": "ubuntu_24", "image_id": "ami-1", "disk_size_gb": 30,
            # Red explícita (override del admin) → el provision salta el
            # auto-discovery; estos tests ejercitan la mecánica de provisión.
            "security_group_ids": ["sg-1"], "subnet_id": "subnet-1",
            "instance_profile": "pcm-ssm-role",
            "db_mode": "rds", "db_name": "forum", "db_user": "odoo",
            "db_password": "secret", "pg_version": "16",
            "rds_identifier": "forum-db", "rds_instance_class": "db.t3.medium",
            "rds_storage_gb": 20, "rds_multi_az": False, "backup_retention_days": 7,
            "create_dns": True, "hosted_zone_id": "Z1", "ttl": 300,
            "admin_password": "adminpw",
        }
        params.update(overrides)
        return params

    def _run_provision_chain(self, params):
        """Ejecuta la cadena R2 como producción: server → install encadenado.

        job_provision_server encola job_install_instance vía with_delay; acá
        se captura ese encolado y se ejecuta con los MISMOS params (que es la
        garantía de la cadena: misma db_password transitoria, mismo token).
        """
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.env_rec), "with_delay",
                               return_value=fake_delay):
            ok_server = self.env_rec.job_provision_server(params)
        if not ok_server:
            return False
        chained_params = fake_delay.job_install_instance.call_args[0][0]
        return self.env_rec.job_install_instance(chained_params)

    def test_enqueue_provision_inyecta_client_token(self):
        # El token viaja en los args del job (queue_job los persiste): un requeue
        # tras reiniciar el server reusa el mismo token y NO duplica la EC2.
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.env_rec), "with_delay", return_value=fake_delay):
            self.env_rec._enqueue_provision({"region": "us-east-1"})
        params = fake_delay.job_provision_server.call_args[0][0]
        self.assertIn("client_token", params)
        self.assertTrue(params["client_token"].startswith("pcm-"))

    def test_enqueue_provision_genera_db_password_local_pg(self):
        # Cero-config: local_pg sin contraseña → se genera una transitoria al
        # encolar (viaja en los args del job; sin ella el usuario PG queda sin
        # password y Odoo entra en loop de fe_sendauth — visto en el E2E de B5).
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.env_rec), "with_delay", return_value=fake_delay):
            self.env_rec._enqueue_provision(
                {"region": "us-east-1", "db_mode": "local_pg", "db_password": ""})
        params = fake_delay.job_provision_server.call_args[0][0]
        self.assertTrue(params["db_password"])
        self.assertGreaterEqual(len(params["db_password"]), 20)

    def test_enqueue_provision_respeta_db_password_explicita(self):
        # Una contraseña dada por el admin NUNCA se pisa; y fuera de local_pg
        # (rds/none) no se inventa ninguna.
        fake_delay = mock.Mock()
        with mock.patch.object(type(self.env_rec), "with_delay", return_value=fake_delay):
            self.env_rec._enqueue_provision(
                {"region": "us-east-1", "db_mode": "local_pg", "db_password": "explicita"})
        self.assertEqual(
            fake_delay.job_provision_server.call_args[0][0]["db_password"], "explicita")
        with mock.patch.object(type(self.env_rec), "with_delay", return_value=fake_delay):
            self.env_rec.state = "draft"
            self.env_rec._enqueue_provision({"region": "us-east-1", "db_mode": "none"})
        self.assertFalse(
            fake_delay.job_provision_server.call_args[0][0].get("db_password"))

    def test_action_provision_abre_wizard(self):
        action = self.env_rec.action_provision()
        self.assertEqual(action["res_model"], "primate.cloud.provision.wizard")
        self.assertEqual(action["context"]["default_environment_id"], self.env_rec.id)

    def test_action_provision_estado_invalido_falla(self):
        self.env_rec.state = "active"
        with self.assertRaises(UserError):
            self.env_rec.action_provision()

    def test_provision_wizard_encola_y_marca_provisioning(self):
        wiz = self.env["primate.cloud.provision.wizard"].create({
            "environment_id": self.env_rec.id, "region": "us-east-1",
            "domain": "forum.primate.cloud", "image_id": "ami-1",
            "instance_type": "t3.medium", "db_mode": "local_pg",
            # Red explícita → salta el chequeo de región (override del admin).
            "security_group_ids": "sg-1", "subnet_id": "subnet-1",
            "create_dns": False,
        })
        with mock.patch.object(type(self.env_rec), "with_delay") as wd:
            wiz.action_provision()
            wd.assert_called_once()
        self.assertEqual(self.env_rec.state, "provisioning")

    def test_provision_wizard_recuerda_config(self):
        # Lo ingresado se guarda y un wizard nuevo se precarga (aunque haya fallado).
        Wiz = self.env["primate.cloud.provision.wizard"]
        wiz = Wiz.create({
            "environment_id": self.env_rec.id, "region": "us-east-1", "domain": "x",
            "image_id": "ami-zzz", "instance_type": "t3.small",
            "security_group_ids": "sg-1,sg-2", "subnet_id": "subnet-1",
            "instance_profile": "pcm-ssm-role",
            "db_mode": "local_pg", "db_password": "secret", "create_dns": False,
        })
        with mock.patch.object(type(self.env_rec), "with_delay"):
            wiz.action_provision()
        self.assertTrue(self.env_rec.provision_config_encrypted)
        cfg = self.env_rec._load_provision_config()
        self.assertEqual(cfg["image_id"], "ami-zzz")
        self.assertEqual(cfg["security_group_ids"], "sg-1,sg-2")
        self.assertEqual(cfg["instance_profile"], "pcm-ssm-role")
        # Un wizard nuevo (mismo entorno) viene precargado.
        defaults = Wiz.with_context(
            default_environment_id=self.env_rec.id
        ).default_get(list(Wiz._fields))
        self.assertEqual(defaults["image_id"], "ami-zzz")
        self.assertEqual(defaults["instance_profile"], "pcm-ssm-role")
        self.assertEqual(defaults["security_group_ids"], "sg-1,sg-2")

    def test_provision_wizard_rds_sin_password_falla(self):
        wiz = self.env["primate.cloud.provision.wizard"].create({
            "environment_id": self.env_rec.id, "region": "us-east-1",
            "domain": "forum.primate.cloud", "image_id": "ami-1",
            "instance_type": "t3.medium", "db_mode": "rds",
            "rds_identifier": "forum-db", "create_dns": False,
        })
        with self.assertRaises(UserError):
            wiz.action_provision()

    def test_job_provision_rds_end_to_end(self):
        base, client = _provision_base()
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self._run_provision_chain(self._params())
        self.assertTrue(ok)
        self.assertEqual(self.env_rec.state, "active")
        # Se crearon y asociaron los recursos.
        self.assertEqual(len(self.env_rec.ec2_instance_ids), 1)
        self.assertEqual(self.env_rec.ec2_instance_ids.aws_instance_id, "i-new")
        self.assertEqual(len(self.env_rec.database_ids), 1)
        self.assertEqual(self.env_rec.database_ids.db_type, "rds")
        self.assertEqual(self.env_rec.database_ids.rds_endpoint, "forum-db.rds.amazonaws.com")
        self.assertEqual(len(self.env_rec.dns_record_ids), 1)
        self.assertEqual(self.env_rec.dns_record_ids.record_value, "1.2.3.4")
        # Bitácora: provision exitoso.
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.env_rec.id), ("action_type", "=", "provision"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_provision_ami_vacio_resuelve(self):
        # Sin AMI: el flujo lo resuelve para la región antes de RunInstances.
        base, client = _provision_base()
        params = self._params(image_id="")
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base), \
             mock.patch.object(type(self.account), "resolve_ubuntu_ami",
                               return_value="ami-auto") as resolver:
            ok = self._run_provision_chain(params)
        self.assertTrue(ok)
        resolver.assert_called()
        _, kwargs = client.run_instances.call_args
        self.assertEqual(kwargs["ImageId"], "ami-auto")

    def test_job_provision_local_pg_sin_dns(self):
        base, client = _provision_base()
        params = self._params(db_mode="local_pg", create_dns=False)
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self._run_provision_chain(params)
        self.assertTrue(ok)
        self.assertEqual(self.env_rec.database_ids.db_type, "local_pg")
        self.assertEqual(self.env_rec.database_ids.ec2_instance_id,
                         self.env_rec.ec2_instance_ids)
        self.assertFalse(self.env_rec.dns_record_ids)
        # RDS no se tocó.
        client.create_db_instance.assert_not_called()

    def test_job_provision_install_falla_deja_error(self):
        base, client = _provision_base()
        client.get_command_invocation.return_value = {
            "Status": "Failed", "StandardErrorContent": "boom", "ResponseCode": 1,
        }
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self._run_provision_chain(self._params())
        self.assertFalse(ok)
        self.assertEqual(self.env_rec.state, "error")
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", self.env_rec.id), ("action_type", "=", "provision"),
             ("result", "=", "failed")]
        )
        self.assertTrue(log)

    def test_job_provision_emite_eventos_bus(self):
        # El front recibe overlay (start) + pasos en vivo + cierre (done).
        base, _client = _provision_base()
        sent = []
        with mock.patch.object(bus, "_send", side_effect=lambda env, p: sent.append(p)), \
             mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            self._run_provision_chain(self._params())
        kinds = [p["kind"] for p in sent]
        self.assertEqual(kinds[0], "provision_start")
        self.assertIn("provision_step", kinds)
        self.assertEqual(kinds[-1], "provision_done")
        self.assertTrue(sent[-1]["ok"])

    def test_job_provision_falla_emite_done_error(self):
        base, client = _provision_base()
        client.get_command_invocation.return_value = {"Status": "Failed", "StandardErrorContent": "x"}
        sent = []
        with mock.patch.object(bus, "_send", side_effect=lambda env, p: sent.append(p)), \
             mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            self._run_provision_chain(self._params())
        done = [p for p in sent if p["kind"] == "provision_done"]
        self.assertTrue(done)
        self.assertFalse(done[-1]["ok"])

    # --- R2: cadena servidor → instancia y recuperación -----------------
    def test_servidor_ok_encadena_install_con_mismos_params(self):
        base, _client = _provision_base()
        fake_delay = mock.Mock()
        # client_token/db_password los inyecta _enqueue_provision; acá se
        # simulan para verificar que la CADENA los reenvía intactos.
        params = self._params(client_token="pcm-test-token")
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base), \
             mock.patch.object(type(self.env_rec), "with_delay", return_value=fake_delay):
            ok = self.env_rec.job_provision_server(params)
        self.assertTrue(ok)
        chained = fake_delay.job_install_instance.call_args[0][0]
        # MISMOS params: la contraseña transitoria y el token viajan intactos.
        self.assertEqual(chained["client_token"], params["client_token"])
        self.assertEqual(chained["db_password"], params["db_password"])
        # El servidor quedó con su máquina 1:1 y el entorno sigue en curso.
        self.assertTrue(self.env_rec.ec2_instance_id)

    def test_install_falla_servidor_queda_recuperable(self):
        # El estado "servidor sí, instancia no" NO es un limbo: entorno en
        # error, máquina viva, instancia primaria en error.
        base, client = _provision_base()
        client.get_command_invocation.return_value = {
            "Status": "Failed", "StandardErrorContent": "boom", "ResponseCode": 1,
        }
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self._run_provision_chain(self._params())
        self.assertFalse(ok)
        self.assertEqual(self.env_rec.state, "error")
        self.assertTrue(self.env_rec.ec2_instance_id)
        self.assertNotEqual(self.env_rec.ec2_instance_id.instance_state, "terminated")
        self.assertEqual(self.env_rec.primary_instance_id.state, "error")

    def test_retry_install_reencola_el_job_fallido(self):
        # El retry REUSA el job fallido (mismos args persistidos por queue_job)
        # y no crea otra EC2: solo re-encola la 2ª mitad de la cadena.
        params = self._params()
        job = self.env_rec.with_delay().job_install_instance(params)
        job.db_record().write({"state": "failed"})
        self.env_rec.state = "error"
        self.env_rec.action_retry_install()
        self.assertEqual(job.db_record().state, "pending")
        self.assertEqual(self.env_rec.state, "provisioning")

    def test_retry_install_sin_job_fallido_guia_al_wizard(self):
        self.env_rec.state = "error"
        with self.assertRaises(UserError):
            self.env_rec.action_retry_install()

    def test_gate_crear_instancia_exige_servidor_activo(self):
        # R3-B3 reemplazó el gate "todavía no" de R2 por el wizard real; lo
        # que queda acá es la guarda de estado (el gate por layout legacy y
        # el wizard se fijan en test_r3_multiodoo.TestR3B3Wizard).
        with self.assertRaises(UserError):
            self.env_rec.action_create_instance()  # entorno draft

    def test_rds_ya_existente_se_reusa_en_retry(self):
        # Idempotencia del paso BD (R2): DBInstanceAlreadyExists → reusar,
        # mismo patrón que el InvalidGroup.Duplicate del SG.
        base, client = _provision_base()
        error = Exception("exists")
        error.response = {"Error": {"Code": "DBInstanceAlreadyExists"}}
        client.create_db_instance.side_effect = error
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self._run_provision_chain(self._params())
        self.assertTrue(ok)
        self.assertEqual(self.env_rec.database_ids.rds_endpoint,
                         "forum-db.rds.amazonaws.com")

    def test_resume_provision_delega_en_install_resume(self):
        # Compat: job_resume_provision = install con resume=True (salta la BD).
        base, client = _provision_base()
        self.env_rec.ec2_instance_id = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id,
            "aws_instance_id": "i-resume", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.env_rec.id,
        })
        with mock.patch.object(type(self.account), "_get_aws_service", return_value=base):
            ok = self.env_rec.job_resume_provision(
                self._params(db_mode="local_pg", create_dns=False))
        self.assertTrue(ok)
        self.assertEqual(self.env_rec.state, "active")
        # resume NO re-crea la base: ni RDS ni upsert local.
        client.create_db_instance.assert_not_called()

    def test_render_install_script_reemplaza_tokens(self):
        script = self.env["primate.cloud.environment"]._render_install_script({
            "ODOO_VERSION": "19", "DOMAIN": "forum.primate.cloud",
        })
        self.assertIn("forum.primate.cloud", script)
        self.assertNotIn("%%DOMAIN%%", script)
        self.assertNotIn("%%ODOO_VERSION%%", script)
