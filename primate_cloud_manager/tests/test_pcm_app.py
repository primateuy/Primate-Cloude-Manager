# -*- coding: utf-8 -*-
"""Tests de la app PCM: preferencia de acento por usuario (aditivo)."""
from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestPcmApp(TransactionCase):
    def test_accent_default_teal(self):
        usuario = new_test_user(self.env, login="pcm_accent_user")
        self.assertEqual(usuario.pcm_accent, "teal")

    def test_accent_es_self_writeable(self):
        # El usuario puede leer/escribir su propio acento (preferencia).
        Users = self.env["res.users"]
        self.assertIn("pcm_accent", Users.SELF_WRITEABLE_FIELDS)
        self.assertIn("pcm_accent", Users.SELF_READABLE_FIELDS)
        self.assertIn("pcm_accent_custom", Users.SELF_WRITEABLE_FIELDS)
