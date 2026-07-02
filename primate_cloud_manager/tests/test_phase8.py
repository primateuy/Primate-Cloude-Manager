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


@tagged("post_install", "-at_install", "primate_cloud")
class TestBackupRestore(TransactionCase):
    """Restore (Bloque 4): wizard con salvaguardas, script y job."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.prod = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.staging = self.env["primate.cloud.environment"].create({
            "name": "Forum Staging", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
            "main_url": "staging.forum.primate.cloud",
        })
        self.prod_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-srv", "account_id": self.account.id,
            "environment_id": self.prod.id, "aws_instance_id": "i-prod",
            "instance_state": "running", "region": "us-east-1",
        })
        self.staging_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "stg-srv", "account_id": self.account.id,
            "environment_id": self.staging.id, "aws_instance_id": "i-stg",
            "instance_state": "running", "region": "us-east-1",
        })
        self.prod_db = self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.prod.id, "db_type": "local_pg",
            "ec2_instance_id": self.prod_instance.id,
        })
        self.staging_db = self.env["primate.cloud.database"].create({
            "name": "forum_stg", "account_id": self.account.id,
            "environment_id": self.staging.id, "db_type": "local_pg",
            "ec2_instance_id": self.staging_instance.id,
        })
        self.backup = self.env["primate.cloud.backup"].create({
            "name": "Backup forum 20260701", "environment_id": self.prod.id,
            "database_id": self.prod_db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-backups-test",
            "s3_key": "pcm-backups/Forum/Forum-Prod/forum/20260701.dump",
            "s3_filestore_key":
                "pcm-backups/Forum/Forum-Prod/forum/20260701-filestore.tar.gz",
        })
        self.backup.write({"state": "completed", "size_mb": 100.0})
        self.Wizard = self.env["primate.cloud.backup.restore.wizard"]

    def _wizard(self, **vals):
        base = {"backup_id": self.backup.id,
                "target_environment_id": self.staging.id,
                "target_instance_id": self.staging_instance.id,
                "target_db_name": "forum_stg"}
        base.update(vals)
        return self.Wizard.create(base)

    # --- Wizard: defaults y salvaguardas ---
    def test_default_no_propone_produccion(self):
        """Backup de producción: el destino queda VACÍO."""
        defaults = self.Wizard.with_context(
            default_backup_id=self.backup.id
        ).default_get(["backup_id", "target_environment_id", "target_db_name"])
        self.assertFalse(defaults.get("target_environment_id"))
        self.assertEqual(defaults.get("target_db_name"), "forum")

    def test_default_origen_no_prod_se_propone(self):
        staging_backup = self.env["primate.cloud.backup"].create({
            "name": "Backup stg", "environment_id": self.staging.id,
            "database_id": self.staging_db.id, "backup_type": "pcm_dump",
        })
        staging_backup.write({"state": "completed"})
        defaults = self.Wizard.with_context(
            default_backup_id=staging_backup.id
        ).default_get(["backup_id", "target_environment_id"])
        self.assertEqual(defaults.get("target_environment_id"), self.staging.id)

    def test_prod_exige_nombre_exacto(self):
        wizard = self._wizard(target_environment_id=self.prod.id,
                              target_instance_id=self.prod_instance.id,
                              target_db_name="forum",
                              confirm_environment_name="forum prod")  # mal
        with self.assertRaises(UserError):
            wizard.action_restore()
        wizard.confirm_environment_name = "Forum Prod"
        with mock.patch.object(type(self.prod), "with_delay") as with_delay:
            wizard.action_restore()
            with_delay.assert_called_once()

    def test_nombre_db_invalido(self):
        wizard = self._wizard(target_db_name="forum; drop database x")
        with self.assertRaises(UserError):
            wizard.action_restore()

    def test_instancia_de_otro_entorno(self):
        wizard = self._wizard(target_instance_id=self.prod_instance.id)
        with self.assertRaises(UserError):
            wizard.action_restore()

    def test_pre_restore_es_origen_valido(self):
        """Nada filtra por tipo/nombre: un 'Pre-restore …' se puede restaurar."""
        pre = self.env["primate.cloud.backup"].create({
            "name": "Pre-restore forum_stg 20260702",
            "environment_id": self.staging.id,
            "database_id": self.staging_db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-backups-test",
            "s3_key": "pre-restore/Forum/Forum-Staging/forum_stg/x.dump",
        })
        pre.write({"state": "completed"})
        action = pre.action_restore()
        self.assertEqual(action["res_model"],
                         "primate.cloud.backup.restore.wizard")
        wizard = self._wizard(backup_id=pre.id)
        with mock.patch.object(type(self.staging), "with_delay") as with_delay:
            wizard.action_restore()
            with_delay.assert_called_once()

    def test_action_restore_solo_completed(self):
        self.backup.write({"state": "expired"})
        with self.assertRaises(UserError):
            self.backup.action_restore()

    # --- Script (golden, orden aprobado) ---
    def test_script_orden_stop_terminate_drop(self):
        script = self.env["primate.cloud.environment"] \
            ._build_backup_restore_script("forum_stg", self.backup, True)
        stop = script.index("systemctl stop odoo")
        terminate = script.index("pg_terminate_backend")
        drop = script.index("dropdb --if-exists")
        self.assertLess(stop, terminate)
        self.assertLess(terminate, drop)
        # Validaciones ANTES de detener nada.
        self.assertLess(script.index("df -Pm /var/tmp"), stop)
        self.assertLess(script.index("PCM_ERROR_PG_MISMATCH"), stop)
        # Punto de no retorno marcado antes del drop, y después del terminate.
        drop_marker = script.index("PCM_DROP_STARTED")
        self.assertLess(terminate, drop_marker)
        self.assertLess(drop_marker, drop)
        # Limpieza siempre + sin credenciales.
        self.assertIn("trap 'rm -rf", script)
        for forbidden in ("PGPASSWORD", "password", "postgresql://"):
            self.assertNotIn(forbidden, script)
        # Filestore: se renombra del nombre de origen al de destino.
        self.assertIn("mv \"$FS_TMP\"/forum", script)
        self.assertIn("chown -R odoo:odoo", script)

    def test_script_start_vive_en_el_trap(self):
        """El start de Odoo corre en CUALQUIER salida post-stop (trap EXIT),
        no solo en el camino feliz: la EC2 puede hospedar más bases y un
        restore fallido no puede dejar el servicio abajo para todas."""
        script = self.env["primate.cloud.environment"] \
            ._build_backup_restore_script("forum_stg", self.backup, True)
        restart_trap = script.index("trap 'systemctl start odoo || true;")
        stop = script.index("systemctl stop odoo")
        drop = script.index("dropdb --if-exists")
        # El trap se re-arma inmediatamente después del stop y antes del drop.
        self.assertLess(stop, restart_trap)
        self.assertLess(restart_trap, drop)
        # Fuera del trap NO hay otro start (el camino feliz también sale por él).
        self.assertEqual(script.count("systemctl start odoo"), 1)

    def test_resolve_bucket_fallback_a_politica(self):
        """Registro viejo sin s3_bucket: se resuelve por la política del
        entorno de origen (decisión explícita, no accidente)."""
        policy = self.env["primate.cloud.backup.policy"].create({
            "name": "Con bucket (test)", "policy_type": "custom",
            "managed_by_pcm": True, "s3_bucket": "bucket-politica",
        })
        self.prod.backup_policy_id = policy
        legacy = self.env["primate.cloud.backup"].create({
            "name": "viejo sin bucket", "environment_id": self.prod.id,
            "database_id": self.prod_db.id, "backup_type": "pcm_dump",
            "s3_key": "pcm-backups/x.dump",
        })
        legacy.write({"state": "completed"})
        self.assertEqual(legacy._resolve_bucket(), "bucket-politica")
        # Con campo propio, el campo gana.
        self.assertEqual(self.backup._resolve_bucket(), "pcm-backups-test")

    def test_job_sin_bucket_resoluble_falla_claro(self):
        self.prod.backup_policy_id = False
        legacy = self.env["primate.cloud.backup"].create({
            "name": "irresoluble", "environment_id": self.prod.id,
            "backup_type": "pcm_dump", "s3_key": "x.dump",
        })
        legacy.write({"state": "completed"})
        self.assertFalse(legacy._resolve_bucket())
        ssm = mock.Mock()
        with mock.patch.object(type(self.staging_instance),
                               "_get_ssm_service", return_value=ssm):
            result = self.staging.job_restore_backup({
                "backup_id": legacy.id, "db_name": "forum_stg",
                "instance_id": self.staging_instance.id, "pre_backup": False,
            })
        self.assertFalse(result)
        ssm.run_script.assert_not_called()
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertIn("bucket", log.error_message)

    def test_script_sin_filestore(self):
        script = self.env["primate.cloud.environment"] \
            ._build_backup_restore_script("forum_stg", self.backup, False)
        self.assertNotIn("tar -xzf", script)
        self.assertIn("PCM_RESTORE_OK", script)

    # --- Job ---
    def _run_restore(self, target=None, ssm_output=None, pre_backup=True,
                     head=True, pre_state="completed"):
        target = target or self.staging
        params = {"backup_id": self.backup.id,
                  "db_name": "forum_stg" if target == self.staging else "forum",
                  "instance_id": (self.staging_instance
                                  if target == self.staging
                                  else self.prod_instance).id,
                  "pre_backup": pre_backup}
        ssm = mock.Mock()
        ssm.run_script.return_value = ssm_output or {
            "status": "Success", "stdout": "PCM_DROP_STARTED\nPCM_RESTORE_OK",
        }
        pre_record = self.env["primate.cloud.backup"].create({
            "name": "Pre-restore x", "environment_id": target.id,
            "backup_type": "pcm_dump", "s3_bucket": "pcm-backups-test",
            "s3_key": "pre-restore/x.dump",
        })
        pre_record.write({"state": pre_state})
        head_result = {"key": "x", "size": 1} if head else None
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value=head_result), \
                mock.patch.object(type(target), "_run_database_backup",
                                  return_value=pre_record) as pre_mock, \
                mock.patch.object(type(target), "_staging_neutralize") \
                as neutralize, \
                mock.patch.object(
                    type(self.staging_instance), "_get_ssm_service",
                    return_value=ssm):
            result = target.job_restore_backup(params)
        return result, ssm, neutralize, pre_mock, pre_record

    def test_job_exitoso_neutraliza_staging(self):
        result, ssm, neutralize, _pre, _rec = self._run_restore()
        self.assertTrue(result)
        ssm.run_script.assert_called_once()
        neutralize.assert_called_once()
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertEqual(log.result, "success")

    def test_job_prod_no_neutraliza(self):
        result, _ssm, neutralize, _pre, _rec = self._run_restore(
            target=self.prod)
        self.assertTrue(result)
        neutralize.assert_not_called()

    def test_job_post_drop_muestra_pre_backup(self):
        """Fracaso post-drop: la key del pre-backup queda a la vista."""
        output = {"status": "Failed",
                  "stdout": "PCM_DROP_STARTED", "stderr": "disco lleno"}
        result, _ssm, _n, _pre, pre_record = self._run_restore(
            ssm_output=output)
        self.assertFalse(result)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertEqual(log.result, "failed")
        self.assertIn("restaurable con este pre-backup", log.error_message)
        self.assertIn(pre_record.s3_key, log.error_message)
        self.assertIn("disco lleno", log.error_message)

    def test_job_pre_drop_no_menciona_pre_backup(self):
        output = {"status": "Failed", "stdout": "",
                  "stderr": "PCM_ERROR: espacio insuficiente"}
        result, _ssm, _n, _pre, _rec = self._run_restore(ssm_output=output)
        self.assertFalse(result)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertNotIn("restaurable con este pre-backup",
                         log.error_message or "")

    def test_job_pg_mismatch_mensaje_claro(self):
        output = {"status": "Failed", "stdout": "",
                  "stderr": "PCM_ERROR_PG_MISMATCH origen=16 destino=14"}
        result, _ssm, _n, _pre, _rec = self._run_restore(ssm_output=output)
        self.assertFalse(result)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertIn("PostgreSQL incompatible", log.error_message)

    def test_job_dump_ausente_aborta_sin_tocar(self):
        result, ssm, _n, _pre, _rec = self._run_restore(head=False)
        self.assertFalse(result)
        ssm.run_script.assert_not_called()

    def test_job_pre_backup_fallido_aborta(self):
        result, ssm, _n, _pre, _rec = self._run_restore(pre_state="failed")
        self.assertFalse(result)
        ssm.run_script.assert_not_called()
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.staging.id)], limit=1,
        )
        self.assertIn("pre-backup", log.error_message)


@tagged("post_install", "-at_install", "primate_cloud")
class TestStagingInstanceAware(TransactionCase):
    """Staging consciente de instancias (Bloque 5): origen explícito, RDS,
    neutralización ampliada y wizards."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Forum", "account_id": self.account.id}
        )
        self.origin = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
            "main_url": "forum.primate.cloud",
        })
        self.instance_1 = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-1", "account_id": self.account.id,
            "environment_id": self.origin.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
        })
        self.db_1 = self.env["primate.cloud.database"].create({
            "name": "forum", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
            "ec2_instance_id": self.instance_1.id,
        })
        self.Env = self.env["primate.cloud.environment"]

    def _add_second_instance_and_db(self):
        instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-2", "account_id": self.account.id,
            "environment_id": self.origin.id, "aws_instance_id": "i-2",
            "instance_state": "running", "region": "us-east-1",
        })
        database = self.env["primate.cloud.database"].create({
            "name": "forum2", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "local_pg",
            "ec2_instance_id": instance.id,
        })
        return instance, database

    # --- Resolución explícita de origen ---
    def test_resolve_origen_unico_fallback(self):
        instance, database = self.origin._resolve_staging_origin()
        self.assertEqual(instance, self.instance_1)
        self.assertEqual(database, self.db_1)

    def test_resolve_origen_multi_exige_elegir(self):
        self._add_second_instance_and_db()
        with self.assertRaises(UserError):
            self.origin._resolve_staging_origin()

    def test_resolve_origen_explicito_gana(self):
        instance_2, db_2 = self._add_second_instance_and_db()
        instance, database = self.origin._resolve_staging_origin(
            instance_2, db_2)
        self.assertEqual(instance, instance_2)
        self.assertEqual(database, db_2)

    def test_enqueue_staging_persiste_origen(self):
        instance_2, db_2 = self._add_second_instance_and_db()
        with mock.patch.object(type(self.origin), "with_delay"):
            staging = self.origin._enqueue_staging({
                "name": "Stg", "domain": "stg.forum.primate.cloud",
                "origin_instance_id": instance_2.id,
                "origin_database_id": db_2.id,
            })
        self.assertEqual(staging.staging_origin_instance_id, instance_2)
        self.assertEqual(staging.staging_origin_database_id, db_2)

    def test_enqueue_staging_multi_sin_eleccion_falla(self):
        self._add_second_instance_and_db()
        with self.assertRaises(UserError):
            self.origin._enqueue_staging({
                "name": "Stg", "domain": "stg.forum.primate.cloud",
            })

    # --- Script de backup con origen RDS ---
    def test_script_rds_credenciales_in_situ(self):
        script = self.Env._build_backup_script(
            "forum", "bucket", "k.dump", "k-fs.tar.gz",
            rds_endpoint="forum.abc.us-east-1.rds.amazonaws.com",
        )
        # El dump apunta al endpoint con credenciales leídas del odoo.conf.
        self.assertIn("-h forum.abc.us-east-1.rds.amazonaws.com", script)
        self.assertIn("/etc/odoo/odoo.conf", script)
        self.assertIn('PGPASSWORD="$DB_PASSWORD" pg_dump', script)
        # Las credenciales NUNCA se imprimen (ninguna línea echo las toca).
        for line in script.splitlines():
            if "echo" in line:
                self.assertNotIn("DB_PASSWORD", line)
                self.assertNotIn("DB_USER", line)
        # Sigue siendo streaming.
        self.assertIn("| aws s3 cp - s3://bucket/k.dump", script)

    def test_script_local_sin_modo_rds(self):
        script = self.Env._build_backup_script("forum", "bucket", "k", "kf")
        self.assertIn("sudo -u postgres pg_dump", script)
        self.assertNotIn("PGPASSWORD", script)

    def test_run_database_backup_ejecutor_explicito(self):
        """Una BD RDS sin EC2 propia se dumpea desde el ejecutor indicado."""
        rds_db = self.env["primate.cloud.database"].create({
            "name": "forumrds", "account_id": self.account.id,
            "environment_id": self.origin.id, "db_type": "rds",
            "rds_identifier": "forumrds",
            "rds_endpoint": "forumrds.abc.rds.amazonaws.com",
        })
        ssm = mock.Mock()
        ssm.run_script.return_value = {
            "status": "Success",
            "stdout": "PCM_DUMP_SIZE_BYTES=10\nPCM_FS_SIZE_BYTES=0\nPCM_BACKUP_OK",
        }
        with mock.patch.object(type(self.instance_1), "_get_ssm_service",
                               return_value=ssm):
            record = self.origin._run_database_backup(
                rds_db, self.env["primate.cloud.backup.policy"],
                "bucket", "staging", "us-east-1", instance=self.instance_1,
            )
        self.assertEqual(record.state, "completed")
        script = ssm.run_script.call_args[0][1]
        self.assertIn("forumrds.abc.rds.amazonaws.com", script)

    # --- Neutralización ampliada ---
    def test_neutralizacion_ampliada(self):
        sql = self.Env._render_neutralization_sql("https://stg.forum")
        self.assertIn("UPDATE mail_mail SET state = 'cancel'", sql)
        self.assertIn("DELETE FROM res_users_apikeys", sql)
        self.assertIn("UPDATE website SET domain = 'https://stg.forum'", sql)
        # Lo de D5 sigue intacto.
        self.assertIn("UPDATE ir_cron SET active = false", sql)
        self.assertNotIn("mail.catchall.alias", sql)

    # --- Restore con neutralize opcional (lo usa el refresh) ---
    def test_restore_neutralize_false_saltea(self):
        staging = self.env["primate.cloud.environment"].create({
            "name": "Stg", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
            "origin_environment_id": self.origin.id,
        })
        stg_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "stg-srv", "account_id": self.account.id,
            "environment_id": staging.id, "aws_instance_id": "i-stg",
            "instance_state": "running", "region": "us-east-1",
        })
        backup = self.env["primate.cloud.backup"].create({
            "name": "b", "environment_id": self.origin.id,
            "database_id": self.db_1.id, "backup_type": "pcm_dump",
            "s3_bucket": "bucket", "s3_key": "k.dump",
        })
        backup.write({"state": "completed"})
        ssm = mock.Mock()
        ssm.run_script.return_value = {"status": "Success",
                                       "stdout": "PCM_RESTORE_OK"}
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}), \
                mock.patch.object(type(staging), "_staging_neutralize") \
                as neutralize, \
                mock.patch.object(type(stg_instance), "_get_ssm_service",
                                  return_value=ssm):
            ok = staging.job_restore_backup({
                "backup_id": backup.id, "db_name": "stg_db",
                "instance_id": stg_instance.id, "pre_backup": False,
                "neutralize": False,
            })
        self.assertTrue(ok)
        neutralize.assert_not_called()

    # --- Wizards ---
    def test_wizard_staging_preselecciona_origen_unico(self):
        wizard = self.env["primate.cloud.staging.create.wizard"].new({
            "origin_environment_id": self.origin.id,
        })
        wizard._onchange_origin()
        self.assertEqual(wizard.origin_instance_id, self.instance_1)
        self.assertEqual(wizard.origin_database_id, self.db_1)

    def test_wizard_staging_multi_exige_eleccion(self):
        self._add_second_instance_and_db()
        wizard = self.env["primate.cloud.staging.create.wizard"].create({
            "origin_environment_id": self.origin.id, "name": "Stg",
            "domain": "stg.forum.primate.cloud", "region": "us-east-1",
            "instance_type": "t3.small", "transfer_bucket": "pcm-transfer",
            "db_mode": "local_pg", "create_dns": False,
        })
        with self.assertRaises(UserError):
            wizard._validate()
        wizard.origin_instance_id = self.instance_1
        wizard.origin_database_id = self.db_1
        wizard._validate()  # ya no falla

    def test_wizard_refresh_encola_con_opciones(self):
        staging = self.env["primate.cloud.environment"].create({
            "name": "Stg", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
            "origin_environment_id": self.origin.id,
        })
        wizard = self.env["primate.cloud.staging.refresh.wizard"].create({
            "staging_id": staging.id, "refresh_repos": True,
            "re_neutralize": False, "use_last_backup": True,
        })
        with mock.patch.object(type(staging), "with_delay") as with_delay:
            wizard.action_refresh()
            with_delay.assert_called_once()
            job_args = with_delay.return_value.job_refresh_staging.call_args[0][0]
        self.assertTrue(job_args["refresh_repos"])
        self.assertFalse(job_args["neutralize"])
        self.assertTrue(job_args["use_last_backup"])

    def test_action_refresh_abre_wizard(self):
        staging = self.env["primate.cloud.environment"].create({
            "name": "Stg", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
            "origin_environment_id": self.origin.id,
        })
        action = staging.action_refresh_staging()
        self.assertEqual(action["res_model"],
                         "primate.cloud.staging.refresh.wizard")

    def test_staging_desde_instancia(self):
        action = self.instance_1.action_create_staging_from_instance()
        context = action["context"]
        self.assertEqual(context["default_origin_environment_id"], self.origin.id)
        self.assertEqual(context["default_origin_instance_id"], self.instance_1.id)
        self.assertEqual(context["default_origin_database_id"], self.db_1.id)

    def test_staging_desde_instancia_sin_entorno_activo(self):
        self.origin.state = "error"
        with self.assertRaises(UserError):
            self.instance_1.action_create_staging_from_instance()

    # --- Refresh end-to-end con último backup ---
    def test_refresh_con_ultimo_backup_no_dumpea_origen(self):
        staging = self.env["primate.cloud.environment"].create({
            "name": "Stg", "project_id": self.project.id,
            "env_type": "staging", "state": "active",
            "origin_environment_id": self.origin.id,
            "staging_origin_instance_id": self.instance_1.id,
            "staging_origin_database_id": self.db_1.id,
        })
        stg_instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "stg-srv", "account_id": self.account.id,
            "environment_id": staging.id, "aws_instance_id": "i-stg",
            "instance_state": "running", "region": "us-east-1",
        })
        self.env["primate.cloud.database"].create({
            "name": "stg_db", "account_id": self.account.id,
            "environment_id": staging.id, "db_type": "local_pg",
            "ec2_instance_id": stg_instance.id,
        })
        last = self.env["primate.cloud.backup"].create({
            "name": "último", "environment_id": self.origin.id,
            "database_id": self.db_1.id, "backup_type": "pcm_dump",
            "s3_bucket": "bucket", "s3_key": "staging/k.dump",
        })
        last.write({"state": "completed"})
        ssm = mock.Mock()
        ssm.run_script.return_value = {
            "status": "Success",
            "stdout": "PCM_RESTORE_OK\nNEUTRALIZED",
        }
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k"}), \
                mock.patch.object(type(self.origin), "_run_database_backup") \
                as fresh_backup, \
                mock.patch.object(type(stg_instance), "_get_ssm_service",
                                  return_value=ssm):
            ok = staging.job_refresh_staging({"use_last_backup": True})
        self.assertTrue(ok)
        # No se dumpeó producción: se usó el último backup registrado.
        fresh_backup.assert_not_called()
        self.assertEqual(staging.staging_origin_backup, "staging/k.dump")
