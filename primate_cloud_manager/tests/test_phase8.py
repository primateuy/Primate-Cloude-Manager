# -*- coding: utf-8 -*-
"""Tests de Fase 8 — Bloques 1 y 2: cimientos de respaldos y validador.

Bloque 1: modelos backup.policy y backup, campos de cumplimiento en
environment, constraint de coherencia entorno↔instancia y action_types.
Bloque 2: extensiones aws_rds/aws_s3 (mockeadas), regla pura de evaluación
de cumplimiento, job de verificación y cron. Sin AWS real.
"""
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from datetime import datetime

from ..models.primate_cloud_operation_log import ACTION_TYPES
from ..services import aws_rds, aws_s3


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


@tagged("post_install", "-at_install", "primate_cloud")
class TestAwsServicesPhase8(TransactionCase):
    """Extensiones de aws_rds y aws_s3 para el validador (boto3 mockeado)."""

    def _base_with_client(self):
        base = mock.Mock()
        return base, base.get_client.return_value

    def test_rds_normaliza_latest_restorable_time(self):
        base, client = self._base_with_client()
        client.describe_db_instances.return_value = {"DBInstances": [{
            "DBInstanceIdentifier": "forum-db", "Engine": "postgres",
            "BackupRetentionPeriod": 7, "DBInstanceStatus": "available",
            "LatestRestorableTime": "2026-07-01T03:00:00Z",
        }]}
        data = aws_rds.AwsRdsService(base).get_instance("forum-db")
        self.assertEqual(data["backup_retention_days"], 7)
        self.assertEqual(data["latest_restorable_time"], "2026-07-01T03:00:00Z")

    def test_rds_list_snapshots(self):
        base, client = self._base_with_client()
        client.get_paginator.return_value.paginate.return_value = [{
            "DBSnapshots": [{
                "DBSnapshotIdentifier": "rds:forum-db-2026-07-01",
                "DBInstanceIdentifier": "forum-db",
                "SnapshotType": "automated", "Status": "available",
                "SnapshotCreateTime": "2026-07-01T03:00:00Z",
                "AllocatedStorage": 50,
            }]
        }]
        snapshots = aws_rds.AwsRdsService(base).list_snapshots(identifier="forum-db")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_id"], "rds:forum-db-2026-07-01")
        self.assertEqual(snapshots[0]["snapshot_type"], "automated")
        client.get_paginator.return_value.paginate.assert_called_once_with(
            DBInstanceIdentifier="forum-db"
        )

    def test_s3_list_objects_con_prefijo(self):
        base, client = self._base_with_client()
        client.get_paginator.return_value.paginate.return_value = [{
            "Contents": [
                {"Key": "pcm-backups/forum/a.dump", "Size": 100},
                {"Key": "pcm-backups/forum/b.dump", "Size": 200},
            ]
        }]
        objects = aws_s3.AwsS3Service(base).list_objects(
            "bucket", prefix="pcm-backups/forum/"
        )
        self.assertEqual([o["key"] for o in objects],
                         ["pcm-backups/forum/a.dump", "pcm-backups/forum/b.dump"])
        client.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="bucket", Prefix="pcm-backups/forum/"
        )

    def test_s3_head_object_404_devuelve_none(self):
        base, client = self._base_with_client()

        class FakeClientError(Exception):
            def __init__(self, code):
                self.response = {"ResponseMetadata": {"HTTPStatusCode": code}}

        client.exceptions.ClientError = FakeClientError
        client.head_object.side_effect = FakeClientError(404)
        self.assertIsNone(aws_s3.AwsS3Service(base).head_object("bucket", "no-existe"))
        # Otro código de error NO se traga: se propaga.
        client.head_object.side_effect = FakeClientError(403)
        with self.assertRaises(FakeClientError):
            aws_s3.AwsS3Service(base).head_object("bucket", "prohibido")


@tagged("post_install", "-at_install", "primate_cloud")
class TestBackupCompliance(TransactionCase):
    """Regla de evaluación, job de verificación y cron (Bloque 2)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.environment = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        # Política propia con valores explícitos (NO usar seeds: son noupdate
        # y el admin puede editarlas en su base).
        self.policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Diaria 7d (test)", "policy_type": "custom",
            "expected_frequency": "daily", "expected_retention_days": 7,
            "managed_by_pcm": True, "s3_bucket": "pcm-test-bucket",
        })
        self.environment.backup_policy_id = self.policy
        self.Env = self.env["primate.cloud.environment"]

    def _evidence(self, **overrides):
        entry = {"name": "forum", "db_type": "local_pg",
                 "last_backup": fields.Datetime.now() - timedelta(hours=2),
                 "retention_days": 7, "error": None,
                 "as_of": fields.Datetime.now()}
        entry.update(overrides)
        return [entry]

    # --- Regla pura ---
    def test_evaluate_sin_politica(self):
        state, _detail = self.Env._evaluate_backup_compliance(
            self.env["primate.cloud.backup.policy"], []
        )
        self.assertEqual(state, "no_policy")

    def test_evaluate_politica_none_es_ok(self):
        policy = self.env["primate.cloud.backup.policy"].create(
            {"name": "Nada (test)", "policy_type": "none"}
        )
        state, _detail = self.Env._evaluate_backup_compliance(policy, [])
        self.assertEqual(state, "ok")

    def test_evaluate_sin_bases_no_verificable(self):
        state, _detail = self.Env._evaluate_backup_compliance(self.policy, [])
        self.assertEqual(state, "unverifiable")

    def test_evaluate_cumple(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence()
        )
        self.assertEqual(state, "ok")
        self.assertIn("cumple", detail)

    def test_evaluate_backup_viejo_no_cumple(self):
        evidence = self._evidence(
            last_backup=fields.Datetime.now() - timedelta(hours=30)
        )
        state, detail = self.Env._evaluate_backup_compliance(self.policy, evidence)
        self.assertEqual(state, "non_compliant")
        self.assertIn("fuera de la ventana", detail)

    def test_evaluate_sin_backups_no_cumple(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence(last_backup=False)
        )
        self.assertEqual(state, "non_compliant")
        self.assertIn("sin backups registrados", detail)

    def test_evaluate_retencion_menor_no_cumple(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence(retention_days=3)
        )
        self.assertEqual(state, "non_compliant")
        self.assertIn("retención 3 < 7", detail)

    def test_evaluate_retencion_desconocida_no_verificable(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence(retention_days=None)
        )
        self.assertEqual(state, "unverifiable")
        self.assertIn("retención no verificable", detail)

    def test_evaluate_evidencia_vieja_no_verificable(self):
        """Evidencia recolectada hace >24 h: NUNCA 'Cumple' sobre datos viejos,
        aunque el backup y la retención se vean perfectos."""
        evidence = self._evidence(as_of=fields.Datetime.now() - timedelta(hours=30))
        state, detail = self.Env._evaluate_backup_compliance(self.policy, evidence)
        self.assertEqual(state, "unverifiable")
        self.assertIn("más vieja que 24 h", detail)

    def test_evaluate_evidencia_sin_marca_temporal_no_verificable(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence(as_of=None)
        )
        self.assertEqual(state, "unverifiable")
        self.assertIn("sin marca temporal", detail)

    def test_evaluate_evidencia_vieja_gana_a_cumple_en_mixto(self):
        evidence = self._evidence() + self._evidence(
            name="otra", as_of=fields.Datetime.now() - timedelta(days=2)
        )
        state, _detail = self.Env._evaluate_backup_compliance(self.policy, evidence)
        self.assertEqual(state, "unverifiable")

    def test_evaluate_error_no_verificable(self):
        state, detail = self.Env._evaluate_backup_compliance(
            self.policy, self._evidence(error="AccessDenied")
        )
        self.assertEqual(state, "unverifiable")
        self.assertIn("AccessDenied", detail)

    def test_evaluate_prioridad_no_cumple_gana(self):
        evidence = self._evidence() + self._evidence(error="timeout") + \
            self._evidence(retention_days=1)
        state, _detail = self.Env._evaluate_backup_compliance(self.policy, evidence)
        self.assertEqual(state, "non_compliant")

    def test_evaluate_frecuencia_manual_solo_retencion(self):
        policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Manual (test)", "policy_type": "custom",
            "expected_frequency": "manual", "expected_retention_days": 7,
        })
        # Sin backups pero con retención suficiente: manual no evalúa ventana.
        state, _detail = self.Env._evaluate_backup_compliance(
            policy, self._evidence(last_backup=False)
        )
        self.assertEqual(state, "ok")

    # --- Evidencia y job (sin AWS: base local + registro PCM) ---
    def _create_local_db(self, name="forum"):
        return self.env["primate.cloud.database"].create({
            "name": name, "account_id": self.account.id,
            "environment_id": self.environment.id, "db_type": "local_pg",
        })

    def _register_backup(self, database, hours_ago=2, state="completed"):
        return self.env["primate.cloud.backup"].create({
            "name": "Backup %s" % database.name,
            "environment_id": self.environment.id,
            "database_id": database.id,
            "backup_date": fields.Datetime.now() - timedelta(hours=hours_ago),
            "state": state,
        })

    def test_job_local_con_backup_fresco_cumple(self):
        database = self._create_local_db()
        self._register_backup(database, hours_ago=2)
        state = self.environment.job_check_backup_compliance()
        self.assertEqual(state, "ok")
        self.assertEqual(self.environment.backup_compliance, "ok")
        self.assertTrue(self.environment.last_backup_check)
        # La evidencia refresca el último backup de la base.
        self.assertTrue(database.last_backup_date)

    def test_job_local_backup_viejo_no_cumple_y_loguea(self):
        database = self._create_local_db()
        self._register_backup(database, hours_ago=48)
        state = self.environment.job_check_backup_compliance()
        self.assertEqual(state, "non_compliant")
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_check"),
             ("resource_id", "=", self.environment.id)],
            order="id desc", limit=1,
        )
        self.assertEqual(log.result, "failed")

    def test_job_solo_loguea_transiciones(self):
        database = self._create_local_db()
        self._register_backup(database, hours_ago=2)
        Log = self.env["primate.cloud.operation.log"]
        domain = [("action_type", "=", "backup_check"),
                  ("resource_id", "=", self.environment.id)]
        self.environment.job_check_backup_compliance()  # no_policy -> ok: loguea
        count_first = Log.search_count(domain)
        self.assertEqual(count_first, 1)
        self.environment.job_check_backup_compliance()  # ok -> ok: silencio
        self.assertEqual(Log.search_count(domain), count_first)

    def test_job_backup_fallido_no_cuenta(self):
        database = self._create_local_db()
        self._register_backup(database, hours_ago=2, state="failed")
        state = self.environment.job_check_backup_compliance()
        self.assertEqual(state, "non_compliant")

    def test_job_rds_con_error_no_verificable(self):
        self.env["primate.cloud.database"].create({
            "name": "forum-rds", "account_id": self.account.id,
            "environment_id": self.environment.id, "db_type": "rds",
            "rds_identifier": "forum-rds",
        })
        with mock.patch.object(
            aws_rds.AwsRdsService, "get_instance",
            side_effect=Exception("AccessDenied"),
        ):
            state = self.environment.job_check_backup_compliance()
        self.assertEqual(state, "unverifiable")
        self.assertIn("AccessDenied", self.environment.backup_compliance_detail)

    def test_job_rds_actualiza_base(self):
        database = self.env["primate.cloud.database"].create({
            "name": "forum-rds", "account_id": self.account.id,
            "environment_id": self.environment.id, "db_type": "rds",
            "rds_identifier": "forum-rds",
        })
        fresh = fields.Datetime.now() - timedelta(hours=1)
        with mock.patch.object(
            aws_rds.AwsRdsService, "get_instance",
            return_value={"backup_retention_days": 14,
                          "latest_restorable_time": fresh},
        ):
            state = self.environment.job_check_backup_compliance()
        self.assertEqual(state, "ok")
        self.assertEqual(database.backup_retention_days, 14)
        self.assertEqual(database.last_backup_date, fresh)

    # --- Cron y botón ---
    def test_cron_encola_y_marca_sin_politica(self):
        other = self.env["primate.cloud.environment"].create({
            "name": "Sin Política", "project_id": self.project.id,
            "env_type": "testing", "state": "active",
            "backup_compliance": "ok",  # simular estado previo
        })
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_check_backup_compliance()
            with_delay.assert_called_once()
        self.assertEqual(other.backup_compliance, "no_policy")
        self.assertTrue(other.last_backup_check)

    def test_action_check_requiere_politica(self):
        other = self.env["primate.cloud.environment"].create({
            "name": "Sin Política 2", "project_id": self.project.id,
            "env_type": "testing", "state": "active",
        })
        with self.assertRaises(UserError):
            other.action_check_backup_compliance()

    def test_action_check_encola(self):
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.environment.action_check_backup_compliance()
            with_delay.assert_called_once()


@tagged("post_install", "-at_install", "primate_cloud")
class TestManagedBackups(TransactionCase):
    """Ejecución de backups gestionados (Bloque 3): ventanas, script y job."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum Uy", "account_id": self.account.id}
        )
        self.environment = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Gestionada (test)", "policy_type": "custom",
            "expected_frequency": "daily", "expected_retention_days": 7,
            "managed_by_pcm": True, "s3_bucket": "pcm-backups-test",
            "execution_hour": 3.0,
        })
        self.environment.backup_policy_id = self.policy
        self.instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-srv", "account_id": self.account.id,
            "environment_id": self.environment.id, "aws_instance_id": "i-prod",
            "instance_state": "running", "region": "us-east-1",
        })
        self.database = self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.environment.id, "db_type": "local_pg",
            "ec2_instance_id": self.instance.id,
        })
        self.Env = self.env["primate.cloud.environment"]

    # --- Ventanas de ejecución (regla pura, now inyectable) ---
    def test_window_daily_despues_de_la_hora(self):
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 10, 0)
        )
        self.assertEqual(start, datetime(2026, 7, 1, 3, 0))

    def test_window_daily_antes_de_la_hora(self):
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 2, 0)
        )
        self.assertEqual(start, datetime(2026, 6, 30, 3, 0))

    def test_window_twice_daily(self):
        self.policy.expected_frequency = "twice_daily"
        # 16:00: la ventana vigente es la de las 15:00 (3 + 12).
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 16, 0)
        )
        self.assertEqual(start, datetime(2026, 7, 1, 15, 0))
        # 10:00: la vigente es la de las 03:00.
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 10, 0)
        )
        self.assertEqual(start, datetime(2026, 7, 1, 3, 0))

    def test_window_hourly_y_manual(self):
        self.policy.expected_frequency = "hourly"
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 10, 42)
        )
        self.assertEqual(start, datetime(2026, 7, 1, 10, 0))
        self.policy.expected_frequency = "manual"
        self.assertFalse(self.Env._backup_window_start(self.policy))

    def test_window_media_hora(self):
        self.policy.execution_hour = 3.5
        start = self.Env._backup_window_start(
            self.policy, now=datetime(2026, 7, 1, 10, 0)
        )
        self.assertEqual(start, datetime(2026, 7, 1, 3, 30))

    # --- Script de backup (decisiones del Bloque 3) ---
    def test_script_streaming_sin_credenciales(self):
        script = self.Env._build_backup_script(
            "forum", "bucket", "p/forum/x.dump", "p/forum/x-filestore.tar.gz"
        )
        self.assertIn("set -euo pipefail", script)
        # Guarda de espacio + limpieza siempre.
        self.assertIn("df -Pm /tmp", script)
        self.assertIn("trap 'rm -f /tmp/pcm_backup_*", script)
        # Streaming: pg_dump entubado a aws s3 cp, sin archivo intermedio.
        self.assertIn("pg_dump -Fc -d forum | aws s3 cp - s3://bucket/p/forum/x.dump",
                      script)
        # Peer auth: sin contraseñas ni connection strings.
        self.assertIn("sudo -u postgres", script)
        for forbidden in ("PGPASSWORD", "password", "postgresql://"):
            self.assertNotIn(forbidden, script)
        self.assertIn("PCM_BACKUP_OK", script)

    def test_parse_backup_sizes(self):
        stdout = "PCM_DUMP_SIZE_BYTES=1048576\nruido\nPCM_FS_SIZE_BYTES=2097152\nPCM_BACKUP_OK"
        sizes = self.Env._parse_backup_sizes(stdout)
        self.assertEqual(sizes, {"PCM_DUMP_SIZE_BYTES": 1048576,
                                 "PCM_FS_SIZE_BYTES": 2097152})

    def test_backup_slug(self):
        self.assertEqual(self.Env._backup_slug("Demo PCM (Ürgente)"), "Demo-PCM-rgente-")
        self.assertEqual(self.Env._backup_slug(""), "sin-nombre")

    # --- Job (SSM y S3 mockeados) ---
    def _mock_s3(self):
        return mock.patch.multiple(
            aws_s3.AwsS3Service,
            ensure_bucket=mock.DEFAULT, put_lifecycle_rule=mock.DEFAULT,
        )

    def _run_job(self, ssm_output=None, ssm_error=None):
        ssm = mock.Mock()
        if ssm_error:
            ssm.run_script.side_effect = ssm_error
        else:
            ssm.run_script.return_value = ssm_output
        with self._mock_s3(), mock.patch.object(
            type(self.instance), "_get_ssm_service", return_value=ssm,
        ):
            result = self.environment.job_run_backup()
        return result, ssm

    def test_job_exitoso(self):
        output = {"status": "Success",
                  "stdout": "PCM_DUMP_SIZE_BYTES=1048576\n"
                            "PCM_FS_SIZE_BYTES=1048576\nPCM_BACKUP_OK"}
        result, ssm = self._run_job(ssm_output=output)
        self.assertTrue(result)
        ssm.run_script.assert_called_once()
        backup = self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        )
        self.assertEqual(backup.state, "completed")
        self.assertEqual(backup.size_mb, 2.0)
        self.assertTrue(backup.expiry_date)
        self.assertIn("Forum-Uy/Forum-Prod/forum/", backup.s3_key)
        self.assertTrue(backup.s3_key.endswith(".dump"))
        self.assertTrue(self.database.last_backup_date)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_run"),
             ("resource_id", "=", self.environment.id)], limit=1,
        )
        self.assertEqual(log.result, "success")

    def test_job_instancia_detenida_no_toca_ssm(self):
        self.instance.instance_state = "stopped"
        result, ssm = self._run_job(ssm_output={"status": "Success", "stdout": ""})
        self.assertFalse(result)
        ssm.run_script.assert_not_called()
        backup = self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        )
        self.assertEqual(backup.state, "failed")
        self.assertIn("detenida", backup.error_message)

    def test_job_base_sin_instancia(self):
        self.database.ec2_instance_id = False
        result, ssm = self._run_job(ssm_output={"status": "Success", "stdout": ""})
        self.assertFalse(result)
        ssm.run_script.assert_not_called()
        backup = self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        )
        self.assertEqual(backup.state, "failed")

    def test_job_ssm_inaccesible(self):
        result, _ssm = self._run_job(ssm_error=Exception("InvalidInstanceId"))
        self.assertFalse(result)
        backup = self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        )
        self.assertEqual(backup.state, "failed")
        self.assertIn("InvalidInstanceId", backup.error_message)

    def test_job_script_fallido(self):
        output = {"status": "Failed", "stdout": "", "stderr": "sin espacio"}
        result, _ssm = self._run_job(ssm_output=output)
        self.assertFalse(result)
        backup = self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        )
        self.assertEqual(backup.state, "failed")
        self.assertIn("sin espacio", backup.error_message)

    def test_job_sin_bases_locales_ok(self):
        self.database.unlink()
        with self._mock_s3():
            result = self.environment.job_run_backup()
        self.assertTrue(result)
        self.assertFalse(self.env["primate.cloud.backup"].search(
            [("environment_id", "=", self.environment.id)]
        ))

    def test_job_sin_politica_gestionada(self):
        self.policy.managed_by_pcm = False
        with self.assertRaises(UserError):
            self.environment.job_run_backup()

    # --- Cron programador ---
    def test_cron_encola_si_ventana_descubierta(self):
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_run_managed_backups()
            with_delay.assert_called_once()

    def test_cron_no_encola_si_ventana_cubierta(self):
        self.env["primate.cloud.backup"].create({
            "name": "reciente", "environment_id": self.environment.id,
            "database_id": self.database.id, "backup_type": "pcm_dump",
            "state": "completed", "backup_date": fields.Datetime.now(),
        })
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_run_managed_backups()
            with_delay.assert_not_called()

    def test_cron_ignora_entornos_no_activos(self):
        self.environment.state = "error"
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_run_managed_backups()
            with_delay.assert_not_called()

    # --- In_progress zombi ---
    def test_mark_stuck_failed_umbral(self):
        """Un in_progress de 3 h se marca failed; uno de 30 min sobrevive."""
        Backup = self.env["primate.cloud.backup"]
        now = fields.Datetime.now()
        zombie = Backup.create({
            "name": "zombi", "environment_id": self.environment.id,
            "backup_type": "pcm_dump",
            "backup_date": now - timedelta(hours=3),
        })
        fresh = Backup.create({
            "name": "corriendo", "environment_id": self.environment.id,
            "backup_type": "pcm_dump",
            "backup_date": now - timedelta(minutes=30),
        })
        stuck = Backup._mark_stuck_failed(now=now)
        self.assertEqual(stuck, zombie)
        self.assertEqual(zombie.state, "failed")
        self.assertIn("Interrumpido", zombie.error_message)
        self.assertEqual(fresh.state, "in_progress")

    def test_cron_zombi_redescubre_la_ventana(self):
        """Un in_progress zombi ya no cuenta como ventana cubierta: el cron lo
        marca failed y vuelve a encolar el backup."""
        self.env["primate.cloud.backup"].create({
            "name": "zombi", "environment_id": self.environment.id,
            "database_id": self.database.id, "backup_type": "pcm_dump",
            "backup_date": fields.Datetime.now() - timedelta(hours=3),
        })
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_run_managed_backups()
            with_delay.assert_called_once()

    def test_cron_in_progress_fresco_sigue_cubriendo(self):
        # backup_date = ahora: siempre dentro de la ventana vigente, sin
        # depender de la hora a la que corra la suite.
        self.env["primate.cloud.backup"].create({
            "name": "corriendo", "environment_id": self.environment.id,
            "database_id": self.database.id, "backup_type": "pcm_dump",
            "backup_date": fields.Datetime.now(),
        })
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.Env._cron_run_managed_backups()
            with_delay.assert_not_called()

    # --- Expiración ---
    def test_cron_mark_expired(self):
        Backup = self.env["primate.cloud.backup"]
        old = Backup.create({
            "name": "viejo", "environment_id": self.environment.id,
            "state": "completed",
            "backup_date": fields.Datetime.now() - timedelta(days=10),
            "expiry_date": fields.Datetime.now() - timedelta(days=3),
        })
        fresh = Backup.create({
            "name": "fresco", "environment_id": self.environment.id,
            "state": "completed", "backup_date": fields.Datetime.now(),
            "expiry_date": fields.Datetime.now() + timedelta(days=7),
        })
        Backup._cron_mark_expired()
        self.assertEqual(old.state, "expired")
        self.assertEqual(fresh.state, "completed")

    # --- Botón ---
    def test_action_run_backup_requiere_gestionada(self):
        self.policy.managed_by_pcm = False
        with self.assertRaises(UserError):
            self.environment.action_run_backup()

    def test_action_run_backup_encola(self):
        with mock.patch.object(type(self.environment), "with_delay") as with_delay:
            self.environment.action_run_backup()
            with_delay.assert_called_once()
