# -*- coding: utf-8 -*-
"""F2: modo de gestión de los servidores EC2 ya registrados.

El campo ``management_state`` nace con default ``provisioned``, que es lo
correcto para los servidores que PCM creó. Pero en la base ya hay EC2 que
entraron por el **sync de inventario** —servidores que PCM no creó— y esos son
exactamente el caso que el modo de solo lectura viene a proteger: dejarlos
gestionables sin que nadie los haya adoptado mantendría abierto el riesgo que
el feature cierra.

El modelo ya distingue las dos procedencias con ``provisioned_by_pcm``
(``_register_provisioned`` lo pone en True; el sync lo deja en False), así que
el mapeo es directo y no hay que adivinar:

    provisioned_by_pcm = True   →  provisioned        (gestión normal)
    provisioned_by_pcm = False  →  adopted_readonly   (solo lectura)

Efecto consciente: si alguien venía operando desde PCM un servidor que había
entrado por sync, esa acción empieza a fallar con un error que explica cómo
habilitarla (una acción admin-only en la ficha del servidor). Es el precio de
que la importación sea inofensiva por construcción, y fue una decisión tomada
a la vista del efecto.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Pone en solo lectura los EC2 que PCM no creó."""
    # El campo ya existe (lo creó el update del módulo con su default), así que
    # acá sólo se corrige la procedencia.
    cr.execute("""
        UPDATE primate_cloud_ec2_instance
           SET management_state = 'adopted_readonly'
         WHERE COALESCE(provisioned_by_pcm, FALSE) = FALSE
           AND management_state = 'provisioned'
    """)
    adoptados = cr.rowcount
    cr.execute("""
        SELECT COUNT(*) FROM primate_cloud_ec2_instance
         WHERE management_state = 'provisioned'
    """)
    provisionados = cr.fetchone()[0]
    _logger.info(
        "F2 modo de gestión: %s servidor(es) pasaron a 'adopted_readonly' "
        "(no los creó PCM) y %s quedan en 'provisioned'. Los adoptados sólo "
        "admiten lectura hasta que un admin habilite la gestión.",
        adoptados, provisionados)
