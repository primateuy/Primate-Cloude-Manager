# -*- coding: utf-8 -*-
"""Tests de Fase 8 — Bloque 1: cimientos de respaldos.

Modelos backup.policy y backup, campos de cumplimiento en environment,
constraint de coherencia entorno↔instancia en database y action_types nuevos.
Sin AWS: todo es modelo puro.
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.primate_cloud_operation_log import ACTION_TYPES


@tagged("post_install", "-at_install", "primate_cloud")
class TestPhase8Foundations(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.env_a = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.env_b = self.env["primate.cloud.environment"].create({
            "name": "Otro Entorno", "project_id": self.project.id,
            "env_type": "testing", "state": "active",
        })
        self.instance_a = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-a", "account_id": self.account.id,
            "environment_id": self.env_a.id, "aws_instance_id": "i-aaa",
            "instance_state": "running", "region": "us-east-1",
        })
        self.instance_b = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-b", "account_id": self.account.id,
            "environment_id": self.env_b.id, "aws_instance_id": "i-bbb",
            "instance_state": "running", "region": "us-east-1",
        })

    # ------------------------------------------------------------------
    # Políticas de respaldo
    # ------------------------------------------------------------------
    def test_policy_seeds_existen(self):
        """Las 4 seeds de la spec §10.1 existen y cargaron sin error.

        NO se testean sus valores de negocio: son noupdate y el admin puede
        editarlas en su base (p. ej. marcar 'Gestionada por PCM' + bucket).
        """
        for xmlid in ("backup_policy_none", "backup_policy_basic",
                      "backup_policy_standard", "backup_policy_critical"):
            policy = self.env.ref("primate_cloud_manager.%s" % xmlid)
            self.assertTrue(policy.exists())
            self.assertTrue(policy.name)

    def test_policy_managed_requiere_bucket(self):
        with self.assertRaises(ValidationError):
            self.env["primate.cloud.backup.policy"].create({
                "name": "Gestionada sin bucket", "managed_by_pcm": True,
            })

    def test_policy_retencion_negativa(self):
        with self.assertRaises(ValidationError):
            self.env["primate.cloud.backup.policy"].create({
                "name": "Negativa", "expected_retention_days": -1,
            })

    def test_environment_compliance_default(self):
        """Sin política asignada, el entorno queda 'Sin política definida'."""
        self.assertFalse(self.env_a.backup_policy_id)
        self.assertEqual(self.env_a.backup_compliance, "no_policy")

    # ------------------------------------------------------------------
    # Registro de backups (inmutable estilo bitácora)
    # ------------------------------------------------------------------
    def _create_backup(self):
        return self.env["primate.cloud.backup"].create({
            "name": "Backup forum 2026-07-01",
            "environment_id": self.env_a.id,
        })

    def test_backup_cierre_de_ciclo_permitido(self):
        backup = self._create_backup()
        self.assertEqual(backup.state, "in_progress")
        backup.write({"state": "completed", "size_mb": 128.5,
                      "s3_key": "pcm-backups/forum/x.dump"})
        self.assertEqual(backup.state, "completed")

    def test_backup_edicion_manual_bloqueada(self):
        backup = self._create_backup()
        with self.assertRaises(UserError):
            backup.write({"name": "editado"})
        with self.assertRaises(UserError):
            backup.write({"environment_id": self.env_b.id})

    def test_backup_write_mixto_falla_entero(self):
        """Un write que mezcla campo permitido + prohibido falla ENTERO.

        El bypass clásico de una whitelist sería filtrar en silencio los
        prohibidos y aplicar los permitidos: acá no se aplica NADA.
        """
        backup = self._create_backup()
        with self.assertRaises(UserError):
            backup.write({"state": "completed", "environment_id": self.env_b.id})
        self.assertEqual(backup.state, "in_progress")
        self.assertEqual(backup.environment_id, self.env_a)

    def test_backup_unlink_bloqueado(self):
        backup = self._create_backup()
        with self.assertRaises(UserError):
            backup.unlink()

    # ------------------------------------------------------------------
    # Coherencia entorno↔instancia en database
    # ------------------------------------------------------------------
    def _create_database(self, **vals):
        base_vals = {
            "name": "forum", "account_id": self.account.id, "db_type": "local_pg",
        }
        base_vals.update(vals)
        return self.env["primate.cloud.database"].create(base_vals)

    def test_constraint_mismo_entorno_ok(self):
        database = self._create_database(
            environment_id=self.env_a.id, ec2_instance_id=self.instance_a.id
        )
        self.assertEqual(database.ec2_instance_id, self.instance_a)

    def test_constraint_cruce_de_entornos_falla(self):
        with self.assertRaises(ValidationError):
            self._create_database(
                environment_id=self.env_a.id, ec2_instance_id=self.instance_b.id
            )

    def test_constraint_no_aplica_si_falta_un_entorno(self):
        # Instancia sin entorno: vale para cualquier base.
        loose_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "suelta", "account_id": self.account.id,
            "aws_instance_id": "i-loose", "instance_state": "running",
            "region": "us-east-1",
        })
        database = self._create_database(
            environment_id=self.env_a.id, ec2_instance_id=loose_instance.id
        )
        self.assertTrue(database)
        # Base sin entorno: vale apuntar a cualquier instancia.
        other = self._create_database(name="sinenv", ec2_instance_id=self.instance_b.id)
        self.assertTrue(other)

    def test_assign_instance_valida_y_adopta_entorno(self):
        database = self._create_database()  # sin entorno (suelta)
        database.action_assign_instance(self.instance_a.id)
        self.assertEqual(database.ec2_instance_id, self.instance_a)
        # Adopta el entorno de la instancia al no tener uno propio.
        self.assertEqual(database.environment_id, self.env_a)

    def test_assign_instance_cruzada_falla(self):
        database = self._create_database(environment_id=self.env_a.id)
        with self.assertRaises(ValidationError):
            database.action_assign_instance(self.instance_b.id)

    def test_assign_instance_inexistente(self):
        database = self._create_database()
        with self.assertRaises(UserError):
            database.action_assign_instance(-1)

    # ------------------------------------------------------------------
    # Bitácora: action_types nuevos
    # ------------------------------------------------------------------
    def test_action_types_fase8(self):
        keys = {key for key, _label in ACTION_TYPES}
        for expected in ("backup_run", "backup_restore", "backup_check",
                         "staging_refresh"):
            self.assertIn(expected, keys)

    def test_log_backup_run(self):
        entry = self.env["primate.cloud.operation.log"].log_operation(
            "backup_run", name="Backup: forum", record=self.env_a
        )
        self.assertEqual(entry.action_type, "backup_run")
        self.assertEqual(entry.resource_model, "primate.cloud.environment")
