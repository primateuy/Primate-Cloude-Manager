# -*- coding: utf-8 -*-
"""Tests de Fase 8.5 — Bloque 1: cimientos del CRUD de DNS.

Servicio aws_route53 (delete + get_change, con moto), campos y reglas del
modelo dns.record: guardia de borrado, validación de forma por tipo, detección
de divergencia en el sync y comparación multi-valor.
"""
import unittest
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.primate_cloud_operation_log import ACTION_TYPES
from ..services import aws_route53

try:
    import boto3
    from moto import mock_aws

    HAS_MOTO = True
except ImportError:  # pragma: no cover
    HAS_MOTO = False


@tagged("post_install", "-at_install", "primate_cloud")
class TestDnsModelPhase85(TransactionCase):
    """Modelo dns.record: guardia, validación, divergencia (sin AWS)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test Phase85"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id, "partner_id": self.partner.id}
        )
        self.prod = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.staging = self.env["primate.cloud.environment"].create({
            "name": "Forum Staging", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
        })
        self.Dns = self.env["primate.cloud.dns.record"]

    def _record(self, **vals):
        base = {
            "name": "forum.primate.cloud", "account_id": self.account.id,
            "hosted_zone_id": "Z123", "record_type": "A",
            "record_value": "1.2.3.4", "ttl": 300,
        }
        base.update(vals)
        return self.Dns.create(base)

    # --- Guardia de borrado (ajuste aprobado: sin entorno = alta) ---
    def test_guardia_alta_en_produccion(self):
        rec = self._record(environment_id=self.prod.id)
        self.assertTrue(rec.delete_needs_ack)

    def test_guardia_baja_en_no_produccion(self):
        rec = self._record(environment_id=self.staging.id)
        self.assertFalse(rec.delete_needs_ack)

    def test_guardia_alta_sin_entorno(self):
        """Registro huérfano (sin entorno): 'desconocido' = potencialmente
        producción → fricción ALTA, nunca baja."""
        rec = self._record(environment_id=False)
        self.assertTrue(rec.delete_needs_ack)

    # --- Validación de forma por tipo ---
    def test_validate_a_ipv4(self):
        self.Dns._validate_dns_value("A", ["1.2.3.4", "10.0.0.1"])
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("A", ["1.2.3.4", "no-ip"])
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("A", ["999.1.1.1"])

    def test_validate_cname_unico_hostname(self):
        self.Dns._validate_dns_value("CNAME", ["forum.primate.cloud"])
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("CNAME", ["a.com", "b.com"])  # >1
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("CNAME", ["1.2.3.4"])  # no hostname

    def test_validate_mx_prioridad_host(self):
        self.Dns._validate_dns_value("MX", ["10 mail.forum.cloud"])
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("MX", ["mail.forum.cloud"])  # sin prio
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("MX", ["10 no_host con espacio"])

    def test_validate_txt_no_vacio_y_tipo_desconocido(self):
        self.Dns._validate_dns_value("TXT", ["v=spf1 -all"])
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("A", [])  # sin valores
        with self.assertRaises(ValidationError):
            self.Dns._validate_dns_value("AAAA", ["::1"])  # no soportado v1

    # --- Comparación multi-valor (conjuntos, sin orden) ---
    def test_valores_difieren_por_conjunto(self):
        self.assertFalse(self.Dns._dns_values_differ("1.1.1.1, 2.2.2.2",
                                                     "2.2.2.2, 1.1.1.1"))
        self.assertTrue(self.Dns._dns_values_differ("1.1.1.1", "1.1.1.2"))
        self.assertTrue(self.Dns._dns_values_differ("1.1.1.1, 2.2.2.2",
                                                    "1.1.1.1"))

    # --- Divergencia en el sync ---
    def test_sync_marca_synced_si_coincide(self):
        rec = self._record(record_value="1.2.3.4", ttl=300)
        self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z123", "name": "forum.primate.cloud",
            "record_type": "A", "record_value": "1.2.3.4", "ttl": 300,
        }])
        self.assertEqual(rec.sync_state, "synced")
        self.assertFalse(rec.record_value_aws)

    def test_sync_marca_divergent_si_valor_difiere(self):
        rec = self._record(record_value="1.2.3.4", ttl=300)
        self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z123", "name": "forum.primate.cloud",
            "record_type": "A", "record_value": "9.9.9.9", "ttl": 300,
        }])
        self.assertEqual(rec.sync_state, "divergent")
        self.assertEqual(rec.record_value_aws, "9.9.9.9")
        # NO se pisa el valor de PCM.
        self.assertEqual(rec.record_value, "1.2.3.4")

    def test_sync_marca_divergent_si_ttl_difiere(self):
        rec = self._record(record_value="1.2.3.4", ttl=300)
        self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z123", "name": "forum.primate.cloud",
            "record_type": "A", "record_value": "1.2.3.4", "ttl": 60,
        }])
        self.assertEqual(rec.sync_state, "divergent")

    def test_sync_normaliza_dot_y_case_del_nombre(self):
        """Mismo registro con distinto case y punto final (FQDN) → synced y SIN
        duplicado; no debe marcarse divergent por diferencia cosmética."""
        rec = self._record(name="Forum.Primate.Cloud", record_value="1.2.3.4",
                           ttl=300)
        before = self.Dns.search_count([])
        self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z123", "name": "forum.primate.cloud.",
            "record_type": "A", "record_value": "1.2.3.4", "ttl": 300,
        }])
        self.assertEqual(rec.sync_state, "synced")
        self.assertFalse(rec.record_value_aws)
        # No se creó un duplicado por la diferencia de formato.
        self.assertEqual(self.Dns.search_count([]), before)

    def test_sync_puebla_is_alias(self):
        """El sync marca is_alias desde AliasTarget (señal real), no desde TTL."""
        self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z1", "name": "alias.primate.cloud",
            "record_type": "A", "record_value": "elb.amazonaws.com",
            "ttl": 300, "is_alias": True,
        }, {
            "hosted_zone_id": "Z1", "name": "simple.primate.cloud",
            "record_type": "A", "record_value": "1.2.3.4",
            "ttl": 300, "is_alias": False,
        }])
        alias = self.Dns.search([("name", "=", "alias.primate.cloud")])
        simple = self.Dns.search([("name", "=", "simple.primate.cloud")])
        self.assertTrue(alias.is_alias)
        self.assertFalse(simple.is_alias)

    def test_normalize_record_marca_alias(self):
        """_normalize_record deriva is_alias de AliasTarget, no del TTL."""
        alias_rrset = {
            "Name": "cdn.forum.cloud.", "Type": "A",
            "AliasTarget": {"DNSName": "d123.cloudfront.net."},
        }
        simple_rrset = {
            "Name": "web.forum.cloud.", "Type": "A", "TTL": 0,
            "ResourceRecords": [{"Value": "1.2.3.4"}],
        }
        alias = aws_route53.AwsRoute53Service._normalize_record(alias_rrset, "Z1")
        simple = aws_route53.AwsRoute53Service._normalize_record(simple_rrset, "Z1")
        self.assertTrue(alias["is_alias"])
        # TTL 0 pero NO alias: tiene ResourceRecords.
        self.assertFalse(simple["is_alias"])

    def test_normalize_dns_name(self):
        self.assertEqual(self.Dns._normalize_dns_name("Forum.X.Com."),
                         "forum.x.com")
        self.assertEqual(self.Dns._normalize_dns_name("  A.B.  "), "a.b")

    def test_sync_nuevo_desde_aws_queda_synced(self):
        res = self.Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "Z999", "name": "nuevo.primate.cloud",
            "record_type": "CNAME", "record_value": "forum.primate.cloud",
            "ttl": 300,
        }])
        self.assertEqual(res["created"], 1)
        rec = self.Dns.search([("name", "=", "nuevo.primate.cloud")])
        self.assertEqual(rec.sync_state, "synced")

    def test_action_types_dns(self):
        keys = {k for k, _l in ACTION_TYPES}
        self.assertIn("dns_update", keys)
        self.assertIn("dns_delete", keys)


@tagged("post_install", "-at_install", "primate_cloud")
class TestDnsJobsPhase85(TransactionCase):
    """Jobs de aplicación/propagación/divergencia (Bloque 2), Route 53 mockeado."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test Phase85"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id, "partner_id": self.partner.id}
        )
        self.env_prod = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.Dns = self.env["primate.cloud.dns.record"]
        self.rec = self.Dns.create({
            "name": "a.forum.cloud", "account_id": self.account.id,
            "environment_id": self.env_prod.id, "hosted_zone_id": "Z1",
            "record_type": "A", "record_value": "1.2.3.4", "ttl": 300,
        })

    def _aws_record(self, value="1.2.3.4", ttl=300):
        return {"name": "a.forum.cloud", "record_type": "A",
                "record_value": value, "ttl": ttl}

    # --- Propagación: nunca synced mientras PENDING ---
    def test_poll_pending_no_pasa_a_synced(self):
        service = mock.Mock()
        service.get_change_status.return_value = "PENDING"
        self.rec.sync_state = "pending"
        res = self.rec._poll_and_finalize(
            service, "c1", timeout=2, interval=1, _sleep=lambda s: None)
        self.assertEqual(res, "PENDING")
        self.assertEqual(self.rec.sync_state, "pending")  # NUNCA synced

    def test_poll_insync_finaliza_synced(self):
        service = mock.Mock()
        service.get_change_status.return_value = "INSYNC"
        service.list_records.return_value = [self._aws_record()]
        res = self.rec._poll_and_finalize(
            service, "c1", timeout=10, interval=1, _sleep=lambda s: None)
        self.assertEqual(res, "INSYNC")
        self.assertEqual(self.rec.sync_state, "synced")

    # --- Finalización contra la realidad de AWS ---
    def test_finalize_divergente_si_alguien_cambio_afuera(self):
        service = mock.Mock()
        service.list_records.return_value = [self._aws_record(value="9.9.9.9")]
        self.rec._finalize_dns_sync(service)
        self.assertEqual(self.rec.sync_state, "divergent")
        self.assertEqual(self.rec.record_value_aws, "9.9.9.9")

    def test_finalize_borrado_confirma_ausencia(self):
        self.rec.state = "deleted"
        service = mock.Mock()
        service.list_records.return_value = []  # ya no está
        self.rec._finalize_dns_sync(service)
        self.assertEqual(self.rec.sync_state, "synced")

    def test_finalize_borrado_divergente_si_sigue(self):
        self.rec.state = "deleted"
        service = mock.Mock()
        service.list_records.return_value = [self._aws_record()]
        self.rec._finalize_dns_sync(service)
        self.assertEqual(self.rec.sync_state, "divergent")

    # --- job_apply_change / job_delete (servicio mockeado) ---
    def test_job_apply_error_marca_error(self):
        service = mock.Mock()
        service.create_record.side_effect = Exception("AccessDenied")
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            ok = self.rec.job_apply_change()
        self.assertFalse(ok)
        self.assertEqual(self.rec.sync_state, "error")

    def test_job_apply_ok_encadena_pending_a_synced(self):
        service = mock.Mock()
        service.create_record.return_value = "/change/C1"
        service.get_change_status.return_value = "INSYNC"
        service.list_records.return_value = [self._aws_record()]
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            ok = self.rec.job_apply_change(action_type="dns_create")
        self.assertTrue(ok)
        self.assertEqual(self.rec.last_change_id, "/change/C1")
        self.assertEqual(self.rec.sync_state, "synced")
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "dns_create"), ("resource_id", "=", self.rec.id)],
            limit=1)
        self.assertEqual(log.result, "success")

    def test_job_delete_marca_deleted(self):
        service = mock.Mock()
        service.delete_record.return_value = "/change/C2"
        service.get_change_status.return_value = "INSYNC"
        service.list_records.return_value = []  # confirmado ausente
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            ok = self.rec.job_delete()
        self.assertTrue(ok)
        self.assertEqual(self.rec.state, "deleted")
        self.assertEqual(self.rec.sync_state, "synced")

    def test_apply_change_valida_forma(self):
        with self.assertRaises(ValidationError):
            self.rec.action_apply_change({"record_value": "no-ip"})

    # --- Cron anti-zombi ---
    def test_cron_resync_insync_finaliza(self):
        self.rec.write({"sync_state": "pending", "last_change_id": "/change/C1"})
        service = mock.Mock()
        service.get_change_status.return_value = "INSYNC"
        service.list_records.return_value = [self._aws_record()]
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            self.Dns._cron_resync_pending()
        self.assertEqual(self.rec.sync_state, "synced")

    def test_cron_resync_zombi_a_error(self):
        """Un pending que sigue PENDING más del umbral → error (ni eterno ni
        falso synced). Se envejece write_date por SQL para simularlo."""
        self.rec.write({"sync_state": "pending", "last_change_id": "/change/C1"})
        # Flush ANTES de envejecer write_date por SQL: si no, el flush implícito
        # del search del cron reescribiría write_date a now() y anularía el SQL.
        self.rec.flush_recordset()
        old = fields.Datetime.now() - timedelta(hours=2)
        self.env.cr.execute(
            "UPDATE primate_cloud_dns_record SET write_date = %s WHERE id = %s",
            (old, self.rec.id))
        self.rec.invalidate_recordset()
        service = mock.Mock()
        service.get_change_status.return_value = "PENDING"
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            self.Dns._cron_resync_pending()
        self.assertEqual(self.rec.sync_state, "error")

    def test_cron_resync_pending_fresco_sigue_pending(self):
        self.rec.write({"sync_state": "pending", "last_change_id": "/change/C1"})
        service = mock.Mock()
        service.get_change_status.return_value = "PENDING"
        with mock.patch.object(type(self.rec), "_service", return_value=service):
            self.Dns._cron_resync_pending()
        self.assertEqual(self.rec.sync_state, "pending")


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud")
class TestRoute53ServicePhase85(TransactionCase):
    """aws_route53: delete_record + get_change_status contra moto real."""

    def _base(self):
        from ..services import aws_base
        return aws_base.AwsBaseService("testing", "testing", "us-east-1")

    def _zone(self, client, name="test.local."):
        zone = client.create_hosted_zone(Name=name, CallerReference="r85")
        return zone["HostedZone"]["Id"].split("/")[-1]

    def test_create_delete_y_get_change(self):
        with mock_aws():
            client = boto3.client("route53", region_name="us-east-1")
            zone_id = self._zone(client)
            service = aws_route53.AwsRoute53Service(self._base())
            # Crear (UPSERT) y confirmar propagación.
            change_id = service.create_record(
                zone_id, "a.test.local", "A", "1.2.3.4", ttl=300)
            self.assertTrue(change_id)
            self.assertEqual(service.get_change_status(change_id), "INSYNC")
            records = service.list_records(zone_id)
            self.assertTrue(any(r["name"] == "a.test.local" for r in records))
            # Borrar con el RRSet exacto.
            del_id = service.delete_record(
                zone_id, "a.test.local", "A", "1.2.3.4", ttl=300)
            self.assertTrue(del_id)
            records = service.list_records(zone_id)
        self.assertFalse(any(r["name"] == "a.test.local" for r in records))

    def test_delete_multivalor(self):
        with mock_aws():
            client = boto3.client("route53", region_name="us-east-1")
            zone_id = self._zone(client)
            service = aws_route53.AwsRoute53Service(self._base())
            service.create_record(
                zone_id, "multi.test.local", "A",
                ["1.1.1.1", "2.2.2.2"], ttl=300)
            del_id = service.delete_record(
                zone_id, "multi.test.local", "A",
                ["1.1.1.1", "2.2.2.2"], ttl=300)
            self.assertTrue(del_id)
            records = service.list_records(zone_id)
        self.assertFalse(any(r["name"] == "multi.test.local" for r in records))

    def test_job_apply_y_delete_end_to_end(self):
        """job_apply_change/job_delete completos contra moto: crear→synced,
        borrar→deleted+synced (registro ausente en la zona)."""
        with mock_aws():
            client = boto3.client("route53", region_name="us-east-1")
            zone_id = self._zone(client, name="forum.cloud.")
            account = self.env["primate.cloud.account"].create({
                "name": "moto", "default_region": "us-east-1",
                "iam_access_key_id": "testing", "iam_secret_access_key": "testing",
            })
            partner = self.env["res.partner"].create({"name": "Cliente Test Phase85 moto"})
            project = self.env["primate.cloud.project"].create(
                {"name": "P", "account_id": account.id, "partner_id": partner.id})
            environment = self.env["primate.cloud.environment"].create({
                "name": "E", "project_id": project.id, "env_type": "staging",
                "state": "active"})
            rec = self.env["primate.cloud.dns.record"].create({
                "name": "web.forum.cloud", "account_id": account.id,
                "environment_id": environment.id, "hosted_zone_id": zone_id,
                "record_type": "A", "record_value": "1.2.3.4", "ttl": 300,
            })
            ok = rec.job_apply_change(action_type="dns_create")
            self.assertTrue(ok)
            self.assertEqual(rec.sync_state, "synced")
            self.assertEqual(rec.state, "active")
            # Editar el valor → sigue synced con el valor nuevo.
            rec.record_value = "5.6.7.8"
            rec.job_apply_change()
            self.assertEqual(rec.sync_state, "synced")
            self.assertEqual(rec.record_value_aws, False)
            # Borrar → deleted + confirmado ausente.
            rec.job_delete()
            self.assertEqual(rec.state, "deleted")
            self.assertEqual(rec.sync_state, "synced")


@tagged("post_install", "-at_install", "primate_cloud")
class TestDnsWizardsPhase85(TransactionCase):
    """Wizards de crear/editar y borrar (Bloque 3): validación, alias, guardia."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Test Phase85"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id, "partner_id": self.partner.id}
        )
        self.prod = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.staging = self.env["primate.cloud.environment"].create({
            "name": "Forum Staging", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
        })
        self.Dns = self.env["primate.cloud.dns.record"]
        self.CreateWiz = self.env["primate.cloud.dns.record.wizard"]
        self.DeleteWiz = self.env["primate.cloud.dns.delete.wizard"]

    def _record(self, **vals):
        base = {
            "name": "forum.primate.cloud", "account_id": self.account.id,
            "hosted_zone_id": "Z1", "record_type": "A",
            "record_value": "1.2.3.4", "ttl": 300, "environment_id": self.staging.id,
        }
        base.update(vals)
        return self.Dns.create(base)

    # --- Crear/editar ---
    def test_wizard_crea_y_encola(self):
        wiz = self.CreateWiz.create({
            "account_id": self.account.id, "environment_id": self.staging.id,
            "hosted_zone_id": "Z1", "name": "web.forum.cloud",
            "record_type": "A", "record_value": "1.2.3.4\n5.6.7.8", "ttl": 300,
        })
        with mock.patch.object(type(self.Dns), "with_delay") as with_delay:
            wiz.action_confirm()
            with_delay.assert_called_once()
        rec = self.Dns.search([("name", "=", "web.forum.cloud")])
        self.assertTrue(rec)
        # Multi-valor unido con ', '.
        self.assertEqual(rec.record_value, "1.2.3.4, 5.6.7.8")
        self.assertEqual(rec.sync_state, "pending")

    def test_wizard_valida_forma(self):
        wiz = self.CreateWiz.create({
            "account_id": self.account.id, "hosted_zone_id": "Z1",
            "name": "web.forum.cloud", "record_type": "A",
            "record_value": "no-es-ip",
        })
        with self.assertRaises(ValidationError):
            wiz.action_confirm()

    def test_wizard_edicion_precarga(self):
        rec = self._record()
        wiz = self.CreateWiz.with_context(
            default_record_id=rec.id).create({})
        self.assertTrue(wiz.is_edit)
        self.assertEqual(wiz.name, rec.name)
        self.assertEqual(wiz.record_type, "A")

    def test_wizard_bloquea_alias(self):
        """Un registro alias (marcador EXPLÍCITO is_alias) no se edita."""
        alias = self._record(name="alias.forum.cloud", is_alias=True,
                             record_value="elb-123.us-east-1.elb.amazonaws.com")
        with self.assertRaises(UserError):
            self.CreateWiz.with_context(default_record_id=alias.id).create({})

    def test_wizard_permite_registro_simple_ttl_cero(self):
        """Un registro simple con TTL 0 (válido aunque raro) NO es alias: debe
        poder editarse. El proxy por TTL lo habría bloqueado mal."""
        rec = self._record(name="ttl0.forum.cloud", ttl=0, is_alias=False)
        wiz = self.CreateWiz.with_context(default_record_id=rec.id).create({})
        self.assertTrue(wiz.is_edit)
        self.assertEqual(wiz.name, "ttl0.forum.cloud")

    # --- Borrado (guardia server-side) ---
    def test_delete_exige_nombre_exacto(self):
        rec = self._record(environment_id=self.staging.id)
        wiz = self.DeleteWiz.create({
            "record_id": rec.id, "confirm_name": "otro.nombre"})
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_delete_no_prod_sin_ack_ok(self):
        rec = self._record(environment_id=self.staging.id)  # no prod
        wiz = self.DeleteWiz.create({
            "record_id": rec.id, "confirm_name": rec.name})
        self.assertFalse(wiz.needs_ack)
        with mock.patch.object(type(rec), "with_delay") as with_delay:
            wiz.action_confirm()
            with_delay.assert_called_once()

    def test_delete_prod_exige_ack(self):
        rec = self._record(environment_id=self.prod.id)  # prod
        self.assertTrue(rec.delete_needs_ack)
        wiz = self.DeleteWiz.create({
            "record_id": rec.id, "confirm_name": rec.name, "acknowledge": False})
        with self.assertRaises(UserError):
            wiz.action_confirm()
        wiz.acknowledge = True
        with mock.patch.object(type(rec), "with_delay") as with_delay:
            wiz.action_confirm()
            with_delay.assert_called_once()

    def test_delete_huerfano_exige_ack(self):
        """Registro sin entorno (origen desconocido): guardia alta igual."""
        rec = self._record(environment_id=False)
        self.assertTrue(rec.delete_needs_ack)
        wiz = self.DeleteWiz.create({
            "record_id": rec.id, "confirm_name": rec.name, "acknowledge": False})
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_action_open_delete_bloquea_ya_borrado(self):
        rec = self._record(state="deleted")
        with self.assertRaises(ValidationError):
            rec.action_open_delete()
