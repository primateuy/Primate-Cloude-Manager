# -*- coding: utf-8 -*-
"""Tests de Fase 9 — Bloque 1, paso (a): identificador estable pcm_ref.

Opción A: clave de atribución de costos inmutable. `environment.pcm_ref` se
genera por registro en create; `res.partner.pcm_ref` se genera lazy y NO se
pisa. El backfill (migración) llena solo vacíos — se ejercita su lógica acá.
"""
import unittest
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

try:
    import boto3  # noqa: F401
    from moto import mock_aws
    HAS_MOTO = True
except ImportError:  # pragma: no cover
    HAS_MOTO = False


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


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud")
class TestRetagPhase9(TransactionCase):
    """Paso (c): re-tagging idempotente, conflicto y dry-run (moto)."""

    def _setup_account_with_instance(self):
        from ..services import aws_base, aws_ec2
        account = self.env["primate.cloud.account"].create({
            "name": "moto", "default_region": "us-east-1",
            "iam_access_key_id": "testing", "iam_secret_access_key": "testing",
        })
        partner = self.env["res.partner"].create({"name": "Cli"})
        project = self.env["primate.cloud.project"].create({
            "name": "P", "account_id": account.id, "partner_id": partner.id})
        environment = self.env["primate.cloud.environment"].create({
            "name": "E", "project_id": project.id, "env_type": "production",
            "state": "active"})
        # EC2 REAL en moto SIN los tags estables (simula recurso legado).
        client = boto3.client("ec2", region_name="us-east-1")
        res = client.run_instances(
            ImageId="ami-12345678", MinCount=1, MaxCount=1,
            InstanceType="t3.micro",
            TagSpecifications=[{"ResourceType": "instance",
                                "Tags": [{"Key": "Name", "Value": "e"}]}])
        aws_id = res["Instances"][0]["InstanceId"]
        instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "e", "account_id": account.id,
            "environment_id": environment.id, "aws_instance_id": aws_id,
            "instance_state": "running", "region": "us-east-1"})
        return account, environment, instance, aws_id

    def _live_env_id_tag(self, aws_id):
        from ..services import aws_base
        client = boto3.client("ec2", region_name="us-east-1")
        r = client.describe_instances(InstanceIds=[aws_id])
        tags = {t["Key"]: t["Value"]
                for t in r["Reservations"][0]["Instances"][0].get("Tags", [])}
        return tags.get(aws_base.ENVIRONMENT_ID_TAG)

    def test_retag_aplica_y_es_idempotente(self):
        with mock_aws():
            account, environment, instance, aws_id = \
                self._setup_account_with_instance()
            # 1ª corrida: el recurso no tiene el tag → tagged=1.
            c1 = account.job_retag_resources(dry_run=False)
            self.assertEqual(c1["tagged"], 1)
            self.assertEqual(c1["already_ok"], 0)
            self.assertEqual(self._live_env_id_tag(aws_id), environment.pcm_ref)
            # 2ª corrida: ya está ok → tagged=0, already_ok=1 (señal de que no
            # hay pendientes).
            c2 = account.job_retag_resources(dry_run=False)
            self.assertEqual(c2["tagged"], 0)
            self.assertEqual(c2["fixed"], 0)
            self.assertEqual(c2["already_ok"], 1)

    def test_retag_conflicto_sobrescribe_y_cuenta_aparte(self):
        with mock_aws():
            account, environment, instance, aws_id = \
                self._setup_account_with_instance()
            from ..services import aws_base
            # Alguien puso un valor DISTINTO a mano en AWS.
            boto3.client("ec2", region_name="us-east-1").create_tags(
                Resources=[aws_id],
                Tags=[{"Key": aws_base.ENVIRONMENT_ID_TAG,
                       "Value": "pcm_env_valor_ajeno"}])
            c = account.job_retag_resources(dry_run=False)
            self.assertEqual(c["fixed"], 1)      # contado aparte
            self.assertEqual(c["tagged"], 0)
            # El modelo es la fuente de verdad: se sobrescribió.
            self.assertEqual(self._live_env_id_tag(aws_id), environment.pcm_ref)

    def test_retag_dry_run_no_aplica(self):
        with mock_aws():
            account, environment, instance, aws_id = \
                self._setup_account_with_instance()
            c = account.job_retag_resources(dry_run=True)
            self.assertEqual(c["tagged"], 1)     # lo reporta en el plan
            # ...pero NO aplicó nada en AWS.
            self.assertIsNone(self._live_env_id_tag(aws_id))

    def test_retag_incluye_volumenes(self):
        with mock_aws():
            account, environment, instance, aws_id = \
                self._setup_account_with_instance()
            from ..services import aws_base
            account.job_retag_resources(dry_run=False)
            # El volumen raíz también quedó taggeado.
            ec2 = boto3.client("ec2", region_name="us-east-1")
            vols = ec2.describe_volumes(Filters=[
                {"Name": "attachment.instance-id", "Values": [aws_id]}])
            vtags = {t["Key"]: t["Value"]
                     for t in vols["Volumes"][0].get("Tags", [])}
            self.assertEqual(vtags.get(aws_base.ENVIRONMENT_ID_TAG),
                             environment.pcm_ref)

    def test_retag_salta_terminada(self):
        with mock_aws():
            account, environment, instance, aws_id = \
                self._setup_account_with_instance()
            instance.instance_state = "terminated"
            c = account.job_retag_resources(dry_run=False)
            # No se re-taggea una instancia terminada.
            self.assertEqual(c["tagged"], 0)
            self.assertEqual(c["already_ok"], 0)


@tagged("post_install", "-at_install", "primate_cloud")
class TestCostPullPhase9(TransactionCase):
    """Bloque 2: pull de Cost Explorer, atribución robusta, upsert y cuadre."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente X"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Forum", "account_id": self.account.id,
            "partner_id": self.partner.id})
        self.env_prod = self.env["primate.cloud.environment"].create({
            "name": "Forum Prod", "project_id": self.project.id,
            "env_type": "production", "state": "active"})
        self.Entry = self.env["primate.cloud.cost.entry"]

    def _ce_result(self, groups, total=None):
        """Arma la respuesta normalizada de aws_cost_explorer."""
        if total is None:
            total = sum(a for _k, a in groups)
        return {"total": total, "currency": "USD",
                "groups": [{"keys": k, "amount": a, "currency": "USD"}
                           for k, a in groups]}

    def _persist(self, result, period=None):
        from datetime import date
        period = period or date(2026, 7, 1)
        self.account._persist_cost_result(
            result, period, date(2026, 8, 1), "monthly",
            self.env.cr.now() if hasattr(self.env.cr, "now") else None)
        return period

    # --- Atribución robusta (por pcm_ref exacto) ---
    def test_atribucion_entorno_existente(self):
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        self._persist(self._ce_result([([tag, "AmazonEC2"], 10.0)]))
        entry = self.Entry.search([("environment_ref", "=", env_ref)])
        self.assertEqual(entry.environment_name, "Forum Prod")
        self.assertEqual(entry.environment_id, self.env_prod)
        self.assertEqual(entry.client_name, "Cliente X")
        self.assertEqual(entry.partner_id, self.partner)
        self.assertEqual(entry.amount, 10.0)

    def test_atribucion_entorno_borrado_conserva_ref(self):
        """Un ref cuyo entorno no existe: name 'Entorno eliminado', ref
        conservado, id/partner en False — el costo NO se pierde."""
        tag = "primate:environment_id$pcm_env_borrado123"
        self._persist(self._ce_result([([tag, "AmazonRDS"], 5.0)]))
        entry = self.Entry.search([("environment_ref", "=", "pcm_env_borrado123")])
        self.assertTrue(entry)
        self.assertEqual(entry.environment_name, "Entorno eliminado")
        self.assertEqual(entry.client_name, "Cliente eliminado")
        self.assertFalse(entry.environment_id)
        self.assertFalse(entry.partner_id)
        self.assertEqual(entry.amount, 5.0)

    def test_atribucion_sin_tag_es_sin_atribuir(self):
        """Grupo sin valor de tag → bucket 'Sin atribuir'."""
        self._persist(self._ce_result([(["", "AWSDataTransfer"], 3.0)]))
        entry = self.Entry.search([("environment_ref", "=", False),
                                   ("service", "=", "AWSDataTransfer")])
        self.assertEqual(entry.environment_name, "Sin atribuir")
        self.assertEqual(entry.amount, 3.0)

    def test_partner_esquema_tres_campos_borrado(self):
        """El cliente tiene el mismo esquema: si el entorno existe pero se
        materializa el partner ref; el snapshot de nombre queda."""
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        self._persist(self._ce_result([([tag, "AmazonEC2"], 7.0)]))
        entry = self.Entry.search([("environment_ref", "=", env_ref)])
        self.assertTrue(entry.client_ref)  # materializado
        self.assertEqual(entry.client_ref, self.partner.pcm_ref)

    # --- Upsert idempotente (re-pull actualiza, no duplica) ---
    def test_upsert_no_duplica_actualiza(self):
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        period = self._persist(self._ce_result([([tag, "AmazonEC2"], 10.0)]))
        # 2º pull del MISMO período con valor nuevo (el mes en curso cambió).
        self._persist(self._ce_result([([tag, "AmazonEC2"], 12.5)]), period)
        entries = self.Entry.search([
            ("account_id", "=", self.account.id),
            ("environment_ref", "=", env_ref),
            ("service", "=", "AmazonEC2"),
            ("period_start", "=", period)])
        self.assertEqual(len(entries), 1)      # NO duplicó
        self.assertEqual(entries.amount, 12.5)  # actualizó el valor

    def test_upsert_sin_atribuir_no_duplica(self):
        period = self._persist(self._ce_result([(["", "Tax"], 1.0)]))
        self._persist(self._ce_result([(["", "Tax"], 2.0)]), period)
        entries = self.Entry.search([
            ("account_id", "=", self.account.id),
            ("environment_ref", "=", False), ("service", "=", "Tax"),
            ("period_start", "=", period)])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries.amount, 2.0)

    def test_upsert_sin_atribuir_vs_eliminado_son_filas_distintas(self):
        """Claves de upsert DISTINTAS: 'Sin atribuir' (ref NULL) y 'Entorno
        eliminado' (ref con valor, id False) NO se colapsan aunque compartan
        cuenta/período/servicio — son costos de naturaleza distinta (nunca
        atribuido vs atribuido a algo que ya no existe)."""
        tag_del = "primate:environment_id$pcm_env_muerto"
        period = self._persist(self._ce_result([
            (["", "AmazonEC2"], 3.0),          # sin atribuir (ref NULL)
            ([tag_del, "AmazonEC2"], 8.0),     # entorno eliminado (ref con valor)
        ]))
        sin = self.Entry.search([
            ("account_id", "=", self.account.id), ("period_start", "=", period),
            ("service", "=", "AmazonEC2"), ("environment_ref", "=", False)])
        elim = self.Entry.search([
            ("account_id", "=", self.account.id), ("period_start", "=", period),
            ("service", "=", "AmazonEC2"),
            ("environment_ref", "=", "pcm_env_muerto")])
        self.assertEqual(len(sin), 1)
        self.assertEqual(sin.amount, 3.0)
        self.assertEqual(sin.environment_name, "Sin atribuir")
        self.assertEqual(len(elim), 1)
        self.assertEqual(elim.amount, 8.0)
        self.assertEqual(elim.environment_name, "Entorno eliminado")
        self.assertNotEqual(sin.id, elim.id)   # DOS filas separadas
        # Y re-pull no las colapsa entre sí.
        self._persist(self._ce_result([
            (["", "AmazonEC2"], 3.5),
            ([tag_del, "AmazonEC2"], 8.5)]), period)
        self.assertEqual(self.Entry.search_count([
            ("account_id", "=", self.account.id), ("period_start", "=", period),
            ("service", "=", "AmazonEC2")]), 2)

    # --- Cuadre: suma de atribuciones = total CE ---
    def test_cuadre_suma_igual_total(self):
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        # entorno + sin-atribuir; total = 10 + 4 = 14.
        result = self._ce_result([([tag, "AmazonEC2"], 10.0),
                                   (["", "AWSDataTransfer"], 4.0)], total=14.0)
        period = self._persist(result)
        ok = self.account._reconcile_costs(result, period, "monthly")
        self.assertTrue(ok)

    def test_cuadre_detecta_descuadre(self):
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        # Persistimos 10 pero el total CE dice 14 (faltarían 4 sin atribuir).
        result = self._ce_result([([tag, "AmazonEC2"], 10.0)], total=14.0)
        period = self._persist(result)
        ok = self.account._reconcile_costs(result, period, "monthly")
        self.assertFalse(ok)  # no cuadra → avisa

    def test_cuadre_con_entorno_borrado_igual_cuadra(self):
        """El bucket 'eliminado' cuenta para el total: suma = total igual."""
        tag_del = "primate:environment_id$pcm_env_x"
        result = self._ce_result([([tag_del, "AmazonEC2"], 8.0),
                                   (["", "Tax"], 2.0)], total=10.0)
        period = self._persist(result)
        self.assertTrue(self.account._reconcile_costs(result, period, "monthly"))

    # --- Salto de grupos en cero: no se persisten, pero el cuadre no se afecta ---
    def test_grupos_cero_no_se_persisten_pero_cuadran(self):
        """CE devuelve un grupo por servicio 'tocado' aunque no facture (visto
        en la pasada real: 10 grupos, todos 0.0). Esas filas de detalle NO se
        persisten (ruido), pero _reconcile_costs sigue cerrando porque reconcilia
        contra result['total'] (total real de CE), no contra la suma persistida."""
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        # Un grupo con costo real + varios en cero (distintos servicios y buckets).
        result = self._ce_result([
            ([tag, "AmazonEC2"], 12.0),          # facturado → se persiste
            ([tag, "AWSGlue"], 0.0),             # tocado pero 0 → NO se persiste
            (["", "AmazonS3"], 0.0),             # sin atribuir y 0 → NO se persiste
            (["", "AWSKMS"], 0.0),               # id.
        ], total=12.0)
        period = self._persist(result)
        # Solo la fila no-cero quedó persistida.
        rows = self.Entry.search([("account_id", "=", self.account.id),
                                  ("period_start", "=", period)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.service, "AmazonEC2")
        self.assertEqual(rows.amount, 12.0)
        # El cuadre cierra: persistido (12) == total CE (12), aunque los ceros
        # no estén en la tabla.
        self.assertTrue(self.account._reconcile_costs(result, period, "monthly"))

    def test_repull_a_cero_elimina_fila_vieja(self):
        """Si un pull previo dejó una fila con costo y el re-pull la reporta en
        cero (corrección de AWS), la fila vieja se elimina para no arrastrar dato
        obsoleto — y el cuadre sigue cerrando."""
        env_ref = self.env_prod.pcm_ref
        tag = "primate:environment_id$" + env_ref
        period = self._persist(self._ce_result([([tag, "AmazonEC2"], 9.0)]))
        self.assertEqual(self.Entry.search_count([
            ("account_id", "=", self.account.id), ("period_start", "=", period)]), 1)
        # Re-pull del mismo período: ahora ese servicio quedó en 0.
        result0 = self._ce_result([([tag, "AmazonEC2"], 0.0)], total=0.0)
        self._persist(result0, period)
        self.assertEqual(self.Entry.search_count([
            ("account_id", "=", self.account.id), ("period_start", "=", period)]), 0)
        self.assertTrue(self.account._reconcile_costs(result0, period, "monthly"))


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud")
class TestCostExplorerServicePhase9(TransactionCase):
    """Round-trip del adaptador Cost Explorer contra moto (datos vacíos: valida
    forma/parsing, no valores — eso es la prueba real del cierre de fase)."""

    def test_get_cost_and_usage_roundtrip(self):
        from ..services import aws_base, aws_cost_explorer
        with mock_aws():
            base = aws_base.AwsBaseService("testing", "testing", "us-east-1")
            svc = aws_cost_explorer.AwsCostExplorerService(base)
            r = svc.get_cost_and_usage(
                "2026-06-01", "2026-07-01", "MONTHLY",
                [{"Type": "TAG", "Key": "primate:environment_id"},
                 {"Type": "DIMENSION", "Key": "SERVICE"}])
        # moto devuelve vacío: la forma normalizada está bien, no rompe.
        self.assertIn("total", r)
        self.assertIn("groups", r)
        self.assertIsInstance(r["groups"], list)

    def test_get_cost_forecast_tolera_sin_historico(self):
        from ..services import aws_base, aws_cost_explorer
        with mock_aws():
            base = aws_base.AwsBaseService("testing", "testing", "us-east-1")
            svc = aws_cost_explorer.AwsCostExplorerService(base)
            f = svc.get_cost_forecast("2026-07-04", "2026-08-01")
        self.assertIn("amount", f)
        self.assertIsInstance(f["amount"], float)


@tagged("post_install", "-at_install", "primate_cloud")
class TestMetricsPhase9(TransactionCase):
    """Bloque 3: snapshot de métricas, None≠cero, monitor_state, alertas."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id})
        self.environment = self.env["primate.cloud.environment"].create({
            "name": "E", "project_id": self.project.id,
            "env_type": "production", "state": "active"})
        self.instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.environment.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1"})

    def _cw(self, **metrics):
        cw = mock.Mock()
        cw.get_ec2_metrics.return_value = metrics
        return cw

    # --- Snapshot y None ≠ cero ---
    def test_snapshot_persiste_y_cachea(self):
        cw = self._cw(cpu=42.0, network_in=100.0, network_out=200.0,
                      status_check_failed=0.0)
        snap = self.instance._take_metrics_snapshot(cw)
        self.assertEqual(snap.cpu, 42.0)
        self.assertEqual(snap.environment_id, self.environment)
        self.assertEqual(self.instance.last_cpu, 42.0)
        self.assertTrue(self.instance.last_metric_date)

    def test_metrica_none_no_se_persiste_como_cero(self):
        """CPU sin dato (None) NO se guarda como 0: el campo queda sin escribir
        y el cache last_cpu no se toca (métrica ausente ≠ métrica en cero)."""
        # status_check con dato (0=OK real), cpu None (sin dato).
        cw = self._cw(cpu=None, network_in=None, network_out=None,
                      status_check_failed=0.0)
        snap = self.instance._take_metrics_snapshot(cw)
        # El cache de cpu NO se seteó (sigue en su default, sin last_cpu nuevo).
        self.assertFalse(self.instance.last_cpu)
        # status_check sí (0 es un valor real).
        self.assertEqual(self.instance.last_status_check_failed, 0.0)

    def test_snapshot_un_solo_request(self):
        """GetMetricData se llama UNA vez por instancia (todas las métricas
        empaquetadas)."""
        cw = self._cw(cpu=10.0, status_check_failed=0.0)
        self.instance._take_metrics_snapshot(cw)
        self.assertEqual(cw.get_ec2_metrics.call_count, 1)

    # --- monitor_state por umbrales ---
    def test_monitor_state_unknown_sin_datos(self):
        self.assertEqual(self.environment.monitor_state, "unknown")

    def test_monitor_state_ok_warn_critical(self):
        self.instance.write({"last_metric_date": fields.Datetime.now(),
                             "last_cpu": 50.0, "last_status_check_failed": 0.0})
        self.environment.invalidate_recordset()
        self.assertEqual(self.environment.monitor_state, "ok")
        self.instance.last_cpu = 85.0
        self.environment.invalidate_recordset()
        self.assertEqual(self.environment.monitor_state, "warn")
        self.instance.last_cpu = 97.0
        self.environment.invalidate_recordset()
        self.assertEqual(self.environment.monitor_state, "critical")

    def test_monitor_state_status_check_es_critico(self):
        """StatusCheckFailed >= 1 → Crítico aunque la CPU esté baja."""
        self.instance.write({"last_metric_date": fields.Datetime.now(),
                             "last_cpu": 5.0, "last_status_check_failed": 1.0})
        self.environment.invalidate_recordset()
        self.assertEqual(self.environment.monitor_state, "critical")

    # --- Alertas derivadas ---
    def test_status_check_entra_como_alerta(self):
        self.instance.last_status_check_failed = 1.0
        data = self.env["primate.cloud.dashboard"].get_dashboard_data()
        textos = [a["text"] for a in data["alerts"]]
        self.assertTrue(any("Status check fallido" in t for t in textos))

    def test_backup_no_cumple_entra_como_alerta(self):
        self.environment.backup_compliance = "non_compliant"
        data = self.env["primate.cloud.dashboard"].get_dashboard_data()
        textos = [a["text"] for a in data["alerts"]]
        self.assertTrue(any("Respaldo no cumple" in t for t in textos))

    def test_home_cuenta_status_check_en_alertas(self):
        before = self.env["primate.cloud.dashboard"].get_home_data()
        base = next(k["value"] for k in before["kpis"] if k["key"] == "alerts") \
            if any(k["key"] == "alerts" for k in before["kpis"]) else None
        self.instance.last_status_check_failed = 1.0
        after = self.env["primate.cloud.dashboard"].get_home_data()
        # El conteo de alertas subió al menos en 1 (el status check).
        kpi_after = [k for k in after["kpis"] if k["key"] == "alerts"]
        if kpi_after and base is not None:
            self.assertGreater(kpi_after[0]["value"], base)

    # --- Cron/job de snapshot ---
    def test_job_snapshot_metrics_recorre_recursos(self):
        with mock.patch.object(type(self.instance), "_take_metrics_snapshot") \
                as take:
            res = self.account.job_snapshot_metrics()
        take.assert_called_once()
        self.assertEqual(res["taken"], 1)


@unittest.skipUnless(HAS_MOTO, "moto no está instalado")
@tagged("post_install", "-at_install", "primate_cloud")
class TestCloudWatchServicePhase9(TransactionCase):
    """Round-trip del adaptador CloudWatch contra moto (datos vacíos → None)."""

    def test_get_ec2_metrics_roundtrip(self):
        from datetime import datetime, timedelta
        from ..services import aws_base, aws_cloudwatch
        with mock_aws():
            base = aws_base.AwsBaseService("testing", "testing", "us-east-1")
            svc = aws_cloudwatch.AwsCloudWatchService(base)
            end = datetime(2026, 7, 3, 12, 0)
            r = svc.get_ec2_metrics("i-123", "us-east-1",
                                    end - timedelta(hours=1), end)
        # Forma correcta: todas las claves presentes; sin datos → None (no 0).
        self.assertIn("cpu", r)
        self.assertIn("status_check_failed", r)
        self.assertIsNone(r["cpu"])  # moto no genera métricas → ausente


@tagged("post_install", "-at_install", "primate_cloud")
class TestSnapshotRetentionPhase9(TransactionCase):
    """Bloque 4: retención de monitor.snapshot (ventana cruda + downsample + tope)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id})
        self.env_ = self.env["primate.cloud.environment"].create({
            "name": "E", "project_id": self.project.id,
            "env_type": "production", "state": "active"})
        self.instance = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.env_.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1"})
        self.Snap = self.env["primate.cloud.monitor.snapshot"]

    def _snap(self, dt, cpu=10.0):
        return self.Snap.create({
            "ec2_instance_id": self.instance.id,
            "environment_id": self.env_.id, "snapshot_date": dt, "cpu": cpu})

    def test_retencion_cruda_downsample_y_tope(self):
        from datetime import timedelta
        now = fields.Datetime.now()
        # a) 3 snapshots de HOY (ventana cruda) → intactas.
        raw = [self._snap(now - timedelta(hours=h)) for h in (1, 2, 3)]
        # b) 3 snapshots del MISMO día viejo (30d) → downsample a 1 (la más nueva).
        # Anclado a mediodía para que ±2h no crucen la medianoche (si el test
        # corre pasada la 00:00, restar horas caería en otro día calendario).
        old_day = (now - timedelta(days=30)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        old_same_day = [self._snap(old_day - timedelta(hours=h)) for h in (0, 1, 2)]
        newest_old = old_same_day[0]  # la de h=0 es la más nueva del día
        # c) 1 snapshot de OTRO día viejo (31d) → se conserva su representante.
        other_old = self._snap(now - timedelta(days=31))
        # d) 1 snapshot > 365d → borrada por el tope duro.
        ancient = self._snap(now - timedelta(days=400))

        res = self.Snap._cron_cleanup_snapshots()

        # Ventana cruda intacta.
        for s in raw:
            self.assertTrue(s.exists())
        # Downsample: del día viejo queda solo la más nueva.
        self.assertTrue(newest_old.exists())
        self.assertFalse(old_same_day[1].exists())
        self.assertFalse(old_same_day[2].exists())
        # El otro día viejo conserva su representante.
        self.assertTrue(other_old.exists())
        # Tope duro: la ancestral se borró.
        self.assertFalse(ancient.exists())
        self.assertEqual(res["capped"], 1)
        self.assertEqual(res["downsampled"], 2)


@tagged("post_install", "-at_install", "primate_cloud")
class TestCostOverviewAndLogsPhase9(TransactionCase):
    """Bloque 4: serializers de la UI (resumen de costos con 'datos al', logs)."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id})
        self.env_ = self.env["primate.cloud.environment"].create({
            "name": "E", "project_id": self.project.id,
            "env_type": "production", "state": "active"})
        self.Dash = self.env["primate.cloud.dashboard"]

    def test_cost_overview_incluye_pulled_at_y_desglose(self):
        from datetime import date
        today = fields.Date.context_today(self.account)
        month_start = today.replace(day=1)
        now = fields.Datetime.now()
        self.account.cost_pulled_at = now
        Entry = self.env["primate.cloud.cost.entry"]
        Entry.create({
            "account_id": self.account.id, "period_start": month_start,
            "period_end": date(month_start.year, month_start.month, 28),
            "granularity": "monthly", "environment_ref": self.env_.pcm_ref,
            "environment_name": "E", "client_name": "Cli",
            "service": "AmazonEC2", "amount": 10.0, "pulled_at": now})
        Entry.create({
            "account_id": self.account.id, "period_start": month_start,
            "period_end": date(month_start.year, month_start.month, 28),
            "granularity": "monthly", "environment_ref": False,
            "environment_name": "Sin atribuir", "service": "Tax",
            "amount": 2.0, "pulled_at": now})
        data = self.Dash.get_cost_overview(self.account.id)
        # 'datos al' presente (honestidad).
        self.assertTrue(data["pulled_at"])
        self.assertEqual(data["current_total"], 12.0)
        labels = [r["label"] for r in data["by_environment"]]
        self.assertIn("E", labels)
        self.assertIn("Sin atribuir", labels)

    def test_cost_overview_pulled_at_mixto_no_rompe(self):
        """Con varias cuentas (una con pull, otra sin), el 'datos al' global no
        debe romper: max() sobre datetime + False comparaba bool y datetime."""
        now = fields.Datetime.now()
        self.account.cost_pulled_at = now
        # Segunda cuenta SIN pull (cost_pulled_at False).
        self.env["primate.cloud.account"].create({
            "name": "C2", "default_region": "us-east-1",
            "iam_access_key_id": "AK2", "iam_secret_access_key": "sk2"})
        # Sin account_id agrega TODAS las cuentas → antes crasheaba acá.
        data = self.Dash.get_cost_overview()
        self.assertEqual(
            data["pulled_at"], fields.Datetime.to_string(now))

    def test_get_instance_logs_instancia_detenida(self):
        self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.env_.id, "aws_instance_id": "i-1",
            "instance_state": "stopped", "region": "us-east-1"})
        # R4-B6: el wrapper recibe un id de primate.cloud.instance.
        res = self.Dash.get_odoo_logs(
            self.env_.primary_instance_id.id, "odoo")
        self.assertEqual(res["status"], "error")

    def test_fetch_logs_comando_sin_credenciales(self):
        """El comando de logs NO referencia el odoo.conf ni credenciales."""
        inst = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv", "account_id": self.account.id,
            "environment_id": self.env_.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1"})
        captured = {}
        ssm = mock.Mock()
        ssm.run_script.side_effect = lambda inst_id, cmd, **kw: (
            captured.update(cmd=cmd) or {"status": "Success", "stdout": "log"})
        with mock.patch.object(type(inst), "_get_ssm_service", return_value=ssm):
            res = inst.fetch_logs("odoo", lines=100, grep="ERROR",
                                  odoo_instance=self.env_.primary_instance_id)
        self.assertEqual(res["text"], "log")
        for forbidden in ("odoo.conf", "PGPASSWORD", "password", "db_password"):
            self.assertNotIn(forbidden, captured["cmd"])
        self.assertIn("journalctl -u odoo", captured["cmd"])
