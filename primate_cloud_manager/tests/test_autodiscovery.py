# -*- coding: utf-8 -*-
"""Tests del caché de descubrimiento por región (Bloque B2). Discovery mockeado."""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

GREEN = {
    "structural_ok": True, "structural_missing": [],
    "profile": {"exists": True},
    "vpc": {"vpc_id": "vpc-1", "has_igw": True, "ambiguous": False},
    "subnet": {"subnet_id": "subnet-1"},
    "security_group": {"id": "sg-1", "rules_ok": True, "missing_ingress": []},
}
MISSING_VPC = {
    "structural_ok": False, "structural_missing": ["vpc"],
    "profile": {"exists": True},
    "vpc": {"vpc_id": None, "has_igw": False, "ambiguous": True},
    "subnet": {"subnet_id": None}, "security_group": None,
}


@tagged("post_install", "-at_install", "primate_cloud")
class TestRegionSetup(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.Setup = self.env["primate.cloud.region.setup"]

    def _fake(self, result):
        """Parcha _discovery para devolver un discover_region canned + contador."""
        fake = mock.Mock()
        fake.discover_region.return_value = result
        return mock.patch.object(
            type(self.Setup), "_discovery", return_value=fake), fake

    def test_discover_ok_mapea_campos(self):
        setup = self.Setup.create(
            {"account_id": self.account.id, "region": "us-east-1"})
        patcher, _fake = self._fake(GREEN)
        with patcher:
            setup.action_discover()
        self.assertEqual(setup.status, "ok")
        self.assertEqual(setup.vpc_id, "vpc-1")
        self.assertEqual(setup.subnet_id, "subnet-1")
        self.assertEqual(setup.security_group_id, "sg-1")
        self.assertTrue(setup.profile_ok)
        self.assertIn("Todo listo", setup.detail)
        self.assertTrue(setup.last_discovered_at)

    def test_discover_missing_estructural_es_accionable(self):
        setup = self.Setup.create(
            {"account_id": self.account.id, "region": "us-east-1"})
        patcher, _fake = self._fake(MISSING_VPC)
        with patcher:
            setup.action_discover()
        self.assertEqual(setup.status, "missing_structural")
        # VPC ambigua → mensaje que dice QUÉ taguear (no "hay varias, elegí").
        self.assertIn("primate:managed_by", setup.detail)
        self.assertIn("re-verificá", setup.detail)

    def test_discover_error_no_rompe(self):
        setup = self.Setup.create(
            {"account_id": self.account.id, "region": "us-east-1"})
        fake = mock.Mock()
        fake.discover_region.side_effect = RuntimeError("AccessDenied boom")
        with mock.patch.object(type(self.Setup), "_discovery", return_value=fake):
            setup.action_discover()
        self.assertEqual(setup.status, "error")
        self.assertIn("boom", setup.detail)

    def test_get_or_discover_ok_fresco_reusa_cache(self):
        """OK reciente → NO se re-descubre (no golpear la API con todo verde)."""
        patcher, fake = self._fake(GREEN)
        with patcher:
            self.Setup.get_or_discover(self.account, "us-east-1")  # 1ª: descubre
            self.Setup.get_or_discover(self.account, "us-east-1")  # 2ª: reusa
        self.assertEqual(fake.discover_region.call_count, 1)

    def test_get_or_discover_no_ok_siempre_reverifica(self):
        """Invalidación HONESTA: un 'falta VPC' cacheado NO se confía viejo —
        se re-descubre siempre (el admin pudo haberlo resuelto en AWS)."""
        patcher, fake = self._fake(MISSING_VPC)
        with patcher:
            self.Setup.get_or_discover(self.account, "us-east-1")  # descubre (rojo)
            self.Setup.get_or_discover(self.account, "us-east-1")  # re-descubre igual
        self.assertEqual(fake.discover_region.call_count, 2)

    def test_get_or_discover_rojo_luego_admin_resuelve(self):
        """Rojo cacheado → el admin arregla AWS → la próxima verificación pasa a
        verde SIN quedar bloqueado por el caché viejo."""
        setup = self.Setup.create(
            {"account_id": self.account.id, "region": "us-east-1"})
        with self._fake(MISSING_VPC)[0]:
            setup.action_discover()
        self.assertEqual(setup.status, "missing_structural")
        with self._fake(GREEN)[0]:
            self.Setup.get_or_discover(self.account, "us-east-1")
        self.assertEqual(setup.status, "ok")   # se destrabó solo
