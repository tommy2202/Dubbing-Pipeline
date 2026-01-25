from __future__ import annotations

from pathlib import Path

import pytest

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security.crypto import MAGIC_NEW, MAGIC_OLD, decrypt_file, encrypt_file, is_encrypted_path
from dubbing_pipeline.stages.character_store import CharacterStore, _MAGIC_NEW
from tests.marker_helpers import b64_32, write_legacy_character_store


def test_crypto_magic_backward_compat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACTS_KEY", b64_32())
    monkeypatch.setenv("ARTIFACTS_KEY_FILE", str(tmp_path / "missing.key"))
    get_settings.cache_clear()

    inp = tmp_path / "plain.txt"
    data = b"hello marker compat"
    inp.write_bytes(data)
    enc_new = tmp_path / "enc_new.bin"
    encrypt_file(inp, enc_new, kind="uploads", job_id="j1")
    raw_new = enc_new.read_bytes()
    assert raw_new.startswith(MAGIC_NEW)
    assert is_encrypted_path(enc_new)

    out_new = tmp_path / "dec_new.txt"
    decrypt_file(enc_new, out_new, kind="uploads", job_id="j1")
    assert out_new.read_bytes() == data

    enc_old = tmp_path / "enc_old.bin"
    raw_old = MAGIC_OLD + raw_new[len(MAGIC_NEW) :]
    enc_old.write_bytes(raw_old)
    assert is_encrypted_path(enc_old)

    out_old = tmp_path / "dec_old.txt"
    decrypt_file(enc_old, out_old, kind="uploads", job_id="j1")
    assert out_old.read_bytes() == data


def test_character_store_migrates_legacy_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "characters.json"
    monkeypatch.setenv("CHAR_STORE_KEY", b64_32())
    monkeypatch.setenv("CHAR_STORE_KEY_FILE", str(tmp_path / "missing.key"))
    get_settings.cache_clear()

    payload = {
        "version": 1,
        "next_n": 2,
        "characters": {
            "SPEAKER_01": {
                "embedding": [0.0, 1.0, 0.0],
                "count": 1,
                "shows": {"show": 1},
                "speaker_wavs": ["/tmp/a.wav"],
            }
        },
    }
    write_legacy_character_store(p, payload=payload)

    store = CharacterStore(p)
    store.load()
    store.save()

    raw = p.read_bytes()
    assert raw.startswith(_MAGIC_NEW)
