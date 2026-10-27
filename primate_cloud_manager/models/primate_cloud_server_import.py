# -*- coding: utf-8 -*-
"""Importación masiva de servidores existentes de una cuenta AWS (F2).

Trae los EC2 de una cuenta —en las regiones elegidas— junto con sus instancias
Odoo, para clasificarlos después a mano por cliente. Es **inofensiva por
construcción**:

- Todo servidor nuevo entra en ``adopted_readonly`` (lo hace
  ``_sync_from_aws``): se puede mirar y no operar hasta que un admin habilite
  la gestión.
- Lo único que se ejecuta en los servidores es el discovery, que es de SÓLO
  LECTURA (contrato en ``data/discovery_read.py``).
- Un servidor sin acceso SSM no falla la corrida: queda «Sin acceso SSM» y el
  reporte dice qué hay que hacer para habilitarlo.
- Un layout no reconocido tampoco la falla: queda «no reconocido», con lo que
  se encontró.

Cada corrida deja una cabecera con contadores y una línea por servidor. El job
commitea después de cada servidor, por dos motivos: el bus de Odoo entrega los
mensajes recién en el commit (sin commits no hay progreso en vivo), y cada
servidor es un upsert independiente e idempotente, así que un error en el 40
no tiene por qué revertir los 39 anteriores.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import aws_ec2, aws_ssm
from ..tools import bus
from .primate_cloud_account import AWS_REGIONS

_logger = logging.getLogger(__name__)

UNCLASSIFIED_PROJECT_PARAM = "pcm.unclassified_project_id"
UNCLASSIFIED_PROJECT_NAME = "⚠ SIN CLASIFICAR (asignar cliente)"
UNASSIGNED_PARTNER_PARAM = "pcm.unassigned_partner_id"
UNASSIGNED_PARTNER_NAME = "⚠ SIN CLIENTE (asignar)"

# Qué hacer con un servidor al que PCM no llega por SSM. Va en el reporte.
# Texto plano a propósito: `_()` a nivel de módulo no tiene idioma resuelto
# al importar. Las otras constantes del proyecto tampoco lo usan.
PASOS_SIN_AGENTE = (
    "SSM no conoce esta instancia (el agente no está registrado):\n"
    "  1. Verificar que el agente SSM esté instalado y corriendo. En Ubuntu: "
    "`sudo snap list amazon-ssm-agent` y "
    "`sudo systemctl status snap.amazon-ssm-agent.amazon-ssm-agent`.\n"
    "  2. Adjuntarle a la EC2 un instance profile con la política "
    "AmazonSSMManagedInstanceCore (el de PCM es `pcm-ssm-role`). Consola EC2 → "
    "Acciones → Seguridad → Modificar rol de IAM.\n"
    "  3. Confirmar que tenga salida a internet (IP pública + ruta al IGW, o "
    "NAT) o VPC endpoints de ssm, ssmmessages y ec2messages.\n"
    "  4. Volver a correr la importación: el servidor se actualiza, no se "
    "duplica.")
PASOS_CONEXION_PERDIDA = (
    "El agente SSM está registrado pero no responde (%s):\n"
    "  1. Reiniciar el agente en el servidor (`sudo snap restart "
    "amazon-ssm-agent`) o reiniciar la instancia desde la consola de AWS.\n"
    "  2. Revisar la salida a internet o los VPC endpoints de SSM.\n"
    "  3. Volver a correr la importación.")


class PrimateCloudServerImport(models.Model):
    """Una corrida de importación de servidores (cabecera)."""

    _name = "primate.cloud.server.import"
    _description = "Importación de servidores desde AWS"
    _order = "id desc"

    name = fields.Char(string="Corrida", required=True, readonly=True,
                       default=lambda self: _("Importación"))
    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta AWS", required=True,
        readonly=True, ondelete="cascade", index=True)
    user_id = fields.Many2one(
        "res.users", string="Lanzada por", readonly=True, ondelete="set null",
        default=lambda self: self.env.user)
    regions = fields.Char(string="Regiones", required=True, readonly=True)
    include_stopped = fields.Boolean(string="Incluye detenidos", readonly=True)
    discover = fields.Boolean(string="Con discovery", readonly=True)
    state = fields.Selection(
        [("draft", "En cola"), ("running", "Corriendo"),
         ("done", "Terminada"), ("failed", "Fallida")],
        string="Estado", default="draft", required=True, readonly=True,
        index=True)
    started_at = fields.Datetime(string="Inicio", readonly=True)
    ended_at = fields.Datetime(string="Fin", readonly=True)

    total_servers = fields.Integer(string="Servidores", readonly=True)
    processed = fields.Integer(string="Procesados", readonly=True)
    created_count = fields.Integer(string="Nuevos", readonly=True)
    updated_count = fields.Integer(string="Actualizados", readonly=True)
    online_count = fields.Integer(string="Con acceso SSM", readonly=True)
    unreachable_count = fields.Integer(string="Sin acceso SSM", readonly=True)
    discovered_count = fields.Integer(string="Con Odoo detectado", readonly=True)
    unrecognized_count = fields.Integer(string="No reconocidos", readonly=True)
    error_count = fields.Integer(string="Con error", readonly=True)
    twin_incomplete_count = fields.Integer(
        string="Gemelo incompleto", readonly=True)
    progress = fields.Float(string="Avance (%)", readonly=True)

    line_ids = fields.One2many(
        "primate.cloud.server.import.line", "import_id", string="Servidores",
        readonly=True)
    report = fields.Text(string="Reporte", readonly=True)

    # ------------------------------------------------------------------
    @api.model
    def _proyecto_centinela(self):
        """Proyecto «SIN CLASIFICAR», find-or-create y ARCHIVADO.

        Mismo patrón que el partner centinela: se referencia por un parámetro
        del sistema (a prueba de renombre). Se crea **archivado** para que no
        aparezca en las vistas de negocio ni en los selectores de proyecto; las
        instancias lo pueden seguir referenciando, y el filtro «Sin clasificar»
        busca justamente eso (``project_id.active = False``).
        """
        Params = self.env["ir.config_parameter"].sudo()
        Project = self.env["primate.cloud.project"].sudo().with_context(
            active_test=False)
        proyecto = Project.browse(
            int(Params.get_param(UNCLASSIFIED_PROJECT_PARAM) or 0)).exists()
        if not proyecto:
            proyecto = Project.search(
                [("name", "=", UNCLASSIFIED_PROJECT_NAME)], limit=1)
        if not proyecto:
            Partner = self.env["res.partner"].sudo()
            partner = Partner.browse(
                int(Params.get_param(UNASSIGNED_PARTNER_PARAM) or 0)).exists()
            if not partner:
                partner = Partner.create({
                    "name": UNASSIGNED_PARTNER_NAME, "is_company": True,
                    "comment": "Partner centinela de primate_cloud_manager: "
                               "NO es un cliente real."})
                Params.set_param(UNASSIGNED_PARTNER_PARAM, str(partner.id))
            proyecto = Project.create({
                "name": UNCLASSIFIED_PROJECT_NAME,
                "partner_id": partner.id,
                "active": False,
                "notes": "Proyecto centinela de la importación de servidores: "
                         "agrupa las instancias Odoo que todavía no tienen "
                         "cliente asignado. NO es un cliente real.",
            })
        if proyecto.active:
            proyecto.active = False
        Params.set_param(UNCLASSIFIED_PROJECT_PARAM, str(proyecto.id))
        return proyecto

    def _regiones(self):
        """Regiones de la corrida, validadas contra las soportadas."""
        validas = {code for code, _label in AWS_REGIONS}
        pedidas = [r.strip() for r in (self.regions or "").split(",") if r.strip()]
        invalidas = [r for r in pedidas if r not in validas]
        if invalidas:
            raise UserError(_("Regiones no soportadas: %s") % ", ".join(invalidas))
        return pedidas

    def _avanzar(self, commit):
        """Publica el avance: bus (refresca la corrida si está abierta) y commit.

        El orden importa: el mensaje del bus se entrega recién al commitear.
        """
        self.progress = (100.0 * self.processed / self.total_servers
                         if self.total_servers else 100.0)
        bus.refresh_record(self.env, self)
        if commit:
            self.env.cr.commit()

    # ------------------------------------------------------------------
    def job_import_servers(self, commit=True):
        """Job: importa los EC2 de la cuenta, verifica SSM y descubre Odoo.

        Args:
            commit (bool): commitear después de cada servidor. Los tests lo
                pasan en False: un commit dentro de un TransactionCase rompe
                su aislamiento.
        """
        self.ensure_one()
        account = self.account_id
        Line = self.env["primate.cloud.server.import.line"]
        if self.account_id.provision_only:
            raise UserError(_(
                "La cuenta «%s» es de SOLO APROVISIONAMIENTO: no importa "
                "servidores ajenos. Importá con la cuenta de solo lectura.")
                % self.account_id.name)
        self.write({"state": "running", "started_at": fields.Datetime.now()})
        estados = {"running"} | ({"stopped"} if self.include_stopped else set())
        errores_region = []
        try:
            regiones = self._regiones()
            base = account._get_aws_service()
            ec2 = aws_ec2.AwsEc2Service(base)
            ssm = aws_ssm.AwsSsmService(base)
            centinela = self._proyecto_centinela() if self.discover else None
        except Exception as error:  # noqa: BLE001 - se audita, no se traga
            return self._cerrar(failed=True, detalle=str(error))
        bus.toast(self.env, _("Importando servidores de %s…") % account.name,
                  title=_("Importar servidores"), reload=False)

        # Primero se LISTA todo (DescribeInstances es barato) y recién después
        # se procesa: así el total se conoce desde el principio y el avance no
        # retrocede. Sumando región por región, 5 de 5 daba 100 % y al entrar
        # la región siguiente pasaba a 5 de 6 = 83 %.
        por_region = []
        for region in regiones:
            try:
                encontrados = [d for d in ec2.list_instances(region=region)
                               if d.get("instance_state") in estados]
            except Exception as error:  # noqa: BLE001 - la región falla sola
                errores_region.append(_("%(r)s: %(e)s", r=region, e=error))
                continue
            if encontrados:
                por_region.append((region, encontrados))
        self.total_servers = sum(len(e) for _r, e in por_region)
        self._avanzar(commit)

        for region, encontrados in por_region:
            sync = self.env["primate.cloud.ec2.instance"]._sync_from_aws(
                account, encontrados, return_records=True)
            try:
                pings = ssm.describe_instance_information(
                    [d["aws_instance_id"] for d in encontrados], region=region)
            except Exception as error:  # noqa: BLE001 - sin SSM no se aborta
                pings = {}
                errores_region.append(_(
                    "%(r)s: no se pudo consultar SSM (%(e)s)", r=region, e=error))
            for maquina, accion in sync["records"]:
                linea = Line.create({
                    "import_id": self.id, "ec2_instance_id": maquina.id,
                    "aws_instance_id": maquina.aws_instance_id,
                    "name": maquina.name, "region": region,
                    "sync_result": accion,
                    "instance_state": maquina.instance_state,
                })
                self._procesar_servidor(maquina, linea, pings, centinela)
                self.processed += 1
                self._avanzar(commit)
        return self._cerrar(errores_region=errores_region)

    def _procesar_servidor(self, maquina, linea, pings, centinela):
        """SSM + discovery de UN servidor. Nunca propaga: la corrida sigue."""
        Log = self.env["primate.cloud.operation.log"]
        if linea.sync_result == "created":
            self.created_count += 1
        else:
            self.updated_count += 1
        ping = pings.get(maquina.aws_instance_id)
        online = ping == "Online"
        maquina.write({"ssm_state": "online" if online else "unreachable",
                       "ssm_checked_at": fields.Datetime.now()})
        vals = {"ssm_state": maquina.ssm_state, "ping_status": ping or ""}
        if not online:
            self.unreachable_count += 1
            vals["manual_steps"] = (PASOS_CONEXION_PERDIDA % ping if ping
                                    else PASOS_SIN_AGENTE)
            vals["discovery_state"] = "not_run"
            linea.write(vals)
            return
        self.online_count += 1
        if not self.discover:
            vals["discovery_state"] = "not_run"
        elif maquina.management_state == "provisioned":
            # Lo creó PCM: sus instancias ya están registradas. Correr el
            # discovery sólo podría duplicarlas con otro slug.
            vals.update({"discovery_state": "skipped",
                         "detail": _("Servidor creado por PCM: sus instancias "
                                     "ya están registradas.")})
        else:
            try:
                with self.env.cr.savepoint():
                    datos = maquina._run_discovery()
                    # Es EL sync de lo importado: el único escritor admitido
                    # de su infraestructura en PCM (regla 2026-10-22).
                    sync = maquina.with_context(pcm_import_sync=True)
                    res = sync._aplicar_discovery(datos, centinela)
                    if datos.get("_twin"):
                        sync._twin_apply(datos["_twin"])
                    elif res["estado"] == "ok":
                        # Odoo detectado pero el gemelo no llegó entero:
                        # marcado y contado, nunca en silencio.
                        maquina._twin_marcar("incomplete",
                                             datos.get("_twin_error"))
                        self.twin_incomplete_count += 1
                        res["detalle"] = "%s%s" % (
                            (res["detalle"] + " · ") if res["detalle"] else "",
                            _("Gemelo INCOMPLETO: %s") % (
                                datos.get("_twin_error") or "?"))
                vals.update({
                    "discovery_state": ("ok" if res["estado"] == "ok"
                                        else "unrecognized"),
                    "instances_found": res["instancias"],
                    "databases_found": res["bases"],
                    "detail": res["detalle"],
                })
                if res["estado"] == "ok":
                    self.discovered_count += 1
                else:
                    self.unrecognized_count += 1
                Log.log_operation(
                    "instance_discover", record=maquina,
                    name=_("Discovery: %s") % maquina.name,
                    result="success" if res["estado"] == "ok" else "partial",
                    error_message=res["detalle"] or None)
            except Exception as error:  # noqa: BLE001 - un servidor no aborta
                self.error_count += 1
                vals.update({"discovery_state": "error",
                             "detail": str(error)[:2000]})
                Log.log_operation(
                    "instance_discover", record=maquina,
                    name=_("Discovery: %s") % maquina.name,
                    result="failed", error_message=str(error))
        linea.write(vals)

    def _cerrar(self, failed=False, detalle=None, errores_region=None):
        """Cierra la corrida: reporte, bitácora, toast. Devuelve si salió bien."""
        errores_region = errores_region or []
        partes = [_(
            "Servidores: %(t)s · nuevos %(n)s · actualizados %(u)s · con "
            "acceso SSM %(on)s · sin acceso SSM %(off)s · con Odoo detectado "
            "%(d)s · no reconocidos %(nr)s · con error %(e)s · gemelo "
            "incompleto %(gi)s",
            t=self.total_servers, n=self.created_count, u=self.updated_count,
            on=self.online_count, off=self.unreachable_count,
            d=self.discovered_count, nr=self.unrecognized_count,
            e=self.error_count, gi=self.twin_incomplete_count)]
        if detalle:
            partes.append(_("Error: %s") % detalle)
        if errores_region:
            partes.append(_("Regiones con problemas:\n  ") +
                          "\n  ".join(errores_region))
        sin_acceso = self.line_ids.filtered(lambda l: l.ssm_state == "unreachable")
        if sin_acceso:
            partes.append(_("SIN ACCESO SSM — pasos manuales por servidor:"))
            for linea in sin_acceso:
                partes.append("• %s (%s)\n%s" % (
                    linea.name, linea.aws_instance_id, linea.manual_steps or ""))
        incompletos = self.line_ids.filtered(
            lambda l: "Gemelo INCOMPLETO" in (l.detail or ""))
        if incompletos:
            partes.append(_("GEMELO INCOMPLETO — capturarlo por ssh "
                            "(infra/gemelo_cloudpepper.sh):"))
            for linea in incompletos:
                partes.append("• %s\n%s" % (linea.name, linea.detail or ""))
        no_rec = self.line_ids.filtered(
            lambda l: l.discovery_state == "unrecognized")
        if no_rec:
            partes.append(_("NO RECONOCIDOS — lo que se encontró:"))
            for linea in no_rec:
                partes.append("• %s\n%s" % (linea.name, linea.detail or ""))

        if failed:
            state, result = "failed", "failed"
        elif self.error_count or errores_region:
            state, result = "done", "partial"
        else:
            state, result = "done", "success"
        self.write({"state": state, "ended_at": fields.Datetime.now(),
                    "report": "\n\n".join(partes),
                    "progress": 100.0 if not failed else self.progress})
        self.env["primate.cloud.operation.log"].log_operation(
            "server_import", record=self,
            name=_("Importar servidores: %s") % self.account_id.name,
            result=result, error_message=self.report)
        bus.toast(self.env, partes[0], title=_("Importación terminada")
                  if not failed else _("Importación fallida"),
                  ntype={"success": "success", "partial": "warning",
                         "failed": "danger"}[result],
                  sticky=result != "success", reload=True)
        return not failed


class PrimateCloudServerImportLine(models.Model):
    """Un servidor dentro de una corrida de importación."""

    _name = "primate.cloud.server.import.line"
    _description = "Servidor de una importación"
    _order = "id"

    import_id = fields.Many2one(
        "primate.cloud.server.import", string="Corrida", required=True,
        ondelete="cascade", index=True)
    ec2_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Servidor", ondelete="set null")
    aws_instance_id = fields.Char(string="Instance ID")
    name = fields.Char(string="Nombre")
    region = fields.Char(string="Región")
    instance_state = fields.Char(string="Estado en AWS")
    sync_result = fields.Selection(
        [("created", "Nuevo"), ("updated", "Actualizado")], string="Alta")
    ssm_state = fields.Selection(
        [("unknown", "Sin verificar"), ("online", "Con acceso SSM"),
         ("unreachable", "Sin acceso SSM")], string="SSM", default="unknown")
    ping_status = fields.Char(string="Ping SSM")
    discovery_state = fields.Selection(
        [("not_run", "No corrió"), ("ok", "Odoo detectado"),
         ("unrecognized", "No reconocido"), ("skipped", "Omitido"),
         ("error", "Error")], string="Discovery", default="not_run")
    instances_found = fields.Integer(string="Instancias")
    databases_found = fields.Integer(string="Bases")
    detail = fields.Text(string="Detalle")
    manual_steps = fields.Text(string="Pasos manuales")
