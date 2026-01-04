from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.stages.tts_engine import CoquiXTTS, choose_similar_voice, load_voice_db
from anime_v2.utils.io import atomic_write_text, write_json
from anime_v2.utils.log import logger
from anime_v2.voice_memory.store import VoiceMemoryStore


def _now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: str  # clone|preset|fallback
    label: str
    speaker_id: str | None = None
    speaker_wav: Path | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "speaker_id": str(self.speaker_id) if self.speaker_id else "",
            "speaker_wav": str(self.speaker_wav) if self.speaker_wav else "",
            "score": float(self.score) if self.score is not None else None,
        }


def _write_silence_wav(path: Path, *, duration_s: float = 2.0, sr: int = 22050) -> None:
    import wave

    n = max(1, int(float(duration_s) * int(sr)))
    buf = b"\x00\x00" * n
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf)


def _top_presets_for_character(character_id: str, *, top_n: int) -> list[Candidate]:
    """
    Best-effort preset selection:
      - if voice memory embedding path exists as .npy, use choose_similar_voice (best only)
      - else list first N presets from db
    """
    s = get_settings()
    preset_dir = Path(s.voice_preset_dir).resolve()
    db_path = Path(s.voice_db_path).resolve()
    embeddings_dir = (db_path.parent / "embeddings").resolve()

    if not str(character_id or "").strip():
        db = load_voice_db(preset_dir=preset_dir, db_path=db_path, embeddings_dir=embeddings_dir)
        presets = db.get("presets", {}) if isinstance(db, dict) else {}
        names = sorted([str(k) for k in presets]) if isinstance(presets, dict) else []
        return [Candidate(kind="preset", label=f"preset:{nm}", speaker_id=nm, score=None) for nm in names[: max(0, int(top_n))]]

    # Attempt similarity using stored embedding.npy from voice memory (may not match preset space; best-effort).
    vm_root = Path(s.voice_memory_dir).resolve()
    emb_path = (vm_root / "embeddings" / str(character_id) / "embedding.npy").resolve()
    if emb_path.exists():
        best = choose_similar_voice(
            emb_path, preset_dir=preset_dir, db_path=db_path, embeddings_dir=embeddings_dir
        )
        if best:
            return [Candidate(kind="preset", label=f"preset:{best}", speaker_id=best, score=1.0)]

    db = load_voice_db(preset_dir=preset_dir, db_path=db_path, embeddings_dir=embeddings_dir)
    presets = db.get("presets", {}) if isinstance(db, dict) else {}
    names = sorted([str(k) for k in presets]) if isinstance(presets, dict) else []
    out = []
    for nm in names[: max(0, int(top_n))]:
        out.append(Candidate(kind="preset", label=f"preset:{nm}", speaker_id=nm, score=None))
    return out


def build_candidates(*, character_id: str | None, top_n: int) -> list[Candidate]:
    s = get_settings()
    out: list[Candidate] = []

    if character_id:
        vm = VoiceMemoryStore(Path(s.voice_memory_dir).resolve())
        # Clone ref candidate
        ref = vm.best_ref(str(character_id))
        if ref is not None and ref.exists():
            out.append(Candidate(kind="clone", label=f"clone:{character_id}", speaker_wav=ref))
        # Preferred preset from character meta
        for rec in vm.list_characters():
            if str(rec.get("character_id") or "") == str(character_id):
                pref = str(rec.get("preset_voice_id") or "").strip()
                if pref:
                    out.append(Candidate(kind="preset", label=f"preset:{pref}", speaker_id=pref, score=None))
                break
        # Similar presets (best-effort)
        out.extend(_top_presets_for_character(str(character_id), top_n=int(top_n)))
    else:
        # No character context: list first N presets
        out.extend(_top_presets_for_character("", top_n=int(top_n)))

    # Deduplicate by (kind,label)
    seen = set()
    uniq: list[Candidate] = []
    for c in out:
        k = (c.kind, c.label, str(c.speaker_id or ""), str(c.speaker_wav or ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    if not uniq:
        # Always provide at least one candidate so the tool is usable without any deps.
        uniq = [Candidate(kind="fallback", label="fallback:silence", speaker_id=None)]
    return uniq[: max(1, int(top_n))]


def audition(
    *,
    text: str,
    top_n: int,
    character_id: str | None,
    out_job_dir: Path,
    language: str = "en",
) -> dict[str, Any]:
    """
    Produce audition WAVs under Output/<job>/audition/.
    Always produces outputs (silence fallback if TTS unavailable).
    """
    job_dir = Path(out_job_dir).resolve()
    out_dir = job_dir / "audition"
    _safe_mkdir(out_dir)

    candidates = build_candidates(character_id=character_id, top_n=int(top_n))
    engine: CoquiXTTS | None = None
    try:
        engine = CoquiXTTS()
    except Exception as ex:
        engine = None
        logger.info("audition_xtts_unavailable", error=str(ex))

    results = []
    for i, cand in enumerate(candidates, 1):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", cand.label)[:80]
        wav = out_dir / f"{i:02d}_{name}.wav"
        ok = False
        err = ""
        try:
            if engine is not None:
                engine.synthesize(
                    text,
                    language=str(language),
                    speaker_id=cand.speaker_id,
                    speaker_wav=cand.speaker_wav,
                    out_path=wav,
                )
                ok = True
            else:
                # best-effort: use espeak-ng via tts stage fallback
                from anime_v2.stages.tts import _espeak_fallback  # type: ignore

                _espeak_fallback(text, wav)
                ok = True
        except Exception as ex:
            err = str(ex)
            ok = False
            _write_silence_wav(wav, duration_s=2.0)

        results.append(
            {
                "candidate": cand.to_dict(),
                "wav": str(wav),
                "ok": bool(ok),
                "error": err,
            }
        )

    manifest = {
        "version": 1,
        "created_at": time.time(),
        "job_dir": str(job_dir),
        "text": str(text),
        "language": str(language),
        "character_id": str(character_id or ""),
        "results": results,
    }
    write_json(out_dir / "manifest.json", manifest, indent=2)
    atomic_write_text(
        job_dir / "analysis" / "audition_summary.json",
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest

