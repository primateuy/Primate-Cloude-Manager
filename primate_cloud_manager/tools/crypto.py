# -*- coding: utf-8 -*-
"""Helpers puros de cifrado simétrico para credenciales sensibles.

No dependen del ORM de Odoo: reciben la clave y el valor, y devuelven el
resultado. Esto los hace testeables de forma aislada. El cifrado usa Fernet
(AES-128 en modo CBC + HMAC-SHA256) de la librería `cryptography`.
"""
from cryptography.fernet import Fernet, InvalidToken


def generate_key():
    """Genera una clave Fernet nueva (urlsafe base64, 32 bytes).

    Returns:
        bytes: clave lista para usar con :func:`encrypt`/:func:`decrypt`.
    """
    return Fernet.generate_key()


def encrypt(key, plaintext):
    """Cifra un texto plano con la clave dada.

    Args:
        key (bytes|str): clave Fernet.
        plaintext (str|bool): valor a cifrar. Si es vacío/False devuelve False.

    Returns:
        str|bool: texto cifrado (str) o False si no había nada que cifrar.
    """
    if not plaintext:
        return False
    if isinstance(key, str):
        key = key.encode()
    token = Fernet(key).encrypt(plaintext.encode())
    return token.decode()


def decrypt(key, ciphertext):
    """Descifra un texto cifrado con la clave dada.

    Args:
        key (bytes|str): clave Fernet.
        ciphertext (str|bool): valor cifrado. Si es vacío/False devuelve False.

    Returns:
        str|bool: texto plano, o False si está vacío o la clave/token no es válido.
    """
    if not ciphertext:
        return False
    if isinstance(key, str):
        key = key.encode()
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        # Clave incorrecta, token corrupto o formato inválido: no exponer detalle.
        return False
