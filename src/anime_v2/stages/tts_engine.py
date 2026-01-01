from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.gates.license import require_coqui_tos
from anime_v2.runtime.device_allocator import pick_device
from anime_v2.runtime.model_manager import ModelManager
from anime_v2.utils.config import get_settings
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger


class TTSEngine(abc.ABC):
    @abc.abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        language: str,
        speaker_id: str | None = None,
        speaker_wav: Path | None = None,
        out_path: Path | None = None,
    ) -> Path:
        """
        Returns a WAV file path.
        Implementations may write to `out_path` (if provided) or a temp file.
        """


class CoquiXTTS(TTSEngine):
    """
    Coqui TTS XTTS engine wrapper.

    Requires:
      - `TTS` Python package installed
      - `COQUI_TOS_AGREED=1` set in env
    """

    def __init__(self) -> None:
        require_coqui_tos()
        settings = get_settings()

        self.model_name = settings.tts_model or "tts_models/multilingual/multi-dataset/xtts_v2"
        self._tts = None
        self._device = pick_device("auto")

    def _load(self):
        if self._tts is not None:
            return self._tts
        logger.info(
            "[v2] Loading Coqui TTS model (via ModelManager): %s device=%s",
            self.model_name,
            self._device,
        )
        self._tts = ModelManager.instance().get_tts(self.model_name, self._device)
        return self._tts

    def synthesize(
        self,
        text: str,
        *,
        language: str,
        speaker_id: str | None = None,
        speaker_wav: Path | None = None,
        out_path: Path | None = None,
    ) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        if out_path is None:
            raise ValueError("out_path is required for CoquiXTTS.")

        tts = self._load()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Coqui XTTS:
        # - cloning path uses speaker_wav + language (do NOT pass speaker preset)
        # - preset path requires a speaker string for multi-speaker models
        if speaker_wav is not None:
            logger.debug("[v2] XTTS clone synth (speaker_wav=%s)", speaker_wav)
            kwargs: dict[str, Any] = {
                "text": text,
                "language": language,
                "speaker_wav": str(speaker_wav),
                "file_path": str(out_path),
            }
            try:
                tts.tts_to_file(**kwargs)
            except TypeError:
                # Older API variants
                tts.tts_to_file(
                    text=text,
                    speaker_wav=str(speaker_wav),
                    language=language,
                    file_path=str(out_path),
                )
            return out_path

        if not speaker_id:
            settings = get_settings()
            speaker_id = settings.tts_speaker or "default"
        if not str(speaker_id).strip():
            raise ValueError("Model is multi-speaker but no speaker was provided.")

        logger.debug("[v2] XTTS preset synth (speaker_id=%s)", speaker_id)
        try:
            tts.tts_to_file(
                text=text, speaker=str(speaker_id), language=language, file_path=str(out_path)
            )
        except TypeError:
            # Some Coqui versions use speaker_id parameter name
            tts.tts_to_file(
                text=text, speaker_id=str(speaker_id), language=language, file_path=str(out_path)
            )
        return out_path


def _cosine_sim(a, b) -> float:
    import numpy as np  # type: ignore

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return -1.0
    return float(np.dot(a, b) / denom)


@dataclass(frozen=True, slots=True)
class VoicePreset:
    name: str
    embedding_path: Path
    samples: list[Path]


def build_voice_db(*, preset_dir: Path, db_path: Path, embeddings_dir: Path) -> dict[str, Any]:
    """
    Build `voices/presets.json` and preset embeddings once.
    """
    preset_dir = preset_dir.resolve()
    embeddings_dir.mkdir(parents=True, exist_ok=True)

    try:
        import numpy as np  # type: ignore
        from resemblyzer import VoiceEncoder, preprocess_wav  # type: ignore
    except Exception as ex:
        logger.warning("[v2] Preset voice DB build skipped (missing deps): %s", ex)
        data = {"version": 1, "presets": {}}
        write_json(db_path, data)
        return data

    encoder = VoiceEncoder()
    presets: dict[str, Any] = {}

    for sub in sorted(preset_dir.glob("*")):
        if not sub.is_dir():
            continue
        name = sub.name
        wavs = sorted([p for p in sub.rglob("*.wav") if p.is_file()])
        if not wavs:
            continue

        embs = []
        for wav in wavs:
            try:
                w = preprocess_wav(str(wav))
                embs.append(encoder.embed_utterance(w))
            except Exception:
                continue
        if not embs:
            continue

        emb = np.mean(np.stack(embs, axis=0), axis=0).astype(np.float32)
        emb_path = embeddings_dir / f"{name}.npy"
        np.save(str(emb_path), emb)
        # Store embedding path relative to DB location (portable)
        try:
            rel = emb_path.resolve().relative_to(db_path.parent.resolve())
            rel_s = str(rel).replace("\\", "/")
        except Exception:
            rel_s = str(emb_path)
        presets[name] = {"embedding": rel_s, "samples": [str(p) for p in wavs]}

    data = {"version": 1, "presets": presets}
    write_json(db_path, data)
    logger.info("[v2] Preset voice DB ready (%s presets) → %s", len(presets), db_path)
    return data


def load_voice_db(*, preset_dir: Path, db_path: Path, embeddings_dir: Path) -> dict[str, Any]:
    db = read_json(db_path, default=None)
    # Support both:
    # - new format: {"version":1,"presets": {"alice":{"embedding":"embeddings/alice.npy"}, ...}}
    # - legacy format: {"version":1,"presets": {"alice":{"embedding_path":"/abs/path.npy"}, ...}}
    if isinstance(db, dict) and isinstance(db.get("presets"), dict):
        return db
    return build_voice_db(preset_dir=preset_dir, db_path=db_path, embeddings_dir=embeddings_dir)


def choose_similar_voice(
    target_embedding: Path, *, preset_dir: Path, db_path: Path, embeddings_dir: Path
) -> str | None:
    """
    Returns best preset name by cosine similarity.
    """
    try:
        import numpy as np  # type: ignore
    except Exception as ex:
        logger.warning("[v2] choose_similar_voice: numpy missing (%s)", ex)
        return None

    db = load_voice_db(preset_dir=preset_dir, db_path=db_path, embeddings_dir=embeddings_dir)
    presets = db.get("presets", {})
    if not isinstance(presets, dict) or not presets:
        return None

    try:
        tgt = np.load(str(target_embedding))
    except Exception as ex:
        logger.warning("[v2] choose_similar_voice: failed loading target embedding (%s)", ex)
        return None

    best_name = None
    best_sim = -math.inf
    for name, meta in presets.items():
        try:
            emb_ref = meta.get("embedding") or meta.get("embedding_path")
            if not emb_ref:
                continue
            emb_path = Path(str(emb_ref))
            if not emb_path.is_absolute():
                emb_path = (db_path.parent / emb_path).resolve()
            emb = np.load(str(emb_path))
            sim = _cosine_sim(tgt, emb)
            if sim > best_sim:
                best_sim = sim
                best_name = str(name)
        except Exception:
            continue

    logger.info(
        "[v2] choose_similar_voice: best=%s sim=%.3f", best_name, best_sim if best_name else -1.0
    )
    return best_name
