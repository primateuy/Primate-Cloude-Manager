# -*- coding: utf-8 -*-
"""Tests de integración con moto: boto3 real contra un AWS simulado.

A diferencia de los tests unitarios (que mockean métodos), estos ejercitan los
adaptadores de verdad: sesiones boto3, paginación y parsing de respuestas reales
de la API de AWS, sin tocar infraestructura real ni la red. Es la verificación
más cercana a lo real posible. Se saltan si moto no está instalado.
"""
import unittest

from odoo.tests.common import TransactionCase, tagged

from ..services import aws_base, aws_ec2, aws_rds, aws_route53, aws_s3, aws_ssm

try:
    import boto3
    from moto import mock_aws

    HAS_MOTO = True
except ImportError:  # pragma: no cover
    HAS_MOTO = False

# AMI ficticia aceptada por moto.
FAKE_AMI = "ami-12345678"


def _make_base():
    """Crea el servicio base con credenciales de prueba (interceptadas por moto)."""
    return aws_base.AwsBaseService("testing", "testing", "us-east-1")


def _create_ec2(name="forum-prod"):
    """Crea una instancia EC2 en moto y devuelve su id."""
    client = boto3.client("ec2", region_name="us-east-1")
    reservation = client.run_instances(
        ImageId=FAKE_AMI, MinCount=1, MaxCount=1, InstanceType="t3.medium",
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "Name", "Value": name}]}],
    )
    return reservation["Instances"][0]["InstanceId"]


def _create_rds(identifier="forum-db"):
    """Crea una RDS PostgreSQL en moto."""
    client = boto3.client("rds", region_name="us-east-1")
    client.create_db_instance(
        DBInstanceIdentifier=identifier, DBInstanceClass="db.t3.medium",
        Engine="postgres", EngineVersion="15.4", AllocatedStorage=50,
        MasterUsername="odoo", MasterUserPassword="secret123", BackupRetentionPeriod=7,
    )


def _create_route53_record(zone_name="primate.cloud.", record="forum.primate.cloud"):
    """Crea una zona y un registro A en moto. Devuelve el hosted zone id."""
    client = boto3.client("route53", region_name="us-east-1")
    zone = client.create_hosted_zone(Name=zone_name, CallerReference="ref-1")
    zone_id = zone["HostedZone"]["Id"].split("/")[-1]
    client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Changes": [{
            "Action": "CREATE",
            "ResourceRecordSet": {"Name": record, "Type": "A", "TTL": 300,
                                  "ResourceRecords": [{"Value": "1.2.3.4"}]},
        }]},
    )
    return zone_id


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud", "primate_cloud_moto")
class TestMotoServices(TransactionCase):
    """Los adaptadores funcionan contra la API real (simulada) de AWS."""

    def test_sts_connection(self):
        with mock_aws():
            result = _make_base().test_connection()
        self.assertTrue(result["success"])
        self.assertTrue(result["account_id"])

    def test_ec2_list_get_y_lifecycle(self):
        with mock_aws():
            instance_id = _create_ec2("forum-prod")
            service = aws_ec2.AwsEc2Service(_make_base())

            listed = service.list_instances(region="us-east-1")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["aws_instance_id"], instance_id)
            self.assertEqual(listed[0]["name"], "forum-prod")
            self.assertEqual(listed[0]["instance_state"], "running")

            # Acciones de ciclo de vida con AWS Request ID real.
            self.assertTrue(service.stop_instance(instance_id, region="us-east-1"))
            refreshed = service.get_instance(instance_id, region="us-east-1")
            self.assertEqual(refreshed["instance_state"], "stopped")

    def test_rds_list(self):
        with mock_aws():
            _create_rds("forum-db")
            dbs = aws_rds.AwsRdsService(_make_base()).list_instances(region="us-east-1")
        self.assertEqual(len(dbs), 1)
        self.assertEqual(dbs[0]["rds_identifier"], "forum-db")
        self.assertEqual(dbs[0]["engine"], "postgres")
        self.assertTrue(dbs[0]["engine_version"].startswith("15"))

    def test_route53_zones_y_records(self):
        with mock_aws():
            _create_route53_record()
            service = aws_route53.AwsRoute53Service(_make_base())
            zones = service.list_zones()
            self.assertEqual(len(zones), 1)
            records = service.list_records(zones[0]["id"])
        names = {r["name"] for r in records}
        self.assertIn("forum.primate.cloud", names)
        a_record = next(r for r in records if r["name"] == "forum.primate.cloud")
        self.assertEqual(a_record["record_type"], "A")
        self.assertEqual(a_record["record_value"], "1.2.3.4")

    def test_ssm_send_command(self):
        with mock_aws():
            service = aws_ssm.AwsSsmService(_make_base())
            # moto registra la instancia para SSM al crearla.
            instance_id = _create_ec2("ssm-host")
            command_id = service.send_command(instance_id, "echo hola", region="us-east-1")
        self.assertTrue(command_id)


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud", "primate_cloud_moto")
class TestMotoEndToEndSync(TransactionCase):
    """Flujo completo: job_sync_resources real contra moto, escribiendo en Odoo."""

    def test_job_sync_resources_end_to_end(self):
        account = self.env["primate.cloud.account"].create({
            "name": "Cuenta Moto",
            "default_region": "us-east-1",
            "iam_access_key_id": "testing",
            "iam_secret_access_key": "testing",
            "connection_state": "connected",
        })
        with mock_aws():
            _create_ec2("forum-prod")
            _create_rds("forum-db")
            _create_route53_record()
            # Sin mockear servicios: usa boto3 real (interceptado por moto).
            result = account.job_sync_resources()

        self.assertTrue(result)
        self.assertTrue(account.last_sync_date)
        ec2 = self.env["primate.cloud.ec2.instance"].search([("account_id", "=", account.id)])
        self.assertEqual(len(ec2), 1)
        self.assertEqual(ec2.name, "forum-prod")
        db = self.env["primate.cloud.database"].search([("account_id", "=", account.id)])
        self.assertEqual(len(db), 1)
        self.assertEqual(db.pg_version, "15")
        dns = self.env["primate.cloud.dns.record"].search([("account_id", "=", account.id)])
        self.assertTrue(dns.filtered(lambda r: r.name == "forum.primate.cloud"))
        # La operación quedó registrada como exitosa.
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", account.id), ("action_type", "=", "sync"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)

    def test_job_lifecycle_stop_end_to_end(self):
        account = self.env["primate.cloud.account"].create({
            "name": "Cuenta Ciclo", "default_region": "us-east-1",
            "iam_access_key_id": "testing", "iam_secret_access_key": "testing",
        })
        with mock_aws():
            iid = _create_ec2("srv-lifecycle")
            instance = self.env["primate.cloud.ec2.instance"].create({
                "name": "srv-lifecycle", "account_id": account.id,
                "aws_instance_id": iid, "instance_state": "running", "region": "us-east-1",
            })
            ok = instance.job_lifecycle("ec2_stop")
        self.assertTrue(ok)
        # El estado se actualizó con lo que reporta AWS tras detenerla.
        self.assertIn(instance.instance_state, ("stopping", "stopped"))
        log = self.env["primate.cloud.operation.log"].search(
            [("resource_id", "=", instance.id), ("action_type", "=", "ec2_stop"),
             ("result", "=", "success")]
        )
        self.assertTrue(log)
        self.assertTrue(log.aws_request_id)

    def test_validate_connection_end_to_end(self):
        account = self.env["primate.cloud.account"].create({
            "name": "Cuenta Conn",
            "default_region": "us-east-1",
            "iam_access_key_id": "testing",
            "iam_secret_access_key": "testing",
        })
        with mock_aws():
            ok = account.job_validate_connection()
        self.assertTrue(ok)
        self.assertEqual(account.connection_state, "connected")
        self.assertTrue(account.aws_account_id)


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud", "primate_cloud_moto")
class TestMotoCreate(TransactionCase):
    """Creación real (Fase 4) de EC2 / RDS / DNS contra moto."""

    def test_ec2_create_instance(self):
        with mock_aws():
            service = aws_ec2.AwsEc2Service(_make_base())
            data = service.create_instance(
                image_id=FAKE_AMI, instance_type="t3.medium",
                tags=[{"Key": "Name", "Value": "nuevo-srv"},
                      {"Key": "primate:managed_by", "Value": "pcm"}],
                region="us-east-1", disk_size_gb=30, _sleep=lambda s: None,
            )
        # moto arranca las instancias en 'running' de inmediato.
        self.assertTrue(data["aws_instance_id"].startswith("i-"))
        self.assertEqual(data["instance_state"], "running")
        self.assertEqual(data["name"], "nuevo-srv")

    def test_rds_create_instance(self):
        with mock_aws():
            service = aws_rds.AwsRdsService(_make_base())
            data = service.create_instance(
                identifier="nuevo-db", instance_class="db.t3.medium", storage_gb=20,
                master_username="odoo", master_password="secret123", tags=[],
                engine_version="16.3", region="us-east-1", _sleep=lambda s: None,
            )
        self.assertEqual(data["rds_identifier"], "nuevo-db")
        self.assertEqual(data["status"], "available")
        self.assertEqual(data["engine"], "postgres")

    def test_route53_create_record(self):
        with mock_aws():
            client = boto3.client("route53", region_name="us-east-1")
            zone = client.create_hosted_zone(Name="primate.cloud.", CallerReference="r2")
            zone_id = zone["HostedZone"]["Id"].split("/")[-1]
            service = aws_route53.AwsRoute53Service(_make_base())
            change_id = service.create_record(
                zone_id, "nuevo.primate.cloud", "A", "5.6.7.8", ttl=300,
            )
            self.assertTrue(change_id)
            records = service.list_records(zone_id)
        match = [r for r in records if r["name"] == "nuevo.primate.cloud"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["record_value"], "5.6.7.8")


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud")
class TestMotoPhase8(TransactionCase):
    """Fase 8: evidencia de respaldos (retención RDS, snapshots, listing S3)."""

    def test_rds_retencion_y_snapshots(self):
        with mock_aws():
            _create_rds("forum-db")
            service = aws_rds.AwsRdsService(_make_base())
            data = service.get_instance("forum-db", region="us-east-1")
            self.assertEqual(data["backup_retention_days"], 7)
            # Snapshot manual: aparece en la evidencia con fecha y tipo.
            boto3.client("rds", region_name="us-east-1").create_db_snapshot(
                DBSnapshotIdentifier="manual-1", DBInstanceIdentifier="forum-db",
            )
            snapshots = service.list_snapshots(
                identifier="forum-db", region="us-east-1"
            )
        # moto replica AWS: además del manual existe el snapshot automático
        # que genera la creación de la RDS con retención > 0.
        by_type = {s["snapshot_type"]: s for s in snapshots}
        self.assertIn("manual", by_type)
        manual = by_type["manual"]
        self.assertEqual(manual["snapshot_id"], "manual-1")
        self.assertEqual(manual["rds_identifier"], "forum-db")
        self.assertTrue(all(s["created_at"] for s in snapshots))

    def test_s3_list_y_head(self):
        with mock_aws():
            service = aws_s3.AwsS3Service(_make_base())
            service.ensure_bucket("pcm-backups-test", region="us-east-1")
            client = boto3.client("s3", region_name="us-east-1")
            client.put_object(Bucket="pcm-backups-test",
                              Key="pcm-backups/forum/a.dump", Body=b"dump")
            client.put_object(Bucket="pcm-backups-test",
                              Key="otro/b.dump", Body=b"x")
            objects = service.list_objects(
                "pcm-backups-test", prefix="pcm-backups/forum/", region="us-east-1"
            )
            self.assertEqual([o["key"] for o in objects],
                             ["pcm-backups/forum/a.dump"])
            self.assertEqual(objects[0]["size"], 4)
            self.assertTrue(objects[0]["last_modified"])
            head = service.head_object(
                "pcm-backups-test", "pcm-backups/forum/a.dump", region="us-east-1"
            )
            self.assertEqual(head["size"], 4)
            missing = service.head_object(
                "pcm-backups-test", "no-existe.dump", region="us-east-1"
            )
        self.assertIsNone(missing)

    def test_s3_lifecycle_merge_de_reglas(self):
        """put_lifecycle_rule no pisa reglas ajenas: lee, mergea por id y sube."""
        with mock_aws():
            service = aws_s3.AwsS3Service(_make_base())
            service.ensure_bucket("pcm-lc-test", region="us-east-1")
            # Primera regla sobre bucket sin configuración (NoSuchLifecycle...).
            rule_a = service.put_lifecycle_rule(
                "pcm-lc-test", "pcm-backups/", 7, region="us-east-1"
            )
            # Segunda regla con otro prefijo: la primera debe sobrevivir.
            rule_b = service.put_lifecycle_rule(
                "pcm-lc-test", "otro-prefijo/", 30, region="us-east-1"
            )
            # Re-aplicar la primera con otra retención: actualiza, no duplica.
            service.put_lifecycle_rule(
                "pcm-lc-test", "pcm-backups/", 14, region="us-east-1"
            )
            client = boto3.client("s3", region_name="us-east-1")
            rules = client.get_bucket_lifecycle_configuration(
                Bucket="pcm-lc-test"
            )["Rules"]
        by_id = {rule["ID"]: rule for rule in rules}
        self.assertEqual(set(by_id), {rule_a, rule_b})
        self.assertEqual(by_id[rule_a]["Expiration"]["Days"], 14)
        self.assertEqual(by_id[rule_b]["Expiration"]["Days"], 30)
