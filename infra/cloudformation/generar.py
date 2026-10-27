#!/usr/bin/env python3
"""Genera las plantillas de CloudFormation de los prerrequisitos de PCM.

Fuente de verdad ÚNICA (no se editan las plantillas a mano):
  - docs/iam/pcm-prod-readonly.json   → managed policy pcm-prod-readonly
  - docs/iam/pcm-prod-provision.json  → managed policy pcm-prod-provision
  - primate_cloud_manager/data/ssm/pcm-readonly-discovery.json → documento SSM

Transformaciones al generar:
  - ``<ACCOUNT_ID>`` → ``${AWS::AccountId}`` y ``<SUFIJO>`` → ``${Sufijo}``
    (Fn::Sub): nadie reemplaza nada a mano. ``Sufijo`` es un parámetro
    OPCIONAL, vacío por defecto (producción no lo completa); sirve para
    desplegar el stack en una cuenta que ya tiene recursos con estos nombres
    (la sandbox), sin tocarlos.
  - El statement de Route 53 (``DnsSoloNombresDePCMEnLaZonaReal``) queda
    CONDICIONAL: sólo existe si se completan los parámetros ``ZonaDns`` (Hosted
    Zone ID) y ``NombresDns`` (nombres permitidos). Vacíos = PCM no toca Route
    53 y el dominio se agrega a mano. Dos reglas exigen los dos juntos.
  - Se QUITA el statement opcional del bucket de salida del discovery (la
    plantilla no crea ese bucket). Quitar un Allow sólo reduce permisos.

Uso:
  python3 infra/cloudformation/generar.py           # escribe las plantillas
  python3 infra/cloudformation/generar.py --check   # falla si no están al día
"""
import json
import os
import sys

import yaml

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AQUI = os.path.dirname(os.path.abspath(__file__))
POLICY_RO = os.path.join(RAIZ, "docs/iam/pcm-prod-readonly.json")
POLICY_PROV = os.path.join(RAIZ, "docs/iam/pcm-prod-provision.json")
DOC_SSM = os.path.join(
    RAIZ, "primate_cloud_manager/data/ssm/pcm-readonly-discovery.json")
SALIDA_PRE = os.path.join(AQUI, "pcm-prerequisitos.yaml")
SALIDA_SSM = os.path.join(AQUI, "pcm-ssm-readonly.yaml")

# Statements que no van en la plantilla (dependen de recursos que no crea).
QUITAR = {
    "pcm-prod-readonly": {"SalidaDelDiscoveryPorS3Opcional"},
    "pcm-prod-provision": set(),
}
# Statement de Route 53: va condicionado a los parámetros ZonaDns/NombresDns.
SID_DNS = "DnsSoloNombresDePCMEnLaZonaReal"
CONDICION_DNS = "HayZonaDns"
REGION = "us-east-1"
BUCKET = "pcm-dumps-${AWS::AccountId}${Sufijo}"
# Todos los buckets de PCM de ESTA cuenta: pcm-<uso>-<cuenta>[sufijo].
BUCKETS_PCM = "pcm-*-${AWS::AccountId}${Sufijo}"


def _sub(valor):
    """Strings con <ACCOUNT_ID> → {Fn::Sub: ...}; el resto queda igual."""
    if isinstance(valor, dict):
        return {k: _sub(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_sub(v) for v in valor]
    if isinstance(valor, str) and ("<ACCOUNT_ID>" in valor or "<SUFIJO>" in valor):
        if "${" in valor:
            raise SystemExit("no se puede Fn::Sub sobre %r" % valor)
        return {"Fn::Sub": valor.replace("<ACCOUNT_ID>", "${AWS::AccountId}")
                .replace("<SUFIJO>", "${Sufijo}")}
    return valor


def _dns_condicional(stmt):
    """El statement de Route 53 con los parámetros de la plantilla, envuelto
    en Fn::If: sin ZonaDns no existe (AWS::NoValue)."""
    texto = json.dumps(stmt)
    if (texto.count("<ZONE_ID>") != 1
            or texto.count("<NOMBRES_DNS_DE_PCM>") != 1):
        raise SystemExit("%s: se esperaba un <ZONE_ID> y un "
                         "<NOMBRES_DNS_DE_PCM>" % SID_DNS)
    stmt = json.loads(texto)
    if stmt["Resource"] != "arn:aws:route53:::hostedzone/<ZONE_ID>":
        raise SystemExit("%s: Resource inesperado %r" % (SID_DNS, stmt["Resource"]))
    stmt["Resource"] = {"Fn::Sub": "arn:aws:route53:::hostedzone/${ZonaDns}"}
    nombres = stmt["Condition"]["ForAllValues:StringLike"]
    clave = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
    if nombres.get(clave) != ["<NOMBRES_DNS_DE_PCM>"]:
        raise SystemExit("%s: condición de nombres inesperada" % SID_DNS)
    nombres[clave] = {"Fn::Split": [",", {"Ref": "NombresDns"}]}
    return {"Fn::If": [CONDICION_DNS, stmt, {"Ref": "AWS::NoValue"}]}


def _policy(ruta, nombre):
    with open(ruta) as fh:
        doc = json.load(fh)
    statements = []
    for stmt in doc["Statement"]:
        if stmt["Sid"] in QUITAR[nombre]:
            continue
        statements.append(_dns_condicional(stmt) if stmt["Sid"] == SID_DNS
                          else stmt)
    doc["Statement"] = statements
    texto = json.dumps(doc)
    sobrante = [p for p in ("<ZONE_ID>", "<NOMBRES_DNS_DE_PCM>",
                            "<BUCKET_SALIDA_DISCOVERY>") if p in texto]
    if sobrante:
        raise SystemExit("%s: quedan placeholders %s" % (nombre, sobrante))
    return _sub(doc)


PARAMETRO_SUFIJO = {
    "Sufijo": {
        "Type": "String",
        "Default": "",
        "AllowedPattern": "^(-[a-z0-9]{1,12})?$",
        "Description": (
            "OPCIONAL. Vacío en producción. Se agrega al final de todos los "
            "nombres (p. ej. -cfn) para desplegar en una cuenta que ya tiene "
            "recursos con estos nombres, sin tocarlos."),
    },
}


PARAMETROS_DNS = {
    "ZonaDns": {
        "Type": "String",
        "Default": "",
        "AllowedPattern": "^(Z[A-Z0-9]+)?$",
        "Description": (
            "OPCIONAL. Hosted Zone ID de Route 53 donde PCM puede crear los "
            "registros A de sus servidores (ej. Z0123456789ABC). Vacío = PCM "
            "no toca Route 53: el dominio se agrega a mano en la consola. Si "
            "se completa, NombresDns es obligatorio."),
    },
    "NombresDns": {
        "Type": "String",
        "Default": "",
        "AllowedPattern": "^([a-z0-9*][a-z0-9.*-]*(,[a-z0-9*][a-z0-9.*-]*)*)?$",
        "Description": (
            "OPCIONAL (obligatorio si hay ZonaDns). Nombres que PCM puede "
            "crear/actualizar/borrar en esa zona, en minúsculas, separados por "
            "coma, sin punto final; admite comodín (ej. *.erp.ejemplo.com). "
            "Sólo registros A."),
    },
}

REGLAS_DNS = {
    "NombresDnsSiHayZona": {
        "RuleCondition": {"Fn::Not": [{"Fn::Equals": [{"Ref": "ZonaDns"}, ""]}]},
        "Assertions": [{
            "Assert": {"Fn::Not": [{"Fn::Equals": [{"Ref": "NombresDns"}, ""]}]},
            "AssertDescription": (
                "Con ZonaDns hay que completar NombresDns: los nombres que "
                "PCM puede tocar en esa zona (sin ellos no se otorga nada)."),
        }],
    },
    "ZonaDnsSiHayNombres": {
        "RuleCondition": {"Fn::Not": [{"Fn::Equals": [{"Ref": "NombresDns"}, ""]}]},
        "Assertions": [{
            "Assert": {"Fn::Not": [{"Fn::Equals": [{"Ref": "ZonaDns"}, ""]}]},
            "AssertDescription": (
                "NombresDns sin ZonaDns no sirve: completá los dos o dejá los "
                "dos vacíos."),
        }],
    },
}


def _documento():
    with open(DOC_SSM) as fh:
        contenido = json.load(fh)
    return {
        "Type": "AWS::SSM::Document",
        "Properties": {
            "Name": {"Fn::Sub": "pcm-readonly-discovery${Sufijo}"},
            "DocumentType": "Command",
            "DocumentFormat": "JSON",
            "UpdateMethod": "NewVersion",
            "Content": contenido,
        },
    }


def prerrequisitos():
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "PCM - prerrequisitos de la cuenta productiva: documento SSM de solo "
            "lectura, rol/instance profile pcm-ssm-role, bucket pcm-dumps-<cuenta>, "
            "policies pcm-prod-readonly / pcm-prod-provision y sus dos usuarios "
            "IAM (sin consola, sin access keys). Generada por "
            "infra/cloudformation/generar.py: no editar a mano."),
        "Parameters": dict(PARAMETRO_SUFIJO, **PARAMETROS_DNS),
        "Conditions": {
            CONDICION_DNS: {"Fn::Not": [{"Fn::Equals": [{"Ref": "ZonaDns"}, ""]}]},
        },
        "Rules": {
            **REGLAS_DNS,
            "SoloEnUsEast1": {
                "Assertions": [{
                    "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, REGION]},
                    "AssertDescription": (
                        "Esta plantilla se despliega en us-east-1 (el bucket del "
                        "dump va en la misma región que el servidor). Para otras "
                        "regiones con servidores: pcm-ssm-readonly.yaml."),
                }],
            },
        },
        "Resources": {
            "DocumentoLecturaPCM": _documento(),
            "RolSSM": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Fn::Sub": "pcm-ssm-role${Sufijo}"},
                    "Description": "PCM: rol de las EC2 que monta PCM (agente SSM + "
                                   "subir/bajar dumps en buckets pcm-*).",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }],
                    },
                    "ManagedPolicyArns": [{"Fn::Sub": (
                        "arn:${AWS::Partition}:iam::aws:policy/"
                        "AmazonSSMManagedInstanceCore")}],
                    "Policies": [{
                        "PolicyName": "PcmS3BackupObjects",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{
                                "Sid": "ObjetosDeLosBucketsDePCMDeEstaCuenta",
                                "Effect": "Allow",
                                "Action": ["s3:PutObject", "s3:GetObject"],
                                "Resource": {"Fn::Sub": (
                                    "arn:${AWS::Partition}:s3:::" + BUCKETS_PCM
                                    + "/*")},
                                "Condition": {"StringEquals": {
                                    "aws:ResourceAccount": {
                                        "Ref": "AWS::AccountId"}}},
                            }],
                        },
                    }],
                },
            },
            "PerfilSSM": {
                "Type": "AWS::IAM::InstanceProfile",
                "Properties": {
                    "InstanceProfileName": {"Fn::Sub": "pcm-ssm-role${Sufijo}"},
                    "Roles": [{"Ref": "RolSSM"}],
                },
            },
            "BucketDumps": {
                "Type": "AWS::S3::Bucket",
                # Un bucket con dumps no se borra con el stack.
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "BucketName": {"Fn::Sub": BUCKET},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "OwnershipControls": {
                        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [{
                            "ServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "AES256"}}]},
                    # Sin VersioningConfiguration: sin versionado (lo que PCM
                    # borra al terminar un restore, se borra de verdad).
                    "LifecycleConfiguration": {
                        "Rules": [{
                            "Id": "incoming-7-dias",
                            "Status": "Enabled",
                            "Prefix": "incoming/",
                            "ExpirationInDays": 7,
                            "AbortIncompleteMultipartUpload": {
                                "DaysAfterInitiation": 1},
                        }],
                    },
                },
            },
            "BucketDumpsSoloTLS": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": {"Ref": "BucketDumps"},
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Sid": "SoloConTLS",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:*",
                            "Resource": [
                                {"Fn::Sub": "arn:${AWS::Partition}:s3:::" + BUCKET},
                                {"Fn::Sub": "arn:${AWS::Partition}:s3:::" + BUCKET
                                 + "/*"}],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        }],
                    },
                },
            },
            "PolicyReadonly": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": {"Fn::Sub": "pcm-prod-readonly${Sufijo}"},
                    "Description": "PCM: inventario, importacion y gemelo de SOLO LECTURA.",
                    "PolicyDocument": _policy(POLICY_RO, "pcm-prod-readonly"),
                },
            },
            "PolicyProvision": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": {"Fn::Sub": "pcm-prod-provision${Sufijo}"},
                    "Description": "PCM: montar SOLO lo nuevo (etiquetado primate:managed_by=pcm).",
                    "PolicyDocument": _policy(POLICY_PROV, "pcm-prod-provision"),
                },
            },
            "UsuarioReadonly": {
                "Type": "AWS::IAM::User",
                "Properties": {
                    "UserName": {"Fn::Sub": "pcm-prod-ro${Sufijo}"},
                    "ManagedPolicyArns": [{"Ref": "PolicyReadonly"}],
                },
            },
            "UsuarioProvision": {
                "Type": "AWS::IAM::User",
                "Properties": {
                    "UserName": {"Fn::Sub": "pcm-prod-prov${Sufijo}"},
                    "ManagedPolicyArns": [{"Ref": "PolicyProvision"}],
                },
            },
        },
        "Outputs": {
            "Bucket": {"Value": {"Ref": "BucketDumps"}},
            "DocumentoSSM": {"Value": {"Ref": "DocumentoLecturaPCM"}},
            "PerfilDeInstancia": {"Value": {"Ref": "PerfilSSM"}},
            "UsuarioReadonly": {"Value": {"Ref": "UsuarioReadonly"}},
            "UsuarioProvision": {"Value": {"Ref": "UsuarioProvision"}},
        },
    }


def ssm_readonly():
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "PCM - documento SSM de solo lectura pcm-readonly-discovery, para "
            "regiones con servidores a importar distintas de us-east-1. "
            "Generada por infra/cloudformation/generar.py: no editar a mano."),
        "Parameters": PARAMETRO_SUFIJO,
        "Resources": {"DocumentoLecturaPCM": _documento()},
        "Outputs": {"DocumentoSSM": {"Value": {"Ref": "DocumentoLecturaPCM"}}},
    }


CABECERA_PRE = """\
# =============================================================================
# pcm-prerequisitos.yaml — prerrequisitos de la cuenta AWS para PCM
# (Primate Cloud Manager, el módulo de Odoo con el que Primate gobierna sus
# servidores Odoo en AWS).
#
# QUÉ CREA (y nada más; no modifica ni importa ningún recurso existente):
#   1. Documento SSM «pcm-readonly-discovery»: el ÚNICO comando que PCM puede
#      correr en un servidor que NO montó PCM. Es un script fijo, sin
#      parámetros, de solo lectura (inventario de los Odoo del servidor).
#   2. Rol e instance profile «pcm-ssm-role»: el rol que llevan las EC2 que
#      SÍ monta PCM (agente SSM + subir/bajar dumps y backups en los buckets
#      pcm-<uso>-<cuenta> de ESTA cuenta). Limitación conocida: es UN rol
#      para todos los servidores de PCM, así que cualquiera de ellos puede
#      leer los dumps y backups de los demás (ver README_INCO.md).
#   3. Bucket «pcm-dumps-<cuenta>» (us-east-1): dumps a restaurar. Privado,
#      Block Public Access completo, cifrado SSE-S3, sin versionado, solo TLS;
#      lo de incoming/ se borra solo a los 7 días.
#   4. Managed policies «pcm-prod-readonly» y «pcm-prod-provision».
#   5. Usuarios IAM «pcm-prod-ro» y «pcm-prod-prov», cada uno con SOLO su
#      policy, sin acceso a la consola y SIN access keys (las crea un humano
#      después, en la consola, y las carga en PCM).
#
# EL MODELO DE SEGURIDAD, EN DOS IDEAS:
#   - La etiqueta «primate:managed_by = pcm» es la LLAVE: PCM sólo puede operar
#     recursos que la tengan, y la etiqueta sólo se puede poner EN EL MISMO
#     LLAMADO que crea el recurso. Nadie (ni estos usuarios) puede ponérsela o
#     sacársela a algo que ya existe: hay un Deny explícito.
#   - Dos usuarios separados a propósito. Juntos en uno, IAM evalúa SendCommand
#     por recurso (documento e instancia por separado) y la combinación de las
#     dos policies daría un shell arbitrario sobre un servidor ajeno.
#       pcm-prod-ro   → leer el inventario y correr SOLO el documento de lectura.
#       pcm-prod-prov → montar servidores NUEVOS (siempre con la llave) y
#                       operar sólo esos.
#
# CÓMO SE DESPLIEGA: CloudFormation → Create stack → Upload a template file →
# dejar «Sufijo» VACÍO (todo sale de los pseudo-parámetros de la cuenta) →
# «ZonaDns» y «NombresDns»: vacíos si PCM NO va a crear registros DNS (el
# dominio se agrega a mano en Route 53); completos (los dos) para que PCM cree
# SÓLO registros A con esos nombres en esa zona → tildar
# «I acknowledge … IAM resources with custom names» (CAPABILITY_NAMED_IAM).
# Solo en us-east-1 (regla «SoloEnUsEast1»). Para otras regiones con
# servidores a leer: pcm-ssm-readonly.yaml (sólo el documento SSM).
# Verificación posterior, de solo lectura: verificar.sh en CloudShell.
#
# Esta plantilla se GENERA (generar.py) desde las policies y el documento
# versionados del repo de PCM: no se edita a mano.
# =============================================================================
"""

CABECERA_SSM = """\
# =============================================================================
# pcm-ssm-readonly.yaml — documento SSM de SOLO LECTURA de PCM, para una
# región distinta de us-east-1 donde haya servidores a inventariar.
#
# Crea únicamente el documento «pcm-readonly-discovery»: un script fijo, sin
# parámetros, que lista los Odoo del servidor (conf enmascarado, addons_path,
# servicio systemd, repos git y su estado, versión de PostgreSQL y tamaño de
# la base, filestore, nginx y certificados, crons) y lo devuelve por la salida
# estándar en una línea codificada. NO escribe nada en el servidor: ni
# archivos, ni temporales, ni índices de git, ni .pyc.
#
# Es el único SendCommand que la policy «pcm-prod-readonly» permite: aunque
# esas credenciales se filtraran, no se podría correr otra cosa.
# Se despliega igual que pcm-prerequisitos.yaml (sin parámetros), una vez por
# región. Generada por generar.py desde el repo de PCM: no editar a mano.
# =============================================================================
"""

COMENTARIO_RECURSO = {
    "DocumentoLecturaPCM": [
        "Documento SSM de SOLO LECTURA. El script va en runCommand, línea por",
        "línea: primero el inventario (PCM_DISCOVERY_JSON) y después el gemelo",
        "completo (PCM_TWIN_B64 + PCM_TWIN_OK). Sin parámetros: no hay forma de",
        "pasarle un comando. UpdateMethod NewVersion: actualizar crea versión."],
    "RolSSM": [
        "Rol de las EC2 que monta PCM. AmazonSSMManagedInstanceCore habilita el",
        "agente SSM (PCM nunca usa SSH); la inline permite subir/bajar dumps y",
        "backups SÓLO en los buckets pcm-<uso>-<cuenta> de esta cuenta",
        "(aws:ResourceAccount). No lo usan los servidores importados.",
        "LIMITACIÓN: un solo rol para todos los servidores de PCM."],
    "PerfilSSM": [
        "Instance profile con el mismo nombre del rol (es lo que se le pasa a",
        "RunInstances; pcm-prod-prov sólo puede pasar este rol)."],
    "BucketDumps": [
        "Bucket de dumps a restaurar. Retain: borrar el stack NO borra el bucket.",
        "Sin VersioningConfiguration = sin versionado (lo que PCM borra al",
        "terminar un restore se borra de verdad). incoming/ expira a los 7 días",
        "y los multipart incompletos a 1 día."],
    "BucketDumpsSoloTLS": [
        "Rechaza cualquier acceso al bucket que no venga por TLS."],
    "PolicyReadonly": [
        "pcm-prod-readonly: leer el inventario (EC2, RDS, Route 53, CloudWatch,",
        "costos) y correr SÓLO el documento de lectura. Los Deny explícitos son",
        "una segunda red: aunque alguien le sumara otra policy a este usuario,",
        "lo que no es de PCM sigue intocable."],
    "PolicyProvision": [
        "pcm-prod-provision: montar servidores NUEVOS (siempre con la llave) y",
        "operar sólo esos. Todo Allow de escritura exige la etiqueta",
        "primate:managed_by=pcm en el pedido o en el recurso."],
    "UsuarioReadonly": [
        "Usuario de PCM para leer. Sin consola (sin LoginProfile) y sin access",
        "keys: las crea un humano en la consola y las carga en PCM."],
    "UsuarioProvision": [
        "Usuario de PCM para montar. Ídem: sin consola y sin access keys."],
}

COMENTARIO_SID = {
    "LecturaInventario": "Lecturas del inventario (Describe/List/Get). Nada escribe.",
    "DiscoverySoloConElDocumentoPropio":
        "SendCommand SÓLO con el documento propio; ningún otro documento está permitido.",
    "MaquinasNoCreadasPorPCMIntocables":
        "Deny: ninguna acción de ciclo de vida, red ni discos sobre lo que no tiene la llave.",
    "EtiquetasDeLoAjenoIntocables":
        "Deny: no etiquetar recursos ajenos (salvo dentro de una creación).",
    "BasesRdsNoCreadasPorPCMIntocables": "Deny: las RDS ajenas no se tocan.",
    "SinSesionesNiAutomatizacionesNiDocumentos":
        "Deny: sin sesiones, automatizaciones, documentos, parámetros, zonas ni IAM.",
    "NadieTocaLaLlaveDePropiedadEnRecursosExistentes":
        "Deny: la llave primate:managed_by (cualquier mayúscula) sólo nace al crear.",
    "NuncaBorrarEtiquetas":
        "Deny: ningún DeleteTags (uno sin claves borraría la llave sin nombrarla).",
    "LecturasParaOperarLoPropio": "Lecturas necesarias para montar y operar. Nada escribe.",
    "CrearInstanciaSoloEtiquetadaPorPCM":
        "RunInstances sólo si la instancia y su volumen nacen con la llave.",
    "RecursosAuxiliaresDeRunInstances":
        "Lo que RunInstances usa sin crear (AMI, subred, interfaz, SG, key pair).",
    "EtiquetarSoloAlCrear":
        "CreateTags sólo dentro de la creación (ec2:CreateAction).",
    "CicloDeVidaSoloDeLoDePCM": "Arrancar/parar/reiniciar/terminar SÓLO lo que tiene la llave.",
    "SecurityGroupPropioEtiquetado": "Crear security groups sólo con la llave.",
    "SecurityGroupEnLaVpc": "(El SG nuevo se crea dentro de una VPC existente.)",
    "ReglasSoloEnElSecurityGroupDePCM": "Abrir puertos sólo en SGs con la llave.",
    "ElasticIpPropia": "Reservar Elastic IPs sólo con la llave.",
    "AsociarEipSoloEntreRecursosDePCM": "Asociar EIP sólo entre recursos con la llave.",
    "LiberarEipSoloDePCM":
        "Liberar sólo Elastic IPs con la llave (al terminar un servidor de PCM).",
    "DnsSoloNombresDePCMEnLaZonaReal":
        "Sólo con ZonaDns/NombresDns: registros A con esos nombres en esa zona.",
    "PasarSoloElRolDeSSMDePCM": "Sólo puede pasarle a una EC2 el rol pcm-ssm-role.",
    "ShellDeInstalacionDocumento":
        "El documento de shell para instalar Odoo (la instancia se limita abajo).",
    "ShellDeInstalacionSoloEnMaquinasDePCM":
        "...y sólo sobre instancias con la llave: nunca sobre un servidor ajeno.",
    "AmiPublicaDeCanonical":
        "Sólo el parámetro PÚBLICO de Canonical con la AMI de Ubuntu (nada de la cuenta).",
    "CrearBucketsDePCM": "Crear buckets pcm-<uso>-<esta cuenta> (backups).",
    "BucketsDePCMDeEstaCuenta":
        "Listar y lifecycle SÓLO en buckets pcm-<uso>-<cuenta> que sean de ESTA cuenta.",
    "ObjetosDeLosBucketsDePCM":
        "Leer y borrar objetos SÓLO en esos buckets de esta cuenta (aws:ResourceAccount).",
    "SubirDumpsSoloAlBucketDePCM":
        "Subir dumps SÓLO a incoming/ del bucket pcm-dumps-<cuenta>, de esta cuenta.",
}


def _comentar(texto, comentarios_recurso):
    """Inserta los comentarios antes de cada recurso y de cada statement."""
    import re
    salida = []
    en_recursos = False
    for linea in texto.splitlines():
        if linea == "Resources:":
            en_recursos = True
        elif linea and not linea.startswith(" "):
            en_recursos = False
        m = re.match(r"^  (\w+):$", linea)
        if en_recursos and m and m.group(1) in comentarios_recurso:
            salida.append("")
            salida.extend("  # " + l for l in comentarios_recurso[m.group(1)])
        m = re.match(r"^(\s*)- Sid: (\w+)$", linea)
        if m and m.group(2) in COMENTARIO_SID:
            salida.append("%s# %s" % (m.group(1), COMENTARIO_SID[m.group(2)]))
        salida.append(linea)
    return "\n".join(salida) + "\n"


def _yaml(datos, cabecera):
    texto = yaml.safe_dump(datos, sort_keys=False, allow_unicode=True, width=100)
    return cabecera + _comentar(texto, COMENTARIO_RECURSO)


def main():
    check = "--check" in sys.argv[1:]
    desactualizadas = []
    for ruta, datos, cabecera in ((SALIDA_PRE, prerrequisitos(), CABECERA_PRE),
                                  (SALIDA_SSM, ssm_readonly(), CABECERA_SSM)):
        texto = _yaml(datos, cabecera)
        if check:
            actual = open(ruta).read() if os.path.exists(ruta) else ""
            if actual != texto:
                desactualizadas.append(os.path.relpath(ruta, RAIZ))
        else:
            with open(ruta, "w") as fh:
                fh.write(texto)
            print("escrita %s (%d bytes)" % (os.path.relpath(ruta, RAIZ),
                                            len(texto.encode())))
        # El tope de 51.200 bytes es SÓLO de --template-body (CLI). Por
        # consola, CloudFormation sube la plantilla a su bucket automático
        # (cf-templates-…) y el tope es 1 MB: se advierte, no se corta.
        peso = len(texto.encode())
        if peso > 1024 * 1024:
            raise SystemExit("%s pesa %d bytes: supera 1 MB, el tope de "
                             "CloudFormation" % (os.path.basename(ruta), peso))
        if peso > 51200:
            print("AVISO: %s pesa %d bytes (> 51.200): por CLI hay que usar "
                  "--template-url; por consola se sube normal (bucket "
                  "cf-templates-… de CloudFormation, tope 1 MB)."
                  % (os.path.basename(ruta), peso))
    if desactualizadas:
        raise SystemExit("plantillas desactualizadas: %s — correr generar.py"
                         % ", ".join(desactualizadas))
    if check:
        print("plantillas al día")


if __name__ == "__main__":
    main()
