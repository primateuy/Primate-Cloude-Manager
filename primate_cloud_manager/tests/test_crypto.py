# -*- coding: utf-8 -*-
"""Tests del helper puro de cifrado."""
from odoo.tests.common import TransactionCase, tagged

from ..tools import crypto


@tagged("post_install", "-at_install", "primate_cloud")
class TestCrypto(TransactionCase):
    """El cifrado es reversible con la clave correcta e ilegible sin ella."""

    def test_roundtrip(self):
        key = crypto.generate_key()
        secret = "AKIA-super-secreto-123"
        token = crypto.encrypt(key, secret)
        self.assertNotEqual(token, secret, "El token no debe ser el texto plano.")
        self.assertEqual(crypto.decrypt(key, token), secret)

    def test_empty_values(self):
        key = crypto.generate_key()
        self.assertFalse(crypto.encrypt(key, ""))
        self.assertFalse(crypto.encrypt(key, False))
        self.assertFalse(crypto.decrypt(key, ""))
        self.assertFalse(crypto.decrypt(key, False))

    def test_wrong_key_returns_false(self):
        token = crypto.encrypt(crypto.generate_key(), "dato")
        # Descifrar con otra clave no debe explotar: devuelve False.
        self.assertFalse(crypto.decrypt(crypto.generate_key(), token))

    def test_corrupt_token_returns_false(self):
        self.assertFalse(crypto.decrypt(crypto.generate_key(), "no-es-un-token"))
