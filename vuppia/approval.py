"""Autorización autenticada para promover a PRODUCCIÓN (corrección #2).

Un string 'approved_by' no alcanza: cualquiera que invoque el comando lo falsifica.
Acá la promoción exige una FIRMA Ed25519 sobre el contenido exacto (skill + sha del
candidato + ventana temporal + nonce), hecha con la clave PRIVADA del operador, que
vive fuera del worker (idealmente offline o como secreto del servidor). El worker solo
conoce la clave PÚBLICA (VUPPIA_APPROVAL_PUBKEY). Cada token es de un solo uso (nonce
registrado en la bitácora) y vence.

Generar un par de claves (una sola vez, en una máquina segura del operador):
    python3 -m vuppia.approval keygen
Firmar una aprobación para un sha concreto:
    python3 -m vuppia.approval sign --skill hypothesis --sha <sha> --ttl 3600
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .store import canonical, sha256


class ApprovalError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def keygen() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    from cryptography.hazmat.primitives import serialization
    pk = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    pub_raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(pk), _b64(pub_raw)


def sign(private_b64: str, skill: str, candidate_sha: str, ttl: int = 3600) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64))
    now = int(time.time())
    payload = {'skill': skill, 'candidate_sha': candidate_sha, 'not_before': now,
               'expires': now + int(ttl), 'nonce': secrets.token_hex(16), 'v': 1}
    msg = canonical(payload).encode()
    sig = priv.sign(msg)
    return _b64(canonical({'payload': payload, 'sig': _b64(sig)}).encode())


def verify(token_b64: str, skill: str, candidate_sha: str) -> dict:
    """Verifica firma, destinatario y ventana temporal. Devuelve el payload o lanza."""
    pub_b64 = os.getenv('VUPPIA_APPROVAL_PUBKEY', '').strip()
    if not pub_b64:
        raise ApprovalError('Falta VUPPIA_APPROVAL_PUBKEY: no hay autoridad de aprobación configurada')
    try:
        outer = json.loads(_unb64(token_b64))
        payload = outer['payload']
        sig = _unb64(outer['sig'])
    except Exception as exc:                          # noqa: BLE001
        raise ApprovalError('token de aprobación malformado') from exc
    pub = Ed25519PublicKey.from_public_bytes(_unb64(pub_b64))
    try:
        pub.verify(sig, canonical(payload).encode())
    except InvalidSignature as exc:
        raise ApprovalError('firma de aprobación inválida (clave equivocada o token manipulado)') from exc
    if payload.get('skill') != skill or payload.get('candidate_sha') != candidate_sha:
        raise ApprovalError('la aprobación es para otro skill o sha')
    now = int(time.time())
    if now < payload.get('not_before', now) - 60:
        raise ApprovalError('aprobación aún no válida')
    if now > payload.get('expires', 0):
        raise ApprovalError('aprobación vencida')
    return payload


def token_id(token_b64: str) -> str:
    return sha256(token_b64)


def _main(argv):
    import argparse
    p = argparse.ArgumentParser(prog='vuppia.approval')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('keygen')
    s = sub.add_parser('sign')
    s.add_argument('--skill', required=True); s.add_argument('--sha', required=True)
    s.add_argument('--ttl', type=int, default=3600)
    s.add_argument('--key-env', default='VUPPIA_APPROVAL_PRIVKEY')
    a = p.parse_args(argv)
    if a.cmd == 'keygen':
        priv, pub = keygen()
        print(json.dumps({'VUPPIA_APPROVAL_PRIVKEY (operador, guardar en secreto/offline)': priv,
                          'VUPPIA_APPROVAL_PUBKEY (worker)': pub}, indent=2))
    else:
        key = os.getenv(a.key_env, '')
        if not key:
            sys.exit(f'Definí {a.key_env} con la clave privada del operador')
        print(sign(key, a.skill, a.sha, a.ttl))


if __name__ == '__main__':
    _main(sys.argv[1:])
