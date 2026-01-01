from __future__ import annotations

import base64
import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.log import logger

_MAGIC = b"ANV2CHAR"
_FORMAT_VERSION = 1
_AAD = b"anime_v2_character_store:v1"


def _cosine(a, b) -> float:
    import numpy as np  # type: ignore

    a = np.asarray(a, dtype=np.float32).reshape(-1)
    b = np.asarray(b, dtype=np.float32).reshape(-1)
    denom = float((a**2).sum() ** 0.5 * (b**2).sum() ** 0.5)
    if denom == 0.0:
        return -1.0
    return float((a * b).sum() / denom)


@dataclass
class Character:
    id: str
    embedding: list[float]
    count: int
    shows: dict[str, int]
    speaker_wavs: list[str]


class CharacterStore:
    """
    Persistent store for per-character voiceprints across scenes/episodes.
    Backed by JSON at data/characters.json.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.characters: dict[str, Character] = {}
        self._next_n = 1

    @classmethod
    def default(cls) -> "CharacterStore":
        return cls(Path("data") / "characters.json")

    def _get_key(self) -> bytes:
        """
        Returns 32-byte AES key. Sources:
        - env CHAR_STORE_KEY (base64)
        - file CHAR_STORE_KEY_FILE (raw base64, whitespace ok)
        """
        s = get_settings()
        raw = None
        if s.char_store_key:
            raw = s.char_store_key.get_secret_value()
        else:
            try:
                p = Path(s.char_store_key_file)
                if p.exists() and p.is_file():
                    raw = p.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                raw = None
        if not raw:
            raise RuntimeError(
                "CharacterStore encryption key missing. Set CHAR_STORE_KEY (32-byte base64) "
                "or mount secrets/char_store.key (see CHAR_STORE_KEY_FILE)."
            )
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception as ex:
            raise RuntimeError("CHAR_STORE_KEY must be valid base64") from ex
        if len(key) != 32:
            raise RuntimeError("CHAR_STORE_KEY must decode to exactly 32 bytes")
        return key

    def _encrypt(self, plaintext: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        except Exception as ex:  # pragma: no cover
            raise RuntimeError("cryptography is required for CharacterStore encryption") from ex
        key = self._get_key()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext, _AAD)
        # file format:
        # [MAGIC (8)][ver (1)][nonce (12)][ciphertext+tag (N)]
        return _MAGIC + bytes([_FORMAT_VERSION]) + nonce + ct

    def _decrypt(self, blob: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        except Exception as ex:  # pragma: no cover
            raise RuntimeError("cryptography is required for CharacterStore encryption") from ex
        if not blob or len(blob) < (len(_MAGIC) + 1 + 12 + 16):
            raise RuntimeError("CharacterStore file is corrupted (too short)")
        if not blob.startswith(_MAGIC):
            raise RuntimeError("CharacterStore file is not encrypted (missing header)")
        ver = blob[len(_MAGIC)]
        if ver != _FORMAT_VERSION:
            raise RuntimeError(f"Unsupported CharacterStore format version: {ver}")
        nonce = blob[len(_MAGIC) + 1 : len(_MAGIC) + 1 + 12]
        ct = blob[len(_MAGIC) + 1 + 12 :]
        key = self._get_key()
        return AESGCM(key).decrypt(nonce, ct, _AAD)

    def _migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        # JSON schema versioning inside plaintext payload.
        v = int(data.get("version", 1))
        if v == 1:
            return data
        # Future-proof: best-effort migration by keeping known fields.
        logger.warning("CharacterStore schema version mismatch", found=v, expected=1)
        out = {"version": 1, "next_n": int(data.get("next_n", 1)), "characters": data.get("characters", {})}
        return out

    def load(self) -> None:
        if not self.path.exists():
            return
        # Encrypted at rest. Reads require a key.
        blob = self.path.read_bytes()
        pt = self._decrypt(blob)
        data = json.loads(pt.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("CharacterStore decrypted payload is not a JSON object")
        data = self._migrate(data)
        chars = data.get("characters", {})
        self.characters = {}
        for cid, c in chars.items():
            self.characters[cid] = Character(
                id=cid,
                embedding=list(c.get("embedding", [])),
                count=int(c.get("count", 1)),
                shows=dict(c.get("shows", {})),
                speaker_wavs=list(c.get("speaker_wavs", [])),
            )
        self._next_n = int(data.get("next_n", 1))

    def save(self) -> None:
        data = {
            "version": 1,
            "next_n": self._next_n,
            "characters": {
                cid: {
                    "embedding": c.embedding,
                    "count": c.count,
                    "shows": c.shows,
                    "speaker_wavs": c.speaker_wavs,
                }
                for cid, c in self.characters.items()
            },
        }
        tmp = self.path.with_suffix(".tmp")
        pt = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        blob = self._encrypt(pt)
        tmp.write_bytes(blob)
        tmp.replace(self.path)

    def _new_id(self) -> str:
        cid = f"SPEAKER_{self._next_n:02d}"
        self._next_n += 1
        return cid

    def match_or_create(self, embedding, show_id: str, thresholds: dict[str, float]) -> str:
        """
        Returns stable character id.
        thresholds: {"sim": float}
        """
        import numpy as np  # type: ignore

        sim_thresh = float(thresholds.get("sim", 0.72))
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)

        best_id = None
        best_sim = -1.0

        # Prefer matching within same show_id, else global.
        candidates = list(self.characters.values())
        show_first = [c for c in candidates if show_id in c.shows]
        rest = [c for c in candidates if show_id not in c.shows]
        for c in show_first + rest:
            try:
                sim = _cosine(emb, c.embedding)
            except Exception:
                continue
            if sim > best_sim:
                best_sim = sim
                best_id = c.id

        if best_id is not None and best_sim >= sim_thresh:
            self.update(best_id, emb)
            self.characters[best_id].shows[show_id] = int(self.characters[best_id].shows.get(show_id, 0)) + 1
            logger.info("CharacterStore match %s sim=%.3f show=%s", best_id, best_sim, show_id)
            return best_id

        cid = self._new_id()
        self.characters[cid] = Character(id=cid, embedding=emb.astype(float).tolist(), count=1, shows={show_id: 1}, speaker_wavs=[])
        logger.info("CharacterStore new %s show=%s", cid, show_id)
        return cid

    def update(self, id: str, embedding) -> None:
        import numpy as np  # type: ignore

        if id not in self.characters:
            return
        c = self.characters[id]
        old = np.asarray(c.embedding, dtype=np.float32)
        new = np.asarray(embedding, dtype=np.float32).reshape(-1)
        n = max(1, int(c.count))
        avg = (old * n + new) / float(n + 1)
        c.embedding = avg.astype(float).tolist()
        c.count = n + 1

    def link_speaker_wav(self, id: str, path: str) -> None:
        if id not in self.characters:
            return
        c = self.characters[id]
        if path not in c.speaker_wavs:
            c.speaker_wavs.append(path)
