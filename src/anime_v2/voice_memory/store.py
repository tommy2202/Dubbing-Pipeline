"""
Tier-2A: Persistent Character Voice Memory store.

Layout (default under data/voice_memory):
  characters.json
  embeddings/<character_id>/{ref_001.wav, ref_002.wav, embedding.npy|embedding.json}
  episodes/<episode_key>.json
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_copy, atomic_write_text, read_json
from anime_v2.voice_memory.embeddings import compute_embedding, match_embedding


def _now() -> str:
    # keep iso-ish string without new deps
    return __import__("datetime").datetime.now(tz=__import__("datetime").UTC).isoformat()


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    return read_json(path, default=default)


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _try_np_save(path: Path, vec: list[float]) -> bool:
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        # Write via temp then replace for atomicity.
        tmp = Path(str(path) + ".tmp")
        np.save(str(tmp), arr)
        tmp.replace(path)
        return True
    except Exception:
        return False


def _try_np_load(path: Path) -> list[float] | None:
    try:
        import numpy as np  # type: ignore

        arr = np.load(str(path))
        return [float(x) for x in arr.reshape(-1).tolist()]
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class CharacterMeta:
    character_id: str
    display_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    voice_mode: str = ""  # clone|preset|single (preference)
    preset_voice_id: str = ""
    notes: str = ""
    tags: list[str] | None = None


class VoiceMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.characters_path = self.root / "characters.json"
        self.embeddings_dir = self.root / "embeddings"
        self.episodes_dir = self.root / "episodes"
        _safe_mkdir(self.root)
        _safe_mkdir(self.embeddings_dir)
        _safe_mkdir(self.episodes_dir)

    def _load_characters(self) -> dict[str, Any]:
        data = _load_json(self.characters_path, default=None)
        if not isinstance(data, dict):
            return {"version": 1, "characters": {}}
        data.setdefault("version", 1)
        data.setdefault("characters", {})
        if not isinstance(data["characters"], dict):
            data["characters"] = {}
        return data

    def _save_characters(self, data: dict[str, Any]) -> None:
        _dump_json(self.characters_path, data)

    def list_characters(self) -> list[dict[str, Any]]:
        data = self._load_characters()
        chars = data.get("characters", {})
        out = []
        if isinstance(chars, dict):
            for cid, meta in chars.items():
                if not isinstance(meta, dict):
                    continue
                d = dict(meta)
                d.setdefault("character_id", str(cid))
                out.append(d)
        out.sort(key=lambda x: str(x.get("character_id") or ""))
        return out

    def ensure_character(self, *, character_id: str | None = None) -> str:
        data = self._load_characters()
        chars: dict[str, Any] = data["characters"]
        if character_id and str(character_id).strip():
            cid = str(character_id).strip()
        else:
            # stable incrementing IDs: SPEAKER_XX
            existing = list(chars.keys())
            max_n = 0
            for k in existing:
                if str(k).startswith("SPEAKER_"):
                    with __import__("contextlib").suppress(Exception):
                        max_n = max(max_n, int(str(k).split("_", 1)[1]))
            cid = f"SPEAKER_{max_n + 1:02d}"
        now = _now()
        if cid not in chars or not isinstance(chars.get(cid), dict):
            chars[cid] = {
                "character_id": cid,
                "display_name": "",
                "created_at": now,
                "updated_at": now,
                "voice_mode": "",
                "preset_voice_id": "",
                "notes": "",
                "tags": [],
            }
            self._save_characters(data)
        return cid

    def update_character(self, character_id: str, patch: dict[str, Any]) -> None:
        data = self._load_characters()
        chars: dict[str, Any] = data["characters"]
        cid = str(character_id)
        rec = chars.get(cid)
        if not isinstance(rec, dict):
            rec = {"character_id": cid, "created_at": _now()}
        rec.update({k: v for k, v in patch.items()})
        rec["updated_at"] = _now()
        chars[cid] = rec
        self._save_characters(data)

    def rename_character(self, character_id: str, new_name: str) -> None:
        self.update_character(str(character_id), {"display_name": str(new_name)})

    def set_character_voice_mode(self, character_id: str, mode: str) -> None:
        m = str(mode).strip().lower()
        if m not in {"clone", "preset", "single", ""}:
            raise ValueError("voice_mode must be clone|preset|single")
        self.update_character(str(character_id), {"voice_mode": m})

    def set_character_preset(self, character_id: str, preset_voice_id: str) -> None:
        self.update_character(str(character_id), {"preset_voice_id": str(preset_voice_id).strip()})

    def character_dir(self, character_id: str) -> Path:
        d = self.embeddings_dir / str(character_id)
        _safe_mkdir(d)
        return d

    def list_refs(self, character_id: str) -> list[Path]:
        d = self.character_dir(character_id)
        return sorted([p for p in d.glob("ref_*.wav") if p.is_file()])

    def best_ref(self, character_id: str) -> Path | None:
        refs = self.list_refs(character_id)
        return refs[-1] if refs else None

    def load_embedding(self, character_id: str) -> list[float] | None:
        d = self.character_dir(character_id)
        npy = d / "embedding.npy"
        js = d / "embedding.json"
        if npy.exists():
            v = _try_np_load(npy)
            if v:
                return v
        if js.exists():
            try:
                data = json.loads(js.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, dict) and isinstance(data.get("embedding"), list):
                    return [float(x) for x in data["embedding"]]
            except Exception:
                return None
        return None

    def save_embedding(self, character_id: str, embedding: list[float], *, provider: str) -> None:
        d = self.character_dir(character_id)
        npy = d / "embedding.npy"
        js = d / "embedding.json"
        if _try_np_save(npy, embedding):
            # keep a small json metadata too
            _dump_json(js, {"provider": str(provider), "embedding": embedding})
            return
        _dump_json(js, {"provider": str(provider), "embedding": embedding})

    def enroll_ref(
        self,
        character_id: str,
        wav_path: Path,
        *,
        max_refs: int = 5,
    ) -> Path:
        d = self.character_dir(character_id)
        refs = self.list_refs(character_id)
        n = len(refs) + 1
        dest = d / f"ref_{n:03d}.wav"
        atomic_copy(Path(wav_path), dest)
        # prune oldest
        refs2 = self.list_refs(character_id)
        if len(refs2) > int(max_refs):
            for p in refs2[: len(refs2) - int(max_refs)]:
                with __import__("contextlib").suppress(Exception):
                    p.unlink(missing_ok=True)
        return dest

    def match_or_create_from_wav(
        self,
        wav_path: Path,
        *,
        device: str,
        threshold: float,
        auto_enroll: bool,
    ) -> tuple[str, float, str]:
        """
        Returns (character_id, similarity, provider).
        """
        emb, provider = compute_embedding(Path(wav_path), device=device)
        if emb is None:
            raise RuntimeError(f"embedding unavailable (provider={provider})")

        # build candidate embeddings
        candidates: dict[str, list[float]] = {}
        for c in self.list_characters():
            cid = str(c.get("character_id") or "")
            if not cid:
                continue
            v = self.load_embedding(cid)
            if v:
                candidates[cid] = v

        best_id, best_sim = match_embedding(emb, candidates, threshold=float(threshold))
        if best_id is None:
            if not auto_enroll:
                cid = self.ensure_character()
                return cid, float(best_sim), provider
            cid = self.ensure_character()
            self.enroll_ref(cid, wav_path)
            self.save_embedding(cid, emb, provider=provider)
            self.update_character(
                cid,
                {
                    "updated_at": _now(),
                },
            )
            return cid, 1.0, provider

        # update matched character (best-effort running average)
        old = self.load_embedding(best_id) or emb
        try:
            n_old = max(1, len(old))
            n_new = max(1, len(emb))
            if n_old == n_new:
                updated = [(float(o) + float(e)) / 2.0 for o, e in zip(old, emb, strict=False)]
            else:
                updated = emb
        except Exception:
            updated = emb
        self.enroll_ref(best_id, wav_path)
        self.save_embedding(best_id, updated, provider=provider)
        self.update_character(best_id, {"updated_at": _now()})
        return best_id, float(best_sim), provider

    def write_episode_mapping(
        self,
        episode_key: str,
        *,
        source: dict[str, Any],
        mapping: dict[str, dict[str, Any]],
    ) -> Path:
        """
        mapping: diar_label -> {character_id, similarity, provider, confidence}
        """
        ek = str(episode_key).strip()
        if not ek:
            raise ValueError("episode_key required")
        path = self.episodes_dir / f"{ek}.json"
        data = {
            "version": 1,
            "episode_key": ek,
            "created_at": _now(),
            "source": dict(source),
            "mapping": dict(mapping),
        }
        _dump_json(path, data)
        return path


def compute_episode_key(*, audio_hash: str | None, video_path: Path | None) -> str:
    if audio_hash and str(audio_hash).strip():
        return f"audio_{str(audio_hash).strip()[:32]}"
    p = Path(video_path) if video_path is not None else None
    if p and p.exists():
        st = p.stat()
        raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()
        import hashlib

        return hashlib.sha256(raw).hexdigest()[:32]
    import hashlib

    return hashlib.sha256(str(time.time()).encode("utf-8")).hexdigest()[:32]
