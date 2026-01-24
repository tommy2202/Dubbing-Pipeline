from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from dubbing_pipeline.stages.character_store import (
    CharacterStore,
    _AAD_LEGACY,
    _FORMAT_VERSION,
    _MAGIC_OLD,
)


def b64_32() -> str:
    return base64.b64encode(b"\x01" * 32).decode("ascii")


def write_legacy_character_store(path: Path, *, payload: dict[str, Any]) -> None:
    store = CharacterStore(path)
    key = store._get_key()
    plaintext = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    nonce = os.urandom(12)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("cryptography is required for CharacterStore encryption") from ex
    ct = AESGCM(key).encrypt(nonce, plaintext, _AAD_LEGACY)
    blob = _MAGIC_OLD + bytes([_FORMAT_VERSION]) + nonce + ct
    path.write_bytes(blob)
