# -*- coding: utf-8 -*-
"""Adaptador de descubrimiento de red por región (Bloque B1 de auto-discovery).

Servicio Python **puro** (sin ORM, no escribe en BD). Descubre y VALIDA los
recursos de red que un aprovisionamiento necesita, en la cuenta/región dadas.
Read-only: solo ``describe_*`` + un ``RunInstances DryRun`` (que no crea nada) y
``iam:GetInstanceProfile``. La creación del security group vive en ``aws_ec2``
(mutación). Principio: **descubrir y validar el comportamiento real, no adivinar**
(un recurso que existe pero no se comporta como se necesita = no cumple).
"""
import logging

_logger = logging.getLogger(__name__)

# Nombre/tag convenido del SG gestionado por PCM (único por VPC → idempotencia).
MANAGED_SG_NAME = "pcm-managed"
MANAGED_TAG_KEY = "primate:managed_by"
MANAGED_TAG_VALUE = "pcm"
# Reglas mínimas de ingreso: web público (80 + 443). SSM NO necesita puerto
# entrante (agente saliente), así que no se abre SSH.
REQUIRED_INGRESS_PORTS = (80, 443)


class AwsDiscoveryService:
    """Descubrimiento + validación de red por región (composición sobre base)."""

    def __init__(self, base):
        self._base = base

    def _ec2(self, region):
        return self._base.get_client("ec2", region=region)

    # ------------------------------------------------------------------
    # Instance profile / rol
    # ------------------------------------------------------------------
    def discover_instance_profile(self, name):
        """¿Existe el instance profile ``name``? (iam:GetInstanceProfile).

        Returns:
            dict: ``{"exists": bool, "arn": str|None}``. ``NoSuchEntity`` →
            exists False (mensaje "no existe"); otros errores se propagan.
        """
        iam = self._base.get_client("iam")
        try:
            resp = iam.get_instance_profile(InstanceProfileName=name)
            profile = resp.get("InstanceProfile", {})
            return {"exists": True, "arn": profile.get("Arn")}
        except Exception as error:  # noqa: BLE001
            code = ""
            if hasattr(error, "response"):
                code = (error.response or {}).get("Error", {}).get("Code", "")
            if code == "NoSuchEntity":
                return {"exists": False, "arn": None}
            raise

    def check_profile_passable(self, region, image_id, instance_type,
                               profile_name, subnet_id, security_group_ids):
        """Valida el COMPORTAMIENTO: ¿RunInstances con este profile pasaría?

        DryRun no crea nada. Si el request sería válido, AWS devuelve el error
        ``DryRunOperation`` (= pasable). Cualquier otro error (p. ej. PassRole
        denegado, profile inválido) → no pasable, con el motivo real.

        Returns:
            dict: ``{"passable": bool, "reason": str}``.
        """
        params = {
            "ImageId": image_id, "InstanceType": instance_type,
            "MinCount": 1, "MaxCount": 1, "DryRun": True,
            "IamInstanceProfile": {"Name": profile_name},
        }
        if subnet_id:
            params["SubnetId"] = subnet_id
        if security_group_ids:
            params["SecurityGroupIds"] = security_group_ids
        try:
            self._ec2(region).run_instances(**params)
            return {"passable": True, "reason": ""}  # improbable sin DryRun error
        except Exception as error:  # noqa: BLE001
            code = ""
            if hasattr(error, "response"):
                code = (error.response or {}).get("Error", {}).get("Code", "")
            if code == "DryRunOperation":
                return {"passable": True, "reason": ""}
            return {"passable": False, "reason": str(error)}

    # ------------------------------------------------------------------
    # VPC / subnet / ruta a internet
    # ------------------------------------------------------------------
    def discover_vpc(self, region):
        """Elige la VPC de la región: primero la tagueada ``pcm-managed``, luego
        la default. Si hay ambigüedad real (ni tagueada ni default, o varias
        candidatas) NO adivina.

        Returns:
            dict: ``{"vpc_id": str|None, "is_default": bool, "ambiguous": bool,
            "candidates": int}``.
        """
        client = self._ec2(region)
        # 1) VPC tagueada pcm-managed (la que el admin marcó explícitamente).
        tagged = client.describe_vpcs(Filters=[
            {"Name": "tag:%s" % MANAGED_TAG_KEY, "Values": [MANAGED_TAG_VALUE]}
        ]).get("Vpcs", [])
        if len(tagged) == 1:
            return {"vpc_id": tagged[0]["VpcId"], "is_default": False,
                    "ambiguous": False, "candidates": 1}
        if len(tagged) > 1:
            return {"vpc_id": None, "is_default": False, "ambiguous": True,
                    "candidates": len(tagged)}
        # 2) VPC default de la región.
        default = client.describe_vpcs(Filters=[
            {"Name": "isDefault", "Values": ["true"]}]).get("Vpcs", [])
        if len(default) == 1:
            return {"vpc_id": default[0]["VpcId"], "is_default": True,
                    "ambiguous": False, "candidates": 1}
        # 3) Sin default y sin tag → ambigüedad (o ninguna).
        allvpcs = client.describe_vpcs().get("Vpcs", [])
        return {"vpc_id": None, "is_default": False,
                "ambiguous": len(allvpcs) > 1, "candidates": len(allvpcs)}

    def vpc_has_igw(self, region, vpc_id):
        """La VPC tiene salida a internet: un Internet Gateway attached."""
        igws = self._ec2(region).describe_internet_gateways(Filters=[
            {"Name": "attachment.vpc-id", "Values": [vpc_id]}
        ]).get("InternetGateways", [])
        for igw in igws:
            for att in igw.get("Attachments", []):
                if att.get("State") in ("available", "attached"):
                    return True
        return False

    def _subnet_has_igw_route(self, client, subnet_id, vpc_id):
        """La subnet enruta 0.0.0.0/0 a un IGW (pública de verdad, no solo con
        MapPublicIp). Usa su route table explícita o, si no tiene, la main."""
        # Route table asociada explícitamente a la subnet.
        rts = client.describe_route_tables(Filters=[
            {"Name": "association.subnet-id", "Values": [subnet_id]}
        ]).get("RouteTables", [])
        if not rts:
            # Sin asociación explícita → la main de la VPC.
            rts = client.describe_route_tables(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "association.main", "Values": ["true"]}
            ]).get("RouteTables", [])
        for rt in rts:
            for route in rt.get("Routes", []):
                if (route.get("DestinationCidrBlock") == "0.0.0.0/0"
                        and str(route.get("GatewayId", "")).startswith("igw-")):
                    return True
        return False

    def discover_public_subnet(self, region, vpc_id):
        """Subnet PÚBLICA de la VPC: auto-assign public IP + ruta a IGW.

        Returns:
            dict: ``{"subnet_id": str|None, "candidates": int}``.
        """
        client = self._ec2(region)
        subnets = client.describe_subnets(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets", [])
        public = [
            s for s in subnets
            if s.get("MapPublicIpOnLaunch")
            and self._subnet_has_igw_route(client, s["SubnetId"], vpc_id)
        ]
        if not public:
            return {"subnet_id": None, "candidates": 0}
        # Desempate estable: la primera por SubnetId (determinista).
        chosen = sorted(public, key=lambda s: s["SubnetId"])[0]
        return {"subnet_id": chosen["SubnetId"], "candidates": len(public)}

    # ------------------------------------------------------------------
    # Security group
    # ------------------------------------------------------------------
    def discover_managed_sg(self, region, vpc_id):
        """SG gestionado por PCM en la VPC (por nombre + tag). None si no hay."""
        sgs = self._ec2(region).describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [MANAGED_SG_NAME]},
            {"Name": "tag:%s" % MANAGED_TAG_KEY, "Values": [MANAGED_TAG_VALUE]},
        ]).get("SecurityGroups", [])
        return sgs[0] if sgs else None

    @staticmethod
    def validate_sg_rules(sg):
        """¿El SG tiene ingress 80+443 desde 0.0.0.0/0 y egress abierto?

        Returns:
            dict: ``{"ok": bool, "missing_ingress": [int], "egress_open": bool}``.
        """
        open_ingress = set()
        for perm in (sg or {}).get("IpPermissions", []):
            if perm.get("IpProtocol") not in ("tcp", "-1"):
                continue
            if not any(r.get("CidrIp") == "0.0.0.0/0"
                       for r in perm.get("IpRanges", [])):
                continue
            from_p, to_p = perm.get("FromPort"), perm.get("ToPort")
            for port in REQUIRED_INGRESS_PORTS:
                if from_p is not None and from_p <= port <= to_p:
                    open_ingress.add(port)
                elif perm.get("IpProtocol") == "-1":
                    open_ingress.add(port)
        missing = [p for p in REQUIRED_INGRESS_PORTS if p not in open_ingress]
        egress_open = any(
            e.get("IpProtocol") == "-1"
            and any(r.get("CidrIp") == "0.0.0.0/0" for r in e.get("IpRanges", []))
            for e in (sg or {}).get("IpPermissionsEgress", []))
        return {"ok": not missing and egress_open,
                "missing_ingress": missing, "egress_open": egress_open}

    # ------------------------------------------------------------------
    # Composición: foto de la región (read-only)
    # ------------------------------------------------------------------
    def discover_region(self, region, profile_name):
        """Descubre + valida toda la red de la región. NO crea nada.

        Returns:
            dict: estado por recurso + ``structural_missing`` (rol/vpc/subnet que
            faltan y NO se auto-crean) y ``sg`` (que sí se auto-crea si falta).
        """
        result = {"region": region, "structural_missing": []}
        # Rol
        prof = self.discover_instance_profile(profile_name)
        result["profile"] = prof
        if not prof["exists"]:
            result["structural_missing"].append("profile")
        # VPC
        vpc = self.discover_vpc(region)
        result["vpc"] = vpc
        if not vpc["vpc_id"]:
            result["structural_missing"].append("vpc")
        else:
            vpc["has_igw"] = self.vpc_has_igw(region, vpc["vpc_id"])
            if not vpc["has_igw"]:
                result["structural_missing"].append("vpc_igw")
        # Subnet (solo si hay VPC con IGW)
        result["subnet"] = {"subnet_id": None, "candidates": 0}
        if vpc.get("vpc_id") and vpc.get("has_igw"):
            subnet = self.discover_public_subnet(region, vpc["vpc_id"])
            result["subnet"] = subnet
            if not subnet["subnet_id"]:
                result["structural_missing"].append("subnet")
        # SG (se descubre; si falta lo crea aws_ec2, no es structural_missing)
        result["security_group"] = None
        if vpc.get("vpc_id"):
            sg = self.discover_managed_sg(region, vpc["vpc_id"])
            if sg:
                rules = self.validate_sg_rules(sg)
                result["security_group"] = {
                    "id": sg["GroupId"], "rules_ok": rules["ok"],
                    "missing_ingress": rules["missing_ingress"]}
        # "Listo" = nada structural falta (el SG faltante se auto-crea después).
        result["structural_ok"] = not result["structural_missing"]
        return result
