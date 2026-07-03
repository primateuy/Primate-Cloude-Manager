# -*- coding: utf-8 -*-
"""Extensión de res.partner: identificador estable de cliente (Fase 9, Opción A).

El "cliente" de la atribución de costos es el partner (no el proyecto): un
cliente puede tener varios proyectos, y atribuir por proyecto obligaría a sumar
a mano y se rompería al reorganizar. La clave estable del tag AWS
`primate:client_id` es este `pcm_ref`.

Se genera LAZY (solo cuando un partner se usa como cliente PCM), no con un
default: res.partner es un modelo enorme (todos los contactos de Odoo) y no
corresponde generarle un ref a cada contacto.
"""
import uuid

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    pcm_ref = fields.Char(
        string="Ref estable PCM", readonly=True, copy=False, index=True,
        help="Identificador inmutable del cliente para atribución de costos "
             "(tag primate:client_id). No cambia al renombrar el partner.",
    )

    def _ensure_pcm_ref(self):
        """Devuelve el pcm_ref del partner, generándolo la primera vez.

        Idempotente: si ya tiene ref, NO lo pisa (pisar cambiaría la clave de
        recursos ya taggeados y los huérfanaría — mismo principio que
        copy=False). Se escribe con sudo: es bookkeeping interno y el operador
        que aprovisiona puede no tener escritura sobre el partner.

        Returns:
            str: el pcm_ref (nuevo o existente).
        """
        self.ensure_one()
        if not self.pcm_ref:
            self.sudo().pcm_ref = "pcm_cli_" + uuid.uuid4().hex
        return self.pcm_ref
