# -*- coding: utf-8 -*-
"""F1: restauración desde un dump EXTERNO (no producido por PCM).

Lo que se cubre acá, en este orden de importancia:

1. **No romper el camino histórico.** Los cuatro llamadores del staging llaman
   a ``job_restore_backup`` SIN ``origin``; el script que se genera tiene que
   seguir siendo el de siempre. Es la garantía que sostiene todo lo demás.
2. **La invariante del origen**: exactamente uno de ``backup_id`` u ``origin``,
   validada en el job (fuente de verdad) y en el wizard.
3. **El script del camino nuevo** en los tres formatos: detección por *magic*,
   las dos etapas de la guarda de espacio, el ``rsync`` del filestore al nombre
   FINAL de la base, la limpieza garantizada y que no se filtren credenciales.
4. **El cierre**: qué se registra, cuándo se borra la key de origen y cuándo NO.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "primate_cloud")
class TestF1RestoreExterno(TransactionCase):
    """Restore desde key de S3 o ruta del servidor (F1)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Barraca Julia"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Barraca Julia", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.env_srv = self.env["primate.cloud.environment"].create({
            "name": "BJ Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "bj-srv", "account_id": self.account.id,
            "environment_id": self.env_srv.id, "aws_instance_id": "i-bj",
            "instance_state": "running", "region": "us-east-1",
        })
        self.odoo_inst = self.env_srv.primary_instance_id
        self.odoo_inst.write({
            "service_name": "odoo-bj", "pg_user": "odoo_bj",
            "data_dir": "/opt/pcm/instances/bj/data",
        })
        self.db = self.env["primate.cloud.database"].create({
            "name": "barraca_julia", "account_id": self.account.id,
            "environment_id": self.env_srv.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id,
            "instance_id": self.odoo_inst.id,
        })
        # Backup del catálogo, para el camino histórico.
        self.backup = self.env["primate.cloud.backup"].create({
            "name": "Backup bj 20260919", "environment_id": self.env_srv.id,
            "database_id": self.db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-backups-test",
            "s3_key": "pcm-backups/BJ/BJ-Prod/barraca_julia/x.dump",
            "s3_filestore_key":
                "pcm-backups/BJ/BJ-Prod/barraca_julia/x-filestore.tar.gz",
        })
        self.backup.write({"state": "completed", "size_mb": 100.0})
        self.Env = self.env["primate.cloud.environment"]

    # ------------------------------------------------------------------
    # 1. El camino histórico NO cambia (los cuatro llamadores del staging)
    # ------------------------------------------------------------------
    def test_sin_origin_el_script_es_el_historico(self):
        """Sin ``origin`` se genera EXACTAMENTE el script de siempre.

        Los cuatro llamadores del staging (``job_create_staging*``,
        ``job_refresh_staging``, ``_staging_copy_database`` y el wrapper de
        ``primate.cloud.instance``) no pasan origen: si esto cambia, se rompe
        un camino ya verificado en real.
        """
        script = self.Env._build_backup_restore_script(
            "barraca_julia", self.backup, True, self.odoo_inst)
        # Marcas del camino histórico
        self.assertIn('FNAME="/var/tmp/pcm_restore_$$.dump"', script)
        self.assertIn("aws s3 cp s3://pcm-backups-test/", script)
        self.assertIn("pg_restore -d barraca_julia", script)
        self.assertIn("tar -xzf", script)
        # Y NADA del camino nuevo
        for nuevo in ("PCM_LOG_PATH", "unzip", "rsync", "PCM_FORMAT",
                      "head -c 5", "$WORK"):
            self.assertNotIn(nuevo, script,
                             "el camino histórico no debe traer %r" % nuevo)

    def test_sin_origin_orden_de_guardas_intacto(self):
        script = self.Env._build_backup_restore_script(
            "barraca_julia", self.backup, True, self.odoo_inst)
        self.assertLess(script.index("df -Pm /var/tmp"),
                        script.index("systemctl stop"))
        self.assertLess(script.index("systemctl stop"),
                        script.index("pg_terminate_backend"))
        self.assertLess(script.index("pg_terminate_backend"),
                        script.index("dropdb --if-exists"))
        self.assertLess(script.index("PCM_DROP_STARTED"),
                        script.index("dropdb --if-exists"))

    # ------------------------------------------------------------------
    # 2. La invariante del origen
    # ------------------------------------------------------------------
    def test_invariante_los_dos_origenes_falla(self):
        with self.assertRaises(UserError) as e:
            self.Env._restore_origen_normalizado({
                "backup_id": self.backup.id,
                "origin": {"type": "s3_uri", "uri": "s3://pcm-a/x.zip"},
            })
        self.assertIn("ambiguo", str(e.exception))

    def test_invariante_sin_ningun_origen_falla(self):
        with self.assertRaises(UserError) as e:
            self.Env._restore_origen_normalizado({})
        self.assertIn("Falta el origen", str(e.exception))

    def test_invariante_solo_catalogo_devuelve_none(self):
        self.assertIsNone(self.Env._restore_origen_normalizado(
            {"backup_id": self.backup.id}))

    def test_bucket_fuera_de_pcm_se_rechaza(self):
        """La IAM está acotada a pcm-*: se corta acá, no en el servidor."""
        with self.assertRaises(UserError) as e:
            self.Env._restore_origen_normalizado({
                "origin": {"type": "s3_uri", "uri": "s3://otro/x.zip"}})
        self.assertIn("pcm-", str(e.exception))

    def test_ruta_con_metacaracteres_se_rechaza(self):
        with self.assertRaises(UserError):
            self.Env._restore_origen_normalizado({
                "origin": {"type": "server_path",
                           "uri": "/var/tmp/x; rm -rf /"}})

    def test_ruta_relativa_se_rechaza(self):
        with self.assertRaises(UserError):
            self.Env._restore_origen_normalizado({
                "origin": {"type": "server_path", "uri": "var/tmp/x.zip"}})

    # ------------------------------------------------------------------
    # 3. El script del camino nuevo
    # ------------------------------------------------------------------
    def _script_externo(self, **origin):
        base = {"type": "s3_uri", "uri": "s3://pcm-backups-test/in/bj.zip",
                "format": "auto", "bucket": "pcm-backups-test",
                "key": "in/bj.zip"}
        base.update(origin)
        return self.Env._build_backup_restore_script(
            "barraca_julia", self.backup, False, self.odoo_inst,
            origin=base, origin_size_mb=2048.0)

    def test_deteccion_por_magic(self):
        script = self._script_externo()
        self.assertIn("head -c 5", script)
        self.assertIn("PGDMP*", script)
        self.assertIn("PK*", script)

    def test_dos_etapas_de_espacio(self):
        """Etapa 1 con el tamaño conocido; etapa 2 con el descomprimido real.

        El ``dump.sql`` de un zip comprime 5-10×, así que un factor sobre el
        tamaño del zip subestimaría justo en las bases grandes.
        """
        script = self._script_externo()
        self.assertIn("NEED_MB=", script)
        self.assertIn("unzip -l", script)
        self.assertIn("NEED2=", script)
        self.assertLess(script.index("NEED_MB="),
                        script.index("systemctl stop"))
        self.assertLess(script.index("NEED2="),
                        script.index("systemctl stop"))

    def test_rsync_al_filestore_con_el_nombre_FINAL(self):
        """El filestore va a <data_dir>/filestore/<db destino>, no la del zip."""
        script = self._script_externo()
        self.assertIn(
            'rsync -a --delete "$WORK/x/filestore/" '
            "/opt/pcm/instances/bj/data/filestore/barraca_julia/", script)
        self.assertIn("chown -R odoo:odoo "
                      "/opt/pcm/instances/bj/data/filestore/barraca_julia",
                      script)

    def test_limpieza_garantizada_y_unit_rearrancada(self):
        script = self._script_externo()
        self.assertIn('rm -rf "$WORK"', script)
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("systemctl start odoo-bj", script)
        self.assertIn("PCM_CLEANED", script)

    def test_ruta_local_no_se_borra_ni_se_copia(self):
        """El archivo que dejó el operador no es un temporal de PCM."""
        script = self._script_externo(type="server_path",
                                      uri="/var/tmp/pcm/bj.zip",
                                      bucket=None, key=None)
        self.assertIn("FNAME=/var/tmp/pcm/bj.zip", script)
        self.assertNotIn("aws s3 cp s3://", script)
        cleanup = script.split("cleanup()")[1].split("EXIT")[0]
        self.assertNotIn("/var/tmp/pcm/bj.zip", cleanup)

    def test_log_a_archivo_y_tail_acotado(self):
        script = self._script_externo()
        self.assertIn("mkdir -p /var/tmp/pcm/logs", script)
        self.assertIn("PCM_LOG_PATH=", script)
        self.assertIn('exec >>"$LOG" 2>&1', script)
        self.assertIn("tail -c 4000", script)

    def test_sin_credenciales_en_el_script(self):
        script = self._script_externo()
        for prohibido in ("PGPASSWORD", "password", "postgresql://"):
            self.assertNotIn(prohibido, script)

    def test_zip_corrupto_falla_ANTES_de_dropear(self):
        """Un zip inválido tiene que morir sin haber tocado la base.

        La extracción y la verificación de ``dump.sql`` se ejecutan ANTES del
        ``systemctl stop`` y del ``PCM_DROP_STARTED``, así que un zip corrupto
        o que no es un backup de Odoo falla con la base intacta. El pre-backup
        queda como red para los fallos POSTERIORES (un ``psql`` que muere a
        mitad), no para un caso evitable.

        Es una aserción de ORDEN sobre el script: sin un servidor real no se
        puede alimentar un zip roto desde un test unitario, así que se verifica
        la propiedad donde se puede verificar — que el chequeo precede al
        punto de no retorno.
        """
        script = self._script_externo()
        extraccion = script.index('unzip -q -o "$FNAME" -d "$WORK/x"')
        chequeo = script.index('if [ ! -f "$WORK/x/dump.sql" ]')
        stop = script.index("systemctl stop")
        marca_drop = script.index("PCM_DROP_STARTED")
        drop = script.index("dropdb --if-exists")
        self.assertLess(extraccion, chequeo)
        self.assertLess(chequeo, stop,
                        "el zip se verifica ANTES de parar Odoo")
        self.assertLess(chequeo, marca_drop,
                        "el zip se verifica ANTES del punto de no retorno")
        self.assertLess(chequeo, drop)
        # Y después del drop sólo se CARGA: no se vuelve a extraer ni a verificar.
        post_drop = script[drop:]
        self.assertNotIn("unzip -q -o", post_drop)
        self.assertNotIn('if [ ! -f "$WORK/x/dump.sql" ]', post_drop)
        self.assertIn('psql -q -d barraca_julia -f "$WORK/x/dump.sql"',
                      post_drop)

    def test_version_de_origen_sale_del_dump_ya_extraido(self):
        """Con el zip extraído antes, no se descomprime de nuevo para el header."""
        script = self._script_externo()
        self.assertIn('SRC_VER=$(head -n 200 "$WORK/x/dump.sql"', script)
        self.assertNotIn('unzip -p "$FNAME" dump.sql', script)

    def test_ramas_custom_y_plain_conservan_el_orden_historico(self):
        """Sólo el zip se reordenó: custom/plain cargan después del drop."""
        script = self._script_externo(format="custom")
        self.assertLess(script.index("dropdb --if-exists"),
                        script.index("pg_restore -d barraca_julia"))
        script = self._script_externo(format="plain")
        self.assertLess(script.index("dropdb --if-exists"),
                        script.index('psql -q -d barraca_julia -f "$FNAME"'))

    def test_las_tres_ramas_de_restore(self):
        script = self._script_externo()
        self.assertIn("pg_restore -d barraca_julia", script)
        self.assertIn("psql -q -d barraca_julia -f", script)
        self.assertIn("dump.sql", script)

    # ------------------------------------------------------------------
    # 4. Marcadores y cierre
    # ------------------------------------------------------------------
    def test_marcadores_se_parsean(self):
        salida = (
            "PCM_LOG_PATH=/var/tmp/pcm/logs/r.log\n"
            "PCM_FORMAT=odoo_zip\n"
            "PCM_FILESTORE_OK /opt/pcm/instances/bj/data/filestore/barraca_julia\n"
            "PCM_CLEANED work=/var/tmp/pcm/restore_9 descargado=(ninguno)\n"
            "PCM_RESTORE_OK\n")
        d = self.Env._restore_marcadores(salida)
        self.assertEqual(d["format"], "odoo_zip")
        self.assertEqual(d["log_path"], "/var/tmp/pcm/logs/r.log")
        self.assertIn("work=", d["cleaned"])

    def test_marcadores_toleran_vacio(self):
        self.assertEqual(self.Env._restore_marcadores(""), {})
        self.assertEqual(self.Env._restore_marcadores(None), {})

    def test_borrado_de_origen_no_aplica_a_ruta_local(self):
        detalle = self.env_srv._restore_borrar_origen(
            {"type": "server_path", "uri": "/var/tmp/pcm/bj.zip"},
            self.account, "us-east-1")
        self.assertIn("no aplica", detalle)

    def test_retencion_de_pre_restore_usa_el_parametro(self):
        """La retención de los pre-backups es corta y propia, no la de la política."""
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.pre_restore_retention_days", "3")
        with mock.patch(
            "odoo.addons.primate_cloud_manager.services.aws_s3.AwsS3Service"
            ".put_lifecycle_rule", return_value="pcm-pre-restore"
        ) as regla:
            self.env_srv._restore_asegurar_retencion_pre_backup(
                "pcm-backups-test", "us-east-1")
            regla.assert_called_once()
            self.assertEqual(regla.call_args[0][1], "pre-restore/")
            self.assertEqual(regla.call_args[0][2], 3)

    def test_retencion_en_cero_no_crea_regla(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.pre_restore_retention_days", "0")
        with mock.patch(
            "odoo.addons.primate_cloud_manager.services.aws_s3.AwsS3Service"
            ".put_lifecycle_rule"
        ) as regla:
            self.assertIsNone(
                self.env_srv._restore_asegurar_retencion_pre_backup(
                    "pcm-backups-test", "us-east-1"))
            regla.assert_not_called()

    # ------------------------------------------------------------------
    # 5. El wizard
    # ------------------------------------------------------------------
    def _wizard(self, **vals):
        base = {"target_environment_id": self.env_srv.id,
                "target_instance_id": self.machine.id,
                "target_db_name": "barraca_julia",
                "confirm_environment_name": self.env_srv.name}
        base.update(vals)
        return self.env["primate.cloud.backup.restore.wizard"].create(base)

    def test_wizard_sin_origen_falla(self):
        with self.assertRaises(UserError) as e:
            self._wizard().action_restore()
        self.assertIn("Falta el origen", str(e.exception))

    def test_wizard_con_los_dos_origenes_falla(self):
        with self.assertRaises(UserError) as e:
            self._wizard(backup_id=self.backup.id, origin_type="s3_uri",
                         origin_uri="s3://pcm-a/x.zip").action_restore()
        self.assertIn("un solo origen", str(e.exception))

    def test_wizard_origen_sin_uri_falla(self):
        with self.assertRaises(UserError) as e:
            self._wizard(origin_type="s3_uri").action_restore()
        self.assertIn("Completá", str(e.exception))

    def test_wizard_encola_con_origin(self):
        wizard = self._wizard(
            origin_type="s3_uri",
            origin_uri=" s3://pcm-backups-test/in/bj.zip ",
            origin_format="odoo_zip")
        with mock.patch.object(type(self.env_srv), "with_delay") as delay:
            wizard.action_restore()
            delay.assert_called_once()
            params = delay.return_value.job_restore_backup.call_args[0][0]
        self.assertNotIn("backup_id", params)
        # La URI llega sin espacios de más.
        self.assertEqual(params["origin"]["uri"],
                         "s3://pcm-backups-test/in/bj.zip")
        self.assertEqual(params["origin"]["format"], "odoo_zip")
        self.assertTrue(params["delete_source"])

    def test_wizard_del_catalogo_sigue_encolando_backup_id(self):
        """El botón "Restaurar" de un registro no cambió."""
        wizard = self._wizard(backup_id=self.backup.id)
        with mock.patch.object(type(self.env_srv), "with_delay") as delay:
            wizard.action_restore()
            params = delay.return_value.job_restore_backup.call_args[0][0]
        self.assertEqual(params["backup_id"], self.backup.id)
        self.assertNotIn("origin", params)
