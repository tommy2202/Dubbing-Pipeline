from __future__ import annotations

import json
import math
import time
import wave
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.utils.io import atomic_copy, atomic_write_text, write_json
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.vad import VADConfig, detect_speech_segments


@dataclass(frozen=True, slots=True)
class VoiceRefConfig:
    """
    Speaker reference extraction config.

    The ref WAV is always written as 16kHz mono PCM int16.
    """

    target_s: float = 30.0
    overlap_eps_s: float = 0.05
    min_candidate_s: float = 0.7
    max_candidate_s: float = 12.0
    min_speech_ratio: float = 0.60
    silence_pad_ms: int = 80
    vad: VADConfig = VADConfig()
    # Voice store DB file (index) name under voice_store_dir
    index_name: str = "refs.json"


def _read_wav_pcm16(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as wf:
        sr = int(wf.getframerate())
        ch = int(wf.getnchannels())
        sw = int(wf.getsampwidth())
        if ch != 1 or sw != 2:
            raise ValueError(f"expected mono int16 wav; got ch={ch} sw={sw} ({path})")
        frames = wf.readframes(wf.getnframes())
        return sr, frames


def _wav_duration_s(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            n = int(wf.getnframes())
            sr = int(wf.getframerate() or 16000)
        return float(n) / float(sr)
    except Exception:
        return 0.0


def _frames_s(frames: bytes, sr: int) -> float:
    if sr <= 0:
        return 0.0
    return float(len(frames) // 2) / float(sr)


def _rms_norm_int16(buf: bytes) -> float:
    # normalized RMS in [0, 1]
    if not buf:
        return 0.0
    n = max(1, len(buf) // 2)
    s = 0.0
    for i in range(0, len(buf), 2):
        v = int.from_bytes(buf[i : i + 2], "little", signed=True)
        s += float(v * v)
    return math.sqrt(s / n) / 32768.0


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    p = max(0.0, min(1.0, float(p)))
    xs = sorted(vals)
    i = int(round(p * (len(xs) - 1)))
    return float(xs[max(0, min(len(xs) - 1, i))])


@dataclass(frozen=True, slots=True)
class CandidateScore:
    path: Path
    start_s: float
    end_s: float
    speaker_id: str
    duration_s: float
    speech_ratio: float
    rms_dbfs: float
    noise_floor_dbfs: float
    dyn_db: float
    clip_ratio: float
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "start_s": float(self.start_s),
            "end_s": float(self.end_s),
            "speaker_id": str(self.speaker_id),
            "duration_s": float(self.duration_s),
            "speech_ratio": float(self.speech_ratio),
            "rms_dbfs": float(self.rms_dbfs),
            "noise_floor_dbfs": float(self.noise_floor_dbfs),
            "dyn_db": float(self.dyn_db),
            "clip_ratio": float(self.clip_ratio),
            "score": float(self.score),
        }


def _analyze_candidate(wav_path: Path, *, start_s: float, end_s: float, speaker_id: str, cfg: VoiceRefConfig) -> CandidateScore | None:
    dur = max(0.0, float(end_s) - float(start_s))
    if dur <= 0.0:
        return None
    if dur < float(cfg.min_candidate_s):
        return None
    if dur > float(cfg.max_candidate_s):
        # Too long tends to include noise/music; prefer smaller utterances.
        return None

    # Basic format sanity
    try:
        sr, frames = _read_wav_pcm16(wav_path)
    except Exception:
        return None
    if sr != int(cfg.vad.sample_rate):
        return None

    # Trim to VAD speech-only frames (best-effort).
    speech = detect_speech_segments(wav_path, cfg.vad)
    speech_frames = bytearray()
    total_frames = len(frames) // 2
    for s0, e0 in speech:
        i0 = max(0, min(total_frames, int(float(s0) * sr)))
        i1 = max(0, min(total_frames, int(float(e0) * sr)))
        if i1 <= i0:
            continue
        speech_frames += frames[i0 * 2 : i1 * 2]

    total_s = _frames_s(frames, sr)
    speech_s = _frames_s(bytes(speech_frames), sr)
    if total_s <= 1e-6:
        return None
    speech_ratio = float(speech_s / total_s)
    if speech_ratio < float(cfg.min_speech_ratio):
        return None

    # Per-frame RMS distribution (for noise/loudness proxy)
    frame_ms = int(cfg.vad.frame_ms)
    frame_n = max(1, int(sr * (frame_ms / 1000.0)))
    rms_vals: list[float] = []
    clip_cnt = 0
    frame_cnt = 0
    for off in range(0, len(frames), frame_n * 2):
        buf = frames[off : off + frame_n * 2]
        if not buf:
            continue
        frame_cnt += 1
        r = _rms_norm_int16(buf)
        rms_vals.append(r)
        # crude clip detection: any sample near full scale
        with suppress(Exception):
            for j in range(0, len(buf), 2):
                v = int.from_bytes(buf[j : j + 2], "little", signed=True)
                if abs(v) >= 32700:
                    clip_cnt += 1
                    break
    if not rms_vals:
        return None

    rms = max(1e-8, float(_rms_norm_int16(bytes(speech_frames)) or _rms_norm_int16(frames)))
    rms_dbfs = 20.0 * math.log10(rms)

    p20 = _percentile(rms_vals, 0.20)
    p80 = _percentile(rms_vals, 0.80)
    noise_floor = max(1e-8, float(p20))
    noise_floor_dbfs = 20.0 * math.log10(noise_floor)
    dyn_db = 20.0 * math.log10(max(1e-8, float(p80)) / noise_floor)
    clip_ratio = float(clip_cnt) / float(max(1, frame_cnt))

    # Score: prefer speechy, moderate loudness, higher dynamic range, less clipping.
    # This is heuristic; weights are tuned to be stable without extra deps.
    loudness_penalty = abs(rms_dbfs - (-20.0))  # closer to -20dBFS is better
    score = (
        4.0 * speech_ratio
        + 0.15 * dyn_db
        - 0.12 * loudness_penalty
        - 2.0 * clip_ratio
        - 0.05 * max(0.0, noise_floor_dbfs - (-45.0))  # penalize high noise floor
    )

    return CandidateScore(
        path=wav_path,
        start_s=float(start_s),
        end_s=float(end_s),
        speaker_id=str(speaker_id),
        duration_s=float(total_s),
        speech_ratio=float(speech_ratio),
        rms_dbfs=float(rms_dbfs),
        noise_floor_dbfs=float(noise_floor_dbfs),
        dyn_db=float(dyn_db),
        clip_ratio=float(clip_ratio),
        score=float(score),
    )


def _exclude_overlaps(segments: list[dict[str, Any]], *, eps_s: float) -> set[int]:
    """
    Returns indices of segments to drop due to overlap with another speaker.
    """
    items: list[tuple[float, float, str, int]] = []
    for i, s in enumerate(segments):
        try:
            st = float(s.get("start"))
            en = float(s.get("end"))
            sid = str(s.get("speaker_id") or s.get("speaker") or "")
        except Exception:
            continue
        if not sid or en <= st:
            continue
        items.append((st, en, sid, int(i)))
    items.sort(key=lambda x: (x[0], x[1]))

    drop: set[int] = set()
    # sweep: for each segment, compare with later segments whose start < end
    for a in range(len(items)):
        a0, a1, a_sid, a_i = items[a]
        if a_i in drop:
            continue
        for b in range(a + 1, len(items)):
            b0, b1, b_sid, b_i = items[b]
            if b0 >= a1:
                break
            if a_sid == b_sid:
                continue
            ov = max(0.0, min(a1, b1) - max(a0, b0))
            if ov > float(eps_s):
                drop.add(a_i)
                drop.add(b_i)
    return drop


def _concat_wavs(paths: list[Path], *, out_path: Path, cfg: VoiceRefConfig) -> float:
    """
    Concatenate input wavs (16k mono int16) with a small silence pad between.
    Returns duration_s.
    """
    sr = int(cfg.vad.sample_rate)
    pad_frames = max(0, int(float(cfg.silence_pad_ms) / 1000.0 * sr))
    pad = b"\x00\x00" * pad_frames

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_all = bytearray()
    for p in paths:
        try:
            sr2, frames = _read_wav_pcm16(p)
            if sr2 != sr:
                continue
            frames_all += frames
            frames_all += pad
        except Exception:
            continue

    # Trim trailing pad
    if pad and len(frames_all) >= len(pad):
        frames_all = frames_all[: -len(pad)]

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames_all))

    return _frames_s(bytes(frames_all), sr)


def _write_voice_store_index(voice_store_dir: Path, mapping: dict[str, Any], *, cfg: VoiceRefConfig) -> Path:
    voice_store_dir = Path(voice_store_dir).resolve()
    voice_store_dir.mkdir(parents=True, exist_ok=True)
    p = voice_store_dir / str(cfg.index_name)
    payload = {"version": 1, "updated_at": time.time(), "refs": mapping}
    atomic_write_text(p, json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def extract_speaker_refs(
    *,
    segments: list[dict[str, Any]],
    voice_store_dir: Path,
    job_dir: Path | None = None,
    cfg: VoiceRefConfig = VoiceRefConfig(),
    voice_memory_store: Any | None = None,
    voice_memory_max_refs: int = 5,
) -> dict[str, Any]:
    """
    Build per-speaker reference wavs from diarization segments.

    Expected segment shape (from jobs/queue.py diarization.work.json):
      - start, end, speaker_id, wav_path

    Writes:
      - <voice_store_dir>/<speaker_id>/ref.wav
      - (optional) <job_dir>/analysis/voice_refs/manifest.json
      - <voice_store_dir>/refs.json index
      - (optional) enrolls refs into VoiceMemoryStore if provided

    Returns a manifest dict (safe to json-serialize).
    """
    voice_store_dir = Path(voice_store_dir).resolve()
    out: dict[str, Any] = {"version": 1, "created_at": time.time(), "items": {}}

    # Exclude overlap segments across speakers.
    drop = _exclude_overlaps(segments, eps_s=float(cfg.overlap_eps_s))

    # Group candidates by speaker_id
    by_spk: dict[str, list[dict[str, Any]]] = {}
    for i, s in enumerate(segments):
        if i in drop:
            continue
        sid = str(s.get("speaker_id") or s.get("speaker") or "").strip()
        if not sid:
            continue
        by_spk.setdefault(sid, []).append(dict(s))

    # Optional job-local manifest dir
    job_manifest_dir = None
    if job_dir is not None:
        job_manifest_dir = (Path(job_dir) / "analysis" / "voice_refs").resolve()
        job_manifest_dir.mkdir(parents=True, exist_ok=True)

    index_map: dict[str, Any] = {}

    for sid, segs in sorted(by_spk.items(), key=lambda x: x[0]):
        cand_scores: list[CandidateScore] = []
        for s in segs:
            try:
                wp = Path(str(s.get("wav_path") or "")).resolve()
                st = float(s.get("start"))
                en = float(s.get("end"))
            except Exception:
                continue
            if not wp.exists():
                continue
            # Guard against bad paths (e.g., fallback to full audio file): require roughly matching duration.
            expect = max(0.0, float(en) - float(st))
            got = _wav_duration_s(wp)
            if expect > 0.0 and got > max(3.0, expect * 3.0):
                # Looks like a full-file fallback; skip as a candidate.
                continue
            sc = _analyze_candidate(wp, start_s=st, end_s=en, speaker_id=sid, cfg=cfg)
            if sc is not None:
                cand_scores.append(sc)

        cand_scores.sort(key=lambda x: x.score, reverse=True)

        # Select candidates until we hit target duration.
        chosen: list[CandidateScore] = []
        acc = 0.0
        for c in cand_scores:
            if acc >= float(cfg.target_s):
                break
            chosen.append(c)
            acc += float(c.duration_s) * float(max(0.0, min(1.0, c.speech_ratio)))

        # If nothing qualifies, fall back to the longest non-overlap segment (even if low speech ratio).
        warnings: list[str] = []
        if not chosen:
            warnings.append("no_good_candidates")
            best = None
            best_d = 0.0
            for s in segs:
                try:
                    wp = Path(str(s.get("wav_path") or "")).resolve()
                    st = float(s.get("start"))
                    en = float(s.get("end"))
                except Exception:
                    continue
                if not wp.exists():
                    continue
                d = _wav_duration_s(wp)
                if d > best_d:
                    best_d = d
                    best = (wp, st, en)
            if best is not None:
                wp, st, en = best
                sc2 = _analyze_candidate(wp, start_s=st, end_s=en, speaker_id=sid, cfg=VoiceRefConfig(target_s=cfg.target_s, overlap_eps_s=cfg.overlap_eps_s, min_candidate_s=0.0, max_candidate_s=cfg.max_candidate_s, min_speech_ratio=0.0, silence_pad_ms=cfg.silence_pad_ms, vad=cfg.vad, index_name=cfg.index_name))
                if sc2 is not None:
                    chosen = [sc2]
                else:
                    # final fallback: just concat the raw wav without scoring
                    chosen = [
                        CandidateScore(
                            path=wp,
                            start_s=float(st),
                            end_s=float(en),
                            speaker_id=sid,
                            duration_s=float(best_d),
                            speech_ratio=0.0,
                            rms_dbfs=-99.0,
                            noise_floor_dbfs=-99.0,
                            dyn_db=0.0,
                            clip_ratio=0.0,
                            score=-999.0,
                        )
                    ]

        # Build output file
        spk_dir = (voice_store_dir / sid).resolve()
        spk_dir.mkdir(parents=True, exist_ok=True)
        out_ref = spk_dir / "ref.wav"
        tmp_ref = spk_dir / f".ref.tmp.{int(time.time()*1000)}.wav"
        dur_out = _concat_wavs([c.path for c in chosen], out_path=tmp_ref, cfg=cfg)
        if dur_out + 1e-6 < float(cfg.target_s):
            warnings.append(f"insufficient_audio:{dur_out:.2f}s")
        tmp_ref.replace(out_ref)

        # Also write a job-local ref wav for reproducibility / UI playback / pass-2 reruns.
        job_ref = None
        if job_manifest_dir is not None:
            try:
                job_ref = (job_manifest_dir / f"{sid}.wav").resolve()
                atomic_copy(out_ref, job_ref)
            except Exception:
                job_ref = None

        # Update index + job manifest record
        rec = {
            "ref_path": str(out_ref),
            "job_ref_path": str(job_ref) if job_ref is not None else None,
            "duration_s": float(dur_out),
            "target_s": float(cfg.target_s),
            "warnings": warnings,
            "candidates_analyzed": int(len(cand_scores)),
            "segments_used": [c.to_dict() for c in chosen],
        }
        out["items"][sid] = rec
        index_map[sid] = {
            "ref_path": str(out_ref),
            "duration_s": float(dur_out),
            "updated_at": time.time(),
        }

        # Optional: enroll into voice memory store (DB) if provided.
        if voice_memory_store is not None:
            with suppress(Exception):
                # VoiceMemoryStore API: enroll_ref(character_id, wav_path, max_refs=...)
                voice_memory_store.enroll_ref(sid, out_ref, max_refs=int(voice_memory_max_refs))

        if warnings:
            logger.warning("voice_ref_built_with_warnings", speaker_id=sid, warnings=warnings)
        else:
            logger.info("voice_ref_built", speaker_id=sid, duration_s=float(dur_out))

    # Write job-local manifest if requested
    if job_manifest_dir is not None:
        with suppress(Exception):
            write_json(job_manifest_dir / "manifest.json", out, indent=2)

    # Write voice store index (DB)
    try:
        idx = _write_voice_store_index(voice_store_dir, index_map, cfg=cfg)
        out["voice_store_index"] = str(idx)
    except Exception as ex:
        out["voice_store_index_error"] = str(ex)

    return out

