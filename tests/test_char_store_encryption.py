from __future__ import annotations

import base64
from pathlib import Path

import pytest

from anime_v2.stages.character_store import Character, CharacterStore
from anime_v2.config import get_settings


def _b64_32() -> str:
    return base64.b64encode(b"\x01" * 32).decode("ascii")


def test_character_store_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "characters.json"
    store = CharacterStore(p)
    monkeypatch.delenv("CHAR_STORE_KEY", raising=False)
    # ensure key file not present
    monkeypatch.setenv("CHAR_STORE_KEY_FILE", str(tmp_path / "missing.key"))
    get_settings.cache_clear()

    with pytest.raises(RuntimeError) as ex:
        store.save()
    assert "CHAR_STORE_KEY" in str(ex.value)


def test_character_store_roundtrip_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "characters.json"
    monkeypatch.setenv("CHAR_STORE_KEY", _b64_32())
    monkeypatch.setenv("CHAR_STORE_KEY_FILE", str(tmp_path / "missing.key"))
    get_settings.cache_clear()

    s1 = CharacterStore(p)
    s1.characters["SPEAKER_01"] = Character(
        id="SPEAKER_01",
        embedding=[0.0, 1.0, 0.0],
        count=1,
        shows={"show": 1},
        speaker_wavs=["/tmp/a.wav"],
    )
    s1.save()
    assert p.exists()
    # Encrypted file should not be valid UTF-8 JSON
    raw = p.read_bytes()
    assert raw.startswith(b"ANV2CHAR")

    s2 = CharacterStore(p)
    s2.load()
    assert "SPEAKER_01" in s2.characters
    assert s2.characters["SPEAKER_01"].shows.get("show") == 1

