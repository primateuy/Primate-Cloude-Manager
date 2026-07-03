# -*- coding: utf-8 -*-
"""Tests de Fase 9 — Bloque 1, paso (a): identificador estable pcm_ref.

Opción A: clave de atribución de costos inmutable. `environment.pcm_ref` se
genera por registro en create; `res.partner.pcm_ref` se genera lazy y NO se
pisa. El backfill (migración) llena solo vacíos — se ejercita su lógica acá.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestPcmRefPhase9(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente X"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Forum", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })

    def _env(self, name="E", **vals):
        base = {"name": name, "project_id": self.project.id,
                "env_type": "production", "state": "active"}
        base.update(vals)
        return self.env["primate.cloud.environment"].create(base)

    # --- environment.pcm_ref ---
    def test_env_genera_ref_en_create(self):
        env = self._env()
        self.assertTrue(env.pcm_ref)
        self.assertTrue(env.pcm_ref.startswith("pcm_env_"))

    def test_env_refs_unicos_en_lote(self):
        """Create en LOTE: cada registro obtiene un ref distinto (no colisiona
        con la constraint de unicidad, que es el riesgo de un default lambda)."""
        envs = self.env["primate.cloud.environment"].create([
            {"name": "A", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
            {"name": "B", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
            {"name": "D", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
        ])
        refs = envs.mapped("pcm_ref")
        self.assertEqual(len(set(refs)), 3)
        self.assertTrue(all(r for r in refs))

    def test_env_ref_inmutable_al_renombrar(self):
        env = self._env()
        ref = env.pcm_ref
        env.name = "Nombre Nuevo"
        self.assertEqual(env.pcm_ref, ref)  # no cambia al renombrar

    def test_env_ref_explicito_se_respeta(self):
        env = self._env(pcm_ref="pcm_env_fijo")
        self.assertEqual(env.pcm_ref, "pcm_env_fijo")

    def test_env_ref_no_se_copia(self):
        """copy=False: un duplicado NO hereda el ref (lo generaría nuevo)."""
        env = self._env()
        clon = env.copy({"name": "Clon"})
        self.assertTrue(clon.pcm_ref)
        self.assertNotEqual(clon.pcm_ref, env.pcm_ref)

    # --- res.partner.pcm_ref (lazy, no pisa) ---
    def test_partner_ref_lazy_no_default(self):
        """Un partner nuevo NO recibe ref por default (solo al pedirlo)."""
        p = self.env["res.partner"].create({"name": "Contacto suelto"})
        self.assertFalse(p.pcm_ref)

    def test_partner_ensure_genera_y_es_idempotente(self):
        ref1 = self.partner._ensure_pcm_ref()
        self.assertTrue(ref1.startswith("pcm_cli_"))
        ref2 = self.partner._ensure_pcm_ref()
        self.assertEqual(ref1, ref2)  # no pisa

    def test_partner_ensure_no_pisa_existente(self):
        self.partner.sudo().pcm_ref = "pcm_cli_preexistente"
        self.assertEqual(self.partner._ensure_pcm_ref(), "pcm_cli_preexistente")

    # --- Lógica del backfill (solo llena vacíos) ---
    def test_backfill_llena_solo_vacios(self):
        """Replica el WHERE de la migración: un entorno sin ref se llena, uno
        con ref NO se toca."""
        con_ref = self._env(name="ConRef", pcm_ref="pcm_env_yatengo")
        # Simular un registro legado sin ref (bypass del create) por SQL.
        sin_ref = self._env(name="SinRef")
        self.env.cr.execute(
            "UPDATE primate_cloud_environment SET pcm_ref = NULL WHERE id = %s",
            (sin_ref.id,))
        sin_ref.invalidate_recordset()
        # El WHERE de la migración selecciona SOLO el vacío.
        self.env.cr.execute(
            "SELECT id FROM primate_cloud_environment "
            "WHERE (pcm_ref IS NULL OR pcm_ref = '') AND id IN %s",
            (tuple([con_ref.id, sin_ref.id]),))
        ids = [r[0] for r in self.env.cr.fetchall()]
        self.assertEqual(ids, [sin_ref.id])
        self.assertEqual(con_ref.pcm_ref, "pcm_env_yatengo")  # intacto


@tagged("post_install", "-at_install", "primate_cloud")
class TestBuildTagsPhase9(TransactionCase):
    """Paso (b): build_resource_tags emite los tags estables; ausencia de
    partner degrada limpio sin romper el provision."""

    def setUp(self):
        super().setUp()
        from ..services import aws_base
        self.aws_base = aws_base

    def _as_dict(self, tags):
        return {t["Key"]: t["Value"] for t in tags}

    def test_tags_con_refs_estables(self):
        tags = self._as_dict(self.aws_base.build_resource_tags(
            client="Forum", environment="Prod",
            client_ref="pcm_cli_abc", environment_ref="pcm_env_def"))
        self.assertEqual(tags["primate:client_id"], "pcm_cli_abc")
        self.assertEqual(tags["primate:environment_id"], "pcm_env_def")
        # Los tags por nombre se conservan.
        self.assertEqual(tags["primate:client"], "Forum")
        self.assertEqual(tags["primate:environment"], "Prod")

    def test_tag_id_vacio_no_se_emite(self):
        """Un ref ausente = tag ausente, NO tag vacío."""
        tags = self._as_dict(self.aws_base.build_resource_tags(
            client="Forum", environment="Prod",
            client_ref=False, environment_ref="pcm_env_def"))
        self.assertIn("primate:environment_id", tags)
        self.assertNotIn("primate:client_id", tags)  # ausente, no ""

    def test_retrocompat_sin_refs(self):
        """Llamada vieja (sin refs) sigue funcionando: solo tags por nombre."""
        tags = self._as_dict(self.aws_base.build_resource_tags(
            client="Forum", environment="Prod"))
        self.assertEqual(tags["primate:managed_by"], "pcm")
        self.assertNotIn("primate:client_id", tags)
        self.assertNotIn("primate:environment_id", tags)


@tagged("post_install", "-at_install", "primate_cloud")
class TestAttributionRefsPhase9(TransactionCase):
    """Paso (b): _cost_attribution_refs materializa el partner y degrada limpio."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente X"})

    def _env(self, partner=True):
        project = self.env["primate.cloud.project"].create({
            "name": "P", "account_id": self.account.id,
            "partner_id": self.partner.id if partner else False,
        })
        return self.env["primate.cloud.environment"].create({
            "name": "E", "project_id": project.id, "env_type": "production",
            "state": "active",
        })

    def test_refs_con_partner_materializa_lazy(self):
        env = self._env(partner=True)
        # El partner aún no tiene ref (lazy).
        self.assertFalse(self.partner.pcm_ref)
        env_ref, client_ref = env._cost_attribution_refs()
        self.assertEqual(env_ref, env.pcm_ref)
        # Se materializó AL taggear.
        self.assertTrue(client_ref)
        self.assertTrue(client_ref.startswith("pcm_cli_"))
        self.assertEqual(self.partner.pcm_ref, client_ref)

    def test_refs_sin_partner_degrada_limpio(self):
        """Entorno cuyo proyecto no tiene partner: client_ref = False, sin
        romper. El tag de cliente simplemente no se emite."""
        env = self._env(partner=False)
        env_ref, client_ref = env._cost_attribution_refs()
        self.assertEqual(env_ref, env.pcm_ref)
        self.assertFalse(client_ref)

    def test_provision_ec2_sin_partner_no_aborta(self):
        """CASO CRÍTICO: entorno sin partner → el aprovisionamiento arma los
        tags y llama RunInstances IGUAL; el tag de cliente ausente no aborta."""
        from unittest import mock
        env = self._env(partner=False)
        base = mock.Mock()
        client = base.get_client.return_value
        client.run_instances.return_value = {"Instances": [{"InstanceId": "i-x"}]}
        client.describe_instances.return_value = {"Reservations": [{"Instances": [{
            "InstanceId": "i-x", "State": {"Name": "running"},
            "InstanceType": "t3.micro", "PublicIpAddress": "1.2.3.4",
            "PrivateIpAddress": "10.0.0.1",
            "Tags": [{"Key": "Name", "Value": "e"}], "LaunchTime": None,
        }]}]}
        params = {"instance_type": "t3.micro", "image_id": "ami-1",
                  "disk_size_gb": 30}
        # No debe lanzar: se aprovisiona igual sin tag de cliente.
        instance = env._provision_ec2(base, self.account, params, "us-east-1",
                                      "e.local")
        self.assertTrue(instance)
        # Verificar que RunInstances recibió tags SIN client_id pero CON
        # environment_id (atribución de entorno intacta).
        run_kwargs = client.run_instances.call_args.kwargs
        tag_specs = run_kwargs["TagSpecifications"][0]["Tags"]
        keys = {t["Key"] for t in tag_specs}
        self.assertIn("primate:environment_id", keys)
        self.assertNotIn("primate:client_id", keys)
