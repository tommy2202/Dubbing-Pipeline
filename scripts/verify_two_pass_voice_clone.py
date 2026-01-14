#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import random
import tempfile
import wave
from pathlib import Path

from dubbing_pipeline.stages import tts
from dubbing_pipeline.voice_memory.ref_extraction import VoiceRefConfig, extract_speaker_refs


def _write_wav(path: Path, *, sr: int, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    b = bytearray()
    for v in samples:
        vv = max(-32768, min(32767, int(v)))
        b += int(vv).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(bytes(b))


def _slice_wav(src: Path, *, start_s: float, end_s: float, out: Path) -> None:
    with wave.open(str(src), "rb") as wf:
        sr = int(wf.getframerate())
        n0 = max(0, int(start_s * sr))
        n1 = max(n0, int(end_s * sr))
        wf.setpos(n0)
        frames = wf.readframes(n1 - n0)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as outwf:
        outwf.setnchannels(1)
        outwf.setsampwidth(2)
        outwf.setframerate(sr)
        outwf.writeframes(frames)


def _dur(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return float(wf.getnframes()) / float(wf.getframerate() or 16000)


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    # Keep this verifier offline-safe and deterministic.
    os.environ.setdefault("OFFLINE_MODE", "1")
    os.environ.setdefault("ALLOW_EGRESS", "0")
    os.environ.setdefault("ALLOW_HF_EGRESS", "0")
    os.environ.setdefault("STRICT_SECRETS", "0")

    random.seed(0)
    sr = 16000
    total_s = 8.0
    total_n = int(total_s * sr)
    a_freq = 440.0
    b_freq = 660.0
    samples: list[int] = []
    for i in range(total_n):
        t = i / sr
        x = 0.0
        # A: 0-4s
        if 0.0 <= t < 4.0:
            x += 0.55 * math.sin(2 * math.pi * a_freq * t)
        # B: 4-8s
        if 4.0 <= t < 8.0:
            x += 0.35 * math.sin(2 * math.pi * b_freq * t)
        # tiny noise
        x += 0.01 * (random.random() * 2.0 - 1.0)
        samples.append(int(x * 32767.0))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        audio = root / "audio.wav"
        _write_wav(audio, sr=sr, samples=samples)

        # Two speakers, no overlap.
        segs = [
            {"start": 0.0, "end": 2.0, "speaker_id": "SPEAKER_01"},
            {"start": 2.0, "end": 4.0, "speaker_id": "SPEAKER_01"},
            {"start": 4.0, "end": 6.0, "speaker_id": "SPEAKER_02"},
            {"start": 6.0, "end": 8.0, "speaker_id": "SPEAKER_02"},
        ]

        seg_dir = root / "segments"
        diar_segments: list[dict] = []
        for i, s in enumerate(segs):
            out = seg_dir / f"{i:04d}_{s['speaker_id']}.wav"
            _slice_wav(audio, start_s=float(s["start"]), end_s=float(s["end"]), out=out)
            diar_segments.append(
                {
                    "start": float(s["start"]),
                    "end": float(s["end"]),
                    "speaker_id": str(s["speaker_id"]),
                    "wav_path": str(out),
                }
            )

        # Pass 1: extract refs.
        voice_store = root / "voice_store"
        cfg = VoiceRefConfig(target_s=3.0, min_candidate_s=0.5, max_candidate_s=6.0, overlap_eps_s=0.01)
        man = extract_speaker_refs(segments=diar_segments, voice_store_dir=voice_store, job_dir=root, cfg=cfg)
        items = man.get("items") or {}
        assert "SPEAKER_01" in items and "SPEAKER_02" in items, items.keys()
        # Ensure job-local refs exist.
        for sid in ("SPEAKER_01", "SPEAKER_02"):
            job_ref = Path(str(items[sid].get("job_ref_path") or ""))
            assert job_ref.exists(), f"missing job_ref_path for {sid}"
            assert 1.0 <= _dur(job_ref) <= 6.0

        # Minimal translated.json
        translated = root / "translated.json"
        translated.write_text(
            json.dumps(
                {
                    "src_lang": "en",
                    "tgt_lang": "en",
                    "segments": [
                        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_01", "text": "Hello there."},
                        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01", "text": "How are you?"},
                        {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_02", "text": "I am fine."},
                        {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_02", "text": "Thanks."},
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        diar_work = root / "diarization.work.json"
        diar_work.write_text(
            json.dumps({"audio_path": str(audio), "segments": diar_segments, "speaker_embeddings": {}}, indent=2),
            encoding="utf-8",
        )

        # Pass 1 TTS (no clone). Use espeak provider to keep this verifier lightweight.
        out1 = root / "pass1"
        wav1 = out1 / "tts.wav"
        tts.run(
            out_dir=out1,
            translated_json=translated,
            diarization_json=diar_work,
            wav_out=wav1,
            voice_mode="preset",
            no_clone=True,
            tts_provider="espeak",
            voice_store_dir=voice_store,
            source_audio_wav=audio,
        )
        m1 = _read_manifest(out1 / "tts_manifest.json")
        assert bool(m1.get("no_clone")) is True, m1.get("no_clone")

        # Pass 2 TTS (clone mode) using extracted refs.
        out2 = root / "pass2"
        wav2 = out2 / "tts.wav"
        ref_dir = (root / "analysis" / "voice_refs").resolve()
        assert ref_dir.exists(), "expected job-local refs directory"
        tts.run(
            out_dir=out2,
            translated_json=translated,
            diarization_json=diar_work,
            wav_out=wav2,
            voice_mode="clone",
            no_clone=False,
            voice_ref_dir=ref_dir,
            # Use auto so real environments can exercise XTTS; will fall back cleanly when unavailable.
            tts_provider="auto",
            voice_store_dir=voice_store,
            source_audio_wav=audio,
        )
        m2 = _read_manifest(out2 / "tts_manifest.json")
        assert bool(m2.get("no_clone")) is False, m2.get("no_clone")
        assert str(m2.get("voice_ref_dir") or "") == str(ref_dir)

        rep = m2.get("speaker_report")
        assert isinstance(rep, dict) and rep, "missing speaker_report"
        for sid in ("SPEAKER_01", "SPEAKER_02"):
            r = rep.get(sid)
            assert isinstance(r, dict), f"missing report for {sid}"
            refs = r.get("refs_used")
            assert isinstance(refs, list) and refs, f"expected refs_used for {sid}"
            assert any(str(ref_dir) in str(x) for x in refs), (sid, refs)

    print("verify_two_pass_voice_clone: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

