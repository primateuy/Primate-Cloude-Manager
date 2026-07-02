# -*- coding: utf-8 -*-
"""Tests del adaptador base AWS (sin tocar infra real: todo mockeado)."""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from ..services import aws_base


@tagged("post_install", "-at_install", "primate_cloud")
class TestAwsBase(TransactionCase):
    """Construcción de tags y test_connection con boto3 mockeado."""

    def test_claves_de_tags_literales(self):
        """Fija las claves obligatorias como LITERALES (D4).

        Comparar constante-vs-constante no cazaría un typo en la constante
        (p. ej. 'primaate:environment'). Estas claves sostienen la atribución
        de costos (Fase 9): si cambian, se rompe silenciosamente.
        """
        self.assertEqual(aws_base.MANAGED_BY_TAG, "primate:managed_by")
        self.assertEqual(aws_base.MANAGED_BY_VALUE, "pcm")
        self.assertEqual(aws_base.CLIENT_TAG, "primate:client")
        self.assertEqual(aws_base.ENVIRONMENT_TAG, "primate:environment")
        tags = aws_base.build_resource_tags("forum", "produccion")
        self.assertEqual(
            {t["Key"] for t in tags},
            {"primate:managed_by", "primate:client", "primate:environment"},
        )

    def test_build_resource_tags_minimos(self):
        tags = aws_base.build_resource_tags("forum", "produccion")
        as_dict = {t["Key"]: t["Value"] for t in tags}
        self.assertEqual(as_dict[aws_base.MANAGED_BY_TAG], aws_base.MANAGED_BY_VALUE)
        self.assertEqual(as_dict[aws_base.CLIENT_TAG], "forum")
        self.assertEqual(as_dict[aws_base.ENVIRONMENT_TAG], "produccion")

    def test_build_resource_tags_extra(self):
        tags = aws_base.build_resource_tags("forum", "staging", extra={"Name": "forum-stg"})
        as_dict = {t["Key"]: t["Value"] for t in tags}
        self.assertEqual(as_dict["Name"], "forum-stg")
        # Las obligatorias siguen presentes.
        self.assertIn(aws_base.MANAGED_BY_TAG, as_dict)

    def test_connection_success(self):
        with mock.patch.object(aws_base.boto3, "Session") as session_cls:
            sts = session_cls.return_value.client.return_value
            sts.get_caller_identity.return_value = {"Account": "123456789012"}
            service = aws_base.AwsBaseService("ak", "sk", "us-east-1")
            result = service.test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], "123456789012")
        self.assertIsNone(result["error"])

    def test_connection_failure(self):
        with mock.patch.object(aws_base.boto3, "Session") as session_cls:
            sts = session_cls.return_value.client.return_value
            sts.get_caller_identity.side_effect = Exception("InvalidClientTokenId")
            service = aws_base.AwsBaseService("ak", "sk", "us-east-1")
            result = service.test_connection()
        self.assertFalse(result["success"])
        self.assertIsNone(result["account_id"])
        self.assertIn("InvalidClientTokenId", result["error"])

    def test_assume_role_builds_temporary_session(self):
        with mock.patch.object(aws_base.boto3, "Session") as session_cls:
            sts = session_cls.return_value.client.return_value
            sts.assume_role.return_value = {
                "Credentials": {
                    "AccessKeyId": "tmp-ak",
                    "SecretAccessKey": "tmp-sk",
                    "SessionToken": "tmp-token",
                }
            }
            aws_base.AwsBaseService(
                access_key_id="ak",
                secret_access_key="sk",
                region="us-east-1",
                role_arn="arn:aws:iam::123456789012:role/PrimateCloud",
                external_id="ext-123",
            )
            sts.assume_role.assert_called_once()
            _, kwargs = sts.assume_role.call_args
            self.assertEqual(kwargs["ExternalId"], "ext-123")
            self.assertEqual(kwargs["RoleSessionName"], aws_base.ASSUME_ROLE_SESSION_NAME)
