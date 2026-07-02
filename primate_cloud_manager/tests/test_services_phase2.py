# -*- coding: utf-8 -*-
"""Tests de los adaptadores AWS de Fase 2 (EC2, RDS, Route 53) con mocks."""
from datetime import datetime, timezone
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from ..services import aws_ec2, aws_rds, aws_route53


def _base_with_paginator(pages):
    """Devuelve un AwsBaseService falso cuyo paginator entrega `pages`."""
    base = mock.Mock()
    client = base.get_client.return_value
    paginator = client.get_paginator.return_value
    paginator.paginate.return_value = pages
    return base, client


@tagged("post_install", "-at_install", "primate_cloud")
class TestAwsEc2Service(TransactionCase):
    def test_list_instances_normaliza(self):
        launch = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        pages = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-abc",
                                "State": {"Name": "running"},
                                "InstanceType": "t3.medium",
                                "PublicIpAddress": "1.2.3.4",
                                "PrivateIpAddress": "10.0.0.5",
                                "LaunchTime": launch,
                                "Tags": [{"Key": "Name", "Value": "forum-prod"}],
                            }
                        ]
                    }
                ]
            }
        ]
        base, _client = _base_with_paginator(pages)
        result = aws_ec2.AwsEc2Service(base).list_instances(region="us-east-1")
        self.assertEqual(len(result), 1)
        inst = result[0]
        self.assertEqual(inst["aws_instance_id"], "i-abc")
        self.assertEqual(inst["name"], "forum-prod")
        self.assertEqual(inst["instance_state"], "running")
        self.assertEqual(inst["region"], "us-east-1")
        self.assertEqual(inst["tags"]["Name"], "forum-prod")

    def test_name_cae_a_instance_id_sin_tag(self):
        pages = [{"Reservations": [{"Instances": [{"InstanceId": "i-x", "State": {"Name": "stopped"}}]}]}]
        base, _client = _base_with_paginator(pages)
        result = aws_ec2.AwsEc2Service(base).list_instances()
        self.assertEqual(result[0]["name"], "i-x")


@tagged("post_install", "-at_install", "primate_cloud")
class TestAwsRdsService(TransactionCase):
    def test_list_instances_normaliza(self):
        pages = [
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "forum-db",
                        "Engine": "postgres",
                        "EngineVersion": "15.4",
                        "DBInstanceClass": "db.t3.medium",
                        "AllocatedStorage": 50,
                        "MultiAZ": True,
                        "BackupRetentionPeriod": 7,
                        "DBInstanceStatus": "available",
                        "Endpoint": {"Address": "forum-db.rds.amazonaws.com"},
                    }
                ]
            }
        ]
        base, _client = _base_with_paginator(pages)
        result = aws_rds.AwsRdsService(base).list_instances(region="us-east-1")
        db = result[0]
        self.assertEqual(db["rds_identifier"], "forum-db")
        self.assertEqual(db["engine"], "postgres")
        self.assertEqual(db["engine_version"], "15.4")
        self.assertTrue(db["rds_multi_az"])
        self.assertEqual(db["endpoint"], "forum-db.rds.amazonaws.com")


@tagged("post_install", "-at_install", "primate_cloud")
class TestAwsRoute53Service(TransactionCase):
    def test_list_zones(self):
        pages = [{"HostedZones": [{"Id": "/hostedzone/Z123", "Name": "primate.cloud."}]}]
        base, _client = _base_with_paginator(pages)
        zones = aws_route53.AwsRoute53Service(base).list_zones()
        self.assertEqual(zones[0]["id"], "Z123")
        self.assertEqual(zones[0]["name"], "primate.cloud")

    def test_list_records_normaliza_y_alias(self):
        pages = [
            {
                "ResourceRecordSets": [
                    {
                        "Name": "forum.primate.cloud.",
                        "Type": "A",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": "1.2.3.4"}],
                    },
                    {
                        "Name": "alias.primate.cloud.",
                        "Type": "A",
                        "AliasTarget": {"DNSName": "elb.amazonaws.com."},
                    },
                ]
            }
        ]
        base, _client = _base_with_paginator(pages)
        records = aws_route53.AwsRoute53Service(base).list_records("Z123")
        self.assertEqual(records[0]["name"], "forum.primate.cloud")
        self.assertEqual(records[0]["record_value"], "1.2.3.4")
        # El registro alias toma el DNSName del AliasTarget.
        self.assertEqual(records[1]["record_value"], "elb.amazonaws.com")
