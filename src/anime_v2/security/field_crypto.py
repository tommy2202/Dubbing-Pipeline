from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from anime_v2.config import get_settings
from anime_v2.security.crypto import CryptoConfigError  # reuse key source semantics


_PREFIX = "enc:v1:"


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("cryptography is required for encrypted fields") from ex
    return AESGCM


def _read_key() -> bytes:
    """
    Use the same key source as artifact encryption (ARTIFACTS_KEY / ARTIFACTS_KEY_FILE).
    """
    # Import locally to avoid circulars.
    from anime_v2.security.crypto import _read_key_bytes  # type: ignore

    return _read_key_bytes()


def encrypt_field(plaintext: str, *, aad: str) -> str:
    key = _read_key()
    AESGCM = _aesgcm()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), str(aad).encode("utf-8"))
    return _PREFIX + base64.b64encode(nonce).decode("ascii") + ":" + base64.b64encode(ct).decode("ascii")


def decrypt_field(value: str, *, aad: str) -> str:
    v = str(value or "")
    if not v.startswith(_PREFIX):
        return v
    rest = v[len(_PREFIX) :]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        raise CryptoConfigError("Corrupted encrypted field")
    try:
        nonce = base64.b64decode(parts[0], validate=True)
        ct = base64.b64decode(parts[1], validate=True)
    except Exception as ex:
        raise CryptoConfigError("Corrupted encrypted field (base64)") from ex
    if len(nonce) != 12:
        raise CryptoConfigError("Corrupted encrypted field (nonce)")
    AESGCM = _aesgcm()
    key = _read_key()
    pt = AESGCM(key).decrypt(nonce, ct, str(aad).encode("utf-8"))
    return pt.decode("utf-8", errors="strict")


def totp_enabled() -> bool:
    return bool(getattr(get_settings(), "enable_totp", False))

