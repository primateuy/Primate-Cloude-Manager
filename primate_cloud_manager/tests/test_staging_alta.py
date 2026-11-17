# -*- coding: utf-8 -*-
"""Staging de Alta na Pipa (2026-11-17): límites de unit, política de backup
obligatoria en producción, neutralize oficial + anonimización del padrón,
repos espejo con overrides de rama, guard anti-producción del refresco y
métricas RAM/disco del agente CloudWatch.

SSM/AWS siempre mockeados.
"""
import re
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..services import aws_cloudwatch


class _BaseStaging(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "aws_account_id": "512060111009",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Alta"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Alta", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "altanapipa", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.prod = self.server.primary_instance_id
        self.prod.write({"slug": "altanapipa", "pg_user": "odoo_alta",
                         "http_port": 8069, "gevent_port": 8072})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
        })
        self.stg = self.env["primate.cloud.instance"].create({
            "name": "altanapipa-staging", "project_id": self.project.id,
            "environment_id": self.server.id, "slug": "altanapipa-staging",
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community", "pg_user": "odoo_alta_stg",
            "http_port": 8079, "gevent_port": 8082,
            "origin_instance_id": self.prod.id,
        })

    def _params(self, **overrides):
        params = {"db_mode": "local_pg", "db_name": "altanapipa_staging",
                  "db_password": "PgPass_token-urlsafe-XYZ",
                  "db_host": "localhost", "domain": "stg.example.com",
                  "admin_password": "x"}
        params.update(overrides)
        return params


# ----------------------------------------------------------------------------
# Límites de recursos en la unit del staging (cgroups, no limit_memory_*)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestUnitLimitsStaging(_BaseStaging):

    def test_staging_lleva_cgroups_en_la_unit(self):
        """Con workers=0 Odoo ignora limit_memory_soft/hard: la protección
        de producción va en la UNIT del staging."""
        script = self.server._build_instance_install_script(
            self.stg, self._params(), is_default=False)
        self.assertIn("MemoryMax=1G", script)
        self.assertIn("MemoryHigh=768M", script)
        self.assertIn("CPUWeight=30", script)
        self.assertIn("IOWeight=30", script)
        # Dentro del heredoc de la unit, después del EnvironmentFile.
        self.assertLess(script.index("EnvironmentFile="),
                        script.index("MemoryMax=1G"))

    def test_produccion_sin_limites(self):
        script = self.server._build_instance_install_script(
            self.prod, self._params(db_name="altanapipa"), is_default=True)
        self.assertNotIn("MemoryMax", script)
        self.assertNotIn("CPUWeight", script)


# ----------------------------------------------------------------------------
# Producción no nace sin política de backup (gap 2026-11-17)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestBackupPolicyObligatoria(_BaseStaging):

    def _wizard(self, **vals):
        base = {"environment_id": self.server.id,
                "account_id": self.account.id, "region": "us-east-1",
                "domain": "altanapipa.primatecloudmanager.com",
                "db_mode": "local_pg", "db_name": "altanapipa",
                "create_dns": False}
        base.update(vals)
        return self.env["primate.cloud.provision.wizard"].create(base)

    def test_produccion_sin_politica_crea_y_asigna_la_diaria(self):
        wiz = self._wizard()
        wiz._validate()
        self.assertTrue(wiz.backup_policy_id)
        self.assertEqual(wiz.backup_policy_id.s3_bucket,
                         "pcm-backups-512060111009")
        self.assertEqual(wiz.backup_policy_id.expected_frequency, "daily")
        self.assertEqual(wiz._prepare_params()["backup_policy_id"],
                         wiz.backup_policy_id.id)
        # Idempotente: una segunda corrida REUSA la misma política.
        wiz2 = self._wizard()
        wiz2._validate()
        self.assertEqual(wiz2.backup_policy_id, wiz.backup_policy_id)

    def test_staging_no_exige_politica(self):
        self.server.env_type = "staging"
        wiz = self._wizard()
        wiz._validate()
        self.assertFalse(wiz.backup_policy_id)


# ----------------------------------------------------------------------------
# Neutralización: SQL + CLI oficial + checklist, y el padrón anonimizado
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestNeutralizacionOficial(_BaseStaging):

    def _backup(self):
        db = self.env["primate.cloud.database"].create({
            "name": "altanapipa", "environment_id": self.server.id,
            "account_id": self.account.id, "instance_id": self.prod.id})
        return self.env["primate.cloud.backup"].create({
            "name": "bk-test", "environment_id": self.server.id,
            "database_id": db.id, "s3_key": "k/x.dump", "s3_bucket": "b",
            "state": "completed", "size_mb": 10})

    def test_restore_a_staging_corre_sql_cli_y_checklist_en_orden(self):
        self.stg.write({"python_bin": "/opt/pcm/runtime/odoo-19/venv/bin/python3",
                        "odoo_bin": "/opt/pcm/runtime/odoo-19/src/odoo-bin",
                        "conf_path": "/etc/odoo/altanapipa-staging.conf"})
        sql = self.server._render_neutralization_sql("https://stg.example.com")
        script = self.server._build_backup_restore_script(
            "altanapipa_staging", self._backup(), False, self.stg,
            neutralize_sql=sql)
        self.assertIn("neutralize -c /etc/odoo/altanapipa-staging.conf "
                      "-d altanapipa_staging", script)
        self.assertIn("PCM_NEUTRALIZE_CLI_OK", script)
        # Orden: SQL → CLI oficial → checklist de SELECTs.
        self.assertLess(script.index("PCM_NEUTRALIZED"),
                        script.index("PCM_NEUTRALIZE_CLI_OK"))
        self.assertLess(script.index("PCM_NEUTRALIZE_CLI_OK"),
                        script.index("PCM_CHECKLIST_OK"))

    def test_sin_paths_el_cli_se_omite_compat(self):
        self.stg.write({"python_bin": False, "odoo_bin": False})
        sql = self.server._render_neutralization_sql("https://stg.example.com")
        script = self.server._build_backup_restore_script(
            "altanapipa_staging", self._backup(), False, self.stg,
            neutralize_sql=sql)
        self.assertNotIn("PCM_NEUTRALIZE_CLI_OK", script)
        self.assertIn("PCM_CHECKLIST_OK", script)   # el juez final queda

    def test_sql_anonimiza_padron_y_siembra_prueba(self):
        sql = self.server._render_neutralization_sql("https://stg.example.com")
        self.assertIn("portal_birthdate", sql)
        self.assertIn("SET portal_birthdate = NULL", sql)
        self.assertIn("STAGING-TEST-1", sql)
        self.assertIn("STAGING-TEST-2", sql)
        self.assertIn("DATE '2000-01-01'", sql)
        # Los usuarios portal de contactos con ref quedan desactivados.
        self.assertIn("UPDATE res_users SET active = FALSE", sql)


# ----------------------------------------------------------------------------
# Repos espejo del staging (orden, token copiado, overrides de rama)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestReposEspejoStaging(_BaseStaging):

    def _repo(self, name, seq, branch="19.0"):
        repo = self.env["primate.cloud.repository"].create({
            "name": name, "environment_id": self.server.id,
            "github_url": "https://github.com/PrimateUy-SAS/%s" % name,
            "local_path": "/opt/pcm/instances/altanapipa/repos/%s" % name,
            "configured_branch": branch, "sequence": seq,
        })
        repo.github_token = "tok-%s" % name
        return repo

    def test_fichas_espejo_con_orden_token_y_override(self):
        self._repo("webOCA", 10)
        self._repo("alta_na_pipa", 40)
        clones = []
        with mock.patch.object(
                type(self.machine), "clone_addon",
                side_effect=lambda url, path, ref=None, **kw:
                clones.append((path, ref)) or True), \
             mock.patch.object(type(self.machine),
                               "_ensure_custom_addons_path",
                               return_value="restarted"):
            self.server._staging_clone_repos_to_instance(
                self.server, self.machine, self.stg, "us-east-1",
                branch_overrides={"alta_na_pipa": "19.0_staging"})
        # En orden, bajo repos/ del slug staging, con el override aplicado.
        self.assertEqual(clones, [
            ("/opt/pcm/instances/altanapipa-staging/repos/webOCA", "19.0"),
            ("/opt/pcm/instances/altanapipa-staging/repos/alta_na_pipa",
             "19.0_staging")])
        espejos = self.env["primate.cloud.repository"].search(
            [("instance_id", "=", self.stg.id)], order="sequence")
        self.assertEqual(espejos.mapped("name"), ["webOCA", "alta_na_pipa"])
        self.assertEqual(espejos.mapped("configured_branch"),
                         ["19.0", "19.0_staging"])
        # El token CIFRADO se copió y DESCIFRA al mismo valor (misma Fernet).
        self.assertEqual(espejos[0]._get_github_token(), "tok-webOCA")

    def test_clone_fallido_corta(self):
        self._repo("webOCA", 10)
        with mock.patch.object(type(self.machine), "clone_addon",
                               return_value=False):
            with self.assertRaises(UserError):
                self.server._staging_clone_repos_to_instance(
                    self.server, self.machine, self.stg, "us-east-1")

    def test_parser_de_overrides(self):
        Wizard = self.env["primate.cloud.staging.create.wizard"]
        wiz = Wizard.new({"branch_overrides":
                          "alta_na_pipa:19.0_staging, webOCA:19.0"})
        self.assertEqual(wiz._parse_branch_overrides(),
                         {"alta_na_pipa": "19.0_staging", "webOCA": "19.0"})
        for malo in ("sin_dos_puntos", "repo:rama con espacios"):
            wiz = Wizard.new({"branch_overrides": malo})
            with self.assertRaises(UserError, msg=malo):
                wiz._parse_branch_overrides()


# ----------------------------------------------------------------------------
# El refresco JAMÁS escribe sobre producción (contrato con test)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestRefreshNoApuntaAProduccion(_BaseStaging):

    def test_action_y_job_rechazan_produccion(self):
        with self.assertRaises(UserError):
            self.prod.action_refresh_staging()
        with self.assertRaises(UserError):
            self.prod.job_refresh_staging_instance({})
        # La instancia staging pasa la guarda de tipo (falla después por
        # otra cosa en este fixture sin base, pero NO por el tipo).
        try:
            self.stg.job_refresh_staging_instance({})
        except UserError as error:
            self.assertNotIn("STAGING", str(error))


# ----------------------------------------------------------------------------
# RAM/disco del agente CloudWatch (namespace CWAgent)
# ----------------------------------------------------------------------------
@tagged("post_install", "-at_install", "primate_cloud")
class TestMetricasAgente(_BaseStaging):

    def _servicio(self, client):
        base = mock.Mock()
        base.get_client.return_value = client
        return aws_cloudwatch.AwsCloudWatchService(base)

    def test_ram_por_instanceid_y_disk_por_descubrimiento(self):
        client = mock.Mock()
        client.list_metrics.return_value = {"Metrics": [{
            "Dimensions": [
                {"Name": "InstanceId", "Value": "i-1"},
                {"Name": "device", "Value": "root"},
                {"Name": "fstype", "Value": "ext4"},
                {"Name": "path", "Value": "/"}]}]}
        client.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "ram", "Values": [37.5]},
            {"Id": "disk", "Values": [29.1]}]}
        datos = self._servicio(client).get_agent_metrics(
            "i-1", "us-east-1", 0, 1)
        self.assertEqual(datos, {"ram": 37.5, "disk": 29.1})
        # El query del disco usa las dimensiones DESCUBIERTAS (las 4).
        consulta = client.get_metric_data.call_args.kwargs[
            "MetricDataQueries"]
        disco = [q for q in consulta if q["Id"] == "disk"][0]
        self.assertEqual(
            len(disco["MetricStat"]["Metric"]["Dimensions"]), 4)

    def test_sin_listmetrics_el_disco_degrada(self):
        client = mock.Mock()
        client.list_metrics.side_effect = Exception("AccessDenied")
        client.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "ram", "Values": [40.0]}]}
        datos = self._servicio(client).get_agent_metrics(
            "i-1", "us-east-1", 0, 1)
        self.assertEqual(datos, {"ram": 40.0, "disk": None})

    def test_snapshot_persiste_ram_disco_solo_con_agente_activo(self):
        cw = mock.Mock()
        cw.get_ec2_metrics.return_value = {"cpu": 1.0}
        cw.get_agent_metrics.return_value = {"ram": 55.0, "disk": 31.0}
        self.server.cw_agent_state = "active"
        snap = self.machine._take_metrics_snapshot(cw)
        self.assertEqual(snap.ram, 55.0)
        self.assertEqual(snap.disk, 31.0)
        self.assertEqual(self.machine.last_ram, 55.0)
        self.assertTrue(self.machine.last_agent_date)
        # Agente apagado: ni se consulta.
        cw2 = mock.Mock()
        cw2.get_ec2_metrics.return_value = {"cpu": 1.0}
        self.server.cw_agent_state = "disabled"
        self.machine._take_metrics_snapshot(cw2)
        cw2.get_agent_metrics.assert_not_called()
