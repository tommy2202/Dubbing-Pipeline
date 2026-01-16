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
from dubbing_pipeline.utils.ffmpeg_safe import extract_audio_mono_16k
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


@dataclass(frozen=True, slots=True)
class ExtractRefsConfig:
    """
    Public config surface for job-local speaker reference extraction.

    Env-mapped settings are provided by `config.public_config.PublicConfig`.
    """

    target_seconds: float = 30.0
    min_seg_seconds: float = 2.0
    max_seg_seconds: float | None = 10.0
    overlap_eps_s: float = 0.05
    min_speech_ratio: float = 0.60
    silence_pad_ms: int = 80
    target_rms_dbfs: float = -20.0


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


def _normalize_wav_rms(path: Path, *, target_rms_dbfs: float) -> None:
    """
    Simple RMS normalization with hard clipping prevention (no external deps).
    """
    try:
        with wave.open(str(path), "rb") as wf:
            sr = int(wf.getframerate())
            ch = int(wf.getnchannels())
            sw = int(wf.getsampwidth())
            if ch != 1 or sw != 2:
                return
            frames = bytearray(wf.readframes(wf.getnframes()))
        if not frames:
            return
        rms = max(1e-8, float(_rms_norm_int16(bytes(frames))))
        cur_db = 20.0 * math.log10(rms)
        gain_db = float(target_rms_dbfs) - float(cur_db)
        # clamp extreme gains
        gain_db = max(-18.0, min(18.0, gain_db))
        gain = 10.0 ** (gain_db / 20.0)
        # apply gain with clipping prevention
        for i in range(0, len(frames), 2):
            v = int.from_bytes(frames[i : i + 2], "little", signed=True)
            vv = int(round(float(v) * float(gain)))
            vv = max(-32768, min(32767, vv))
            frames[i : i + 2] = int(vv).to_bytes(2, "little", signed=True)
        tmp = Path(str(path) + ".norm.tmp")
        with wave.open(str(tmp), "wb") as wf2:
            wf2.setnchannels(1)
            wf2.setsampwidth(2)
            wf2.setframerate(sr)
            wf2.writeframes(bytes(frames))
        tmp.replace(path)
    except Exception:
        return


def _write_voice_store_index(voice_store_dir: Path, mapping: dict[str, Any], *, cfg: VoiceRefConfig) -> Path:
    voice_store_dir = Path(voice_store_dir).resolve()
    voice_store_dir.mkdir(parents=True, exist_ok=True)
    p = voice_store_dir / str(cfg.index_name)
    payload = {"version": 1, "updated_at": time.time(), "refs": mapping}
    atomic_write_text(p, json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def extract_speaker_refs_legacy(
    *,
    segments: list[dict[str, Any]],
    voice_store_dir: Path,
    job_dir: Path | None = None,
    cfg: VoiceRefConfig = VoiceRefConfig(),
    voice_memory_store: Any | None = None,
    voice_memory_max_refs: int = 5,
) -> dict[str, Any]:
    """
    Compatibility wrapper around `extract_speaker_refs` (canonical).

    This preserves the legacy return format and voice_store layout while using the
    canonical ref selection algorithm.
    """
    voice_store_dir = Path(voice_store_dir).resolve()
    out: dict[str, Any] = {"version": 1, "created_at": time.time(), "items": {}}

    # Normalize diarization timeline (prefer per-segment wav_path when present).
    timeline: list[dict[str, Any]] = []
    dialogue_wav: Path | None = None
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        try:
            st = float(s.get("start"))
            en = float(s.get("end"))
        except Exception:
            continue
        sid = str(s.get("speaker_id") or s.get("speaker") or "").strip()
        if not sid or en <= st:
            continue
        wp = str(s.get("wav_path") or "").strip()
        if wp:
            p = Path(wp).resolve()
            if p.exists():
                if dialogue_wav is None:
                    dialogue_wav = p
        timeline.append({"start": st, "end": en, "speaker_id": sid, "wav_path": wp})

    if not timeline or dialogue_wav is None:
        return out

    # Optional job-local manifest dir
    job_manifest_dir = None
    if job_dir is not None:
        job_manifest_dir = (Path(job_dir) / "analysis" / "voice_refs").resolve()
        job_manifest_dir.mkdir(parents=True, exist_ok=True)

    ref_cfg = ExtractRefsConfig(
        target_seconds=float(cfg.target_s),
        min_seg_seconds=float(cfg.min_candidate_s),
        max_seg_seconds=float(cfg.max_candidate_s),
        overlap_eps_s=float(cfg.overlap_eps_s),
        min_speech_ratio=float(cfg.min_speech_ratio),
        silence_pad_ms=int(cfg.silence_pad_ms),
    )

    index_map: dict[str, Any] = {}
    try:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="dubbing_pipeline_voice_refs_") as td:
            tmp_dir = Path(td).resolve()
            man = extract_speaker_refs(
                diarization_timeline=timeline,
                dialogue_wav=dialogue_wav,
                out_dir=tmp_dir,
                config=ref_cfg,
            )
            items = man.get("items") if isinstance(man, dict) else None
            if not isinstance(items, dict):
                items = {}
            for sid, rec in items.items():
                if not isinstance(rec, dict):
                    continue
                rp = str(rec.get("ref_path") or "").strip()
                if not rp:
                    continue
                ref_src = Path(rp).resolve()
                if not ref_src.exists():
                    continue
                spk_dir = (voice_store_dir / str(sid)).resolve()
                spk_dir.mkdir(parents=True, exist_ok=True)
                out_ref = spk_dir / "ref.wav"
                atomic_copy(ref_src, out_ref)

                job_ref = None
                if job_manifest_dir is not None:
                    try:
                        job_ref = (job_manifest_dir / f"{sid}.wav").resolve()
                        atomic_copy(out_ref, job_ref)
                    except Exception:
                        job_ref = None

                warnings = rec.get("warnings") if isinstance(rec.get("warnings"), list) else []
                segs_used = (
                    rec.get("segments_used") if isinstance(rec.get("segments_used"), list) else []
                )
                duration_s = float(rec.get("duration_sec") or 0.0)
                out["items"][str(sid)] = {
                    "ref_path": str(out_ref),
                    "job_ref_path": str(job_ref) if job_ref is not None else None,
                    "duration_s": float(duration_s),
                    "target_s": float(cfg.target_s),
                    "warnings": warnings,
                    "candidates_analyzed": int(rec.get("segments_used_count") or len(segs_used)),
                    "segments_used": segs_used,
                }
                index_map[str(sid)] = {
                    "ref_path": str(out_ref),
                    "duration_s": float(duration_s),
                    "updated_at": time.time(),
                }

                # Optional: enroll into voice memory store (DB) if provided.
                if voice_memory_store is not None:
                    with suppress(Exception):
                        voice_memory_store.enroll_ref(
                            str(sid), out_ref, max_refs=int(voice_memory_max_refs)
                        )
    except Exception as ex:
        logger.warning("voice_ref_legacy_wrapper_failed", error=str(ex))

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


def extract_speaker_refs(
    diarization_timeline: list[dict[str, Any]],
    dialogue_wav: Path,
    out_dir: Path,
    config: ExtractRefsConfig,
) -> dict[str, Any]:
    """
    Canonical job-local speaker reference extraction.

    Inputs:
    - diarization_timeline: list of dicts with at least {start,end,speaker_id}. Optional wav_path.
    - dialogue_wav: source audio; used only when timeline entries don't provide wav_path.
    - out_dir: directory to write speaker refs + manifest.json
    - config: ExtractRefsConfig

    Outputs (per speaker):
    - <out_dir>/<speaker_id>_ref.wav
    - <out_dir>/manifest.json
    """
    dialogue_wav = Path(dialogue_wav).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = VoiceRefConfig(
        target_s=float(config.target_seconds),
        overlap_eps_s=float(config.overlap_eps_s),
        min_candidate_s=float(config.min_seg_seconds),
        max_candidate_s=float(config.max_seg_seconds if config.max_seg_seconds is not None else 1e9),
        min_speech_ratio=float(config.min_speech_ratio),
        silence_pad_ms=int(config.silence_pad_ms),
    )

    out: dict[str, Any] = {
        "version": 1,
        "created_at": time.time(),
        "dialogue_wav": str(dialogue_wav),
        "out_dir": str(out_dir),
        "config": {
            "target_seconds": float(config.target_seconds),
            "min_seg_seconds": float(config.min_seg_seconds),
            "max_seg_seconds": (
                float(config.max_seg_seconds) if config.max_seg_seconds is not None else None
            ),
            "overlap_eps_s": float(config.overlap_eps_s),
            "min_speech_ratio": float(config.min_speech_ratio),
            "silence_pad_ms": int(config.silence_pad_ms),
            "target_rms_dbfs": float(config.target_rms_dbfs),
        },
        "items": {},
    }

    # Normalize timeline format
    norm: list[dict[str, Any]] = []
    for seg in diarization_timeline or []:
        if not isinstance(seg, dict):
            continue
        try:
            st = float(seg.get("start"))
            en = float(seg.get("end"))
        except Exception:
            continue
        sid = str(seg.get("speaker_id") or seg.get("speaker") or "").strip()
        if not sid or en <= st:
            continue
        wp = str(seg.get("wav_path") or "").strip()
        norm.append({"start": st, "end": en, "speaker_id": sid, "wav_path": wp})

    # Exclude overlap segments across speakers.
    drop = _exclude_overlaps(norm, eps_s=float(cfg.overlap_eps_s))

    # Group by speaker_id
    by_spk: dict[str, list[tuple[int, float, float, str]]] = {}
    for i, s in enumerate(norm):
        if i in drop:
            continue
        by_spk.setdefault(str(s["speaker_id"]), []).append(
            (int(i), float(s["start"]), float(s["end"]), str(s.get("wav_path") or ""))
        )

    tmp_dir = out_dir / "_segments"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for speaker_id, segs in sorted(by_spk.items(), key=lambda x: x[0]):
        rejected: list[dict[str, Any]] = []
        cand_scores: list[CandidateScore] = []
        for idx, st, en, wp0 in segs:
            dur = max(0.0, float(en) - float(st))
            if dur < float(config.min_seg_seconds):
                rejected.append({"start": st, "end": en, "reason": "too_short"})
                continue
            if config.max_seg_seconds is not None and dur > float(config.max_seg_seconds):
                rejected.append({"start": st, "end": en, "reason": "too_long"})
                continue

            wav_path: Path | None = None
            if wp0:
                with suppress(Exception):
                    p = Path(wp0).resolve()
                    if p.exists() and p.is_file():
                        wav_path = p
            if wav_path is None:
                wav_path = (tmp_dir / f"{speaker_id}_{idx:04d}.wav").resolve()
                try:
                    extract_audio_mono_16k(
                        src=dialogue_wav, dst=wav_path, start_s=float(st), end_s=float(en), timeout_s=120
                    )
                except Exception as ex:
                    rejected.append({"start": st, "end": en, "reason": f"slice_failed:{ex}"})
                    continue

            got = _wav_duration_s(wav_path)
            if got > max(3.0, dur * 3.0):
                rejected.append({"start": st, "end": en, "reason": "bad_candidate_duration"})
                continue

            sc = _analyze_candidate(wav_path, start_s=st, end_s=en, speaker_id=speaker_id, cfg=cfg)
            if sc is None:
                rejected.append({"start": st, "end": en, "reason": "score_rejected"})
                continue
            cand_scores.append(sc)

        cand_scores.sort(key=lambda x: x.score, reverse=True)

        chosen: list[CandidateScore] = []
        acc = 0.0
        for c in cand_scores:
            if acc >= float(config.target_seconds):
                break
            chosen.append(c)
            acc += float(c.duration_s) * float(max(0.0, min(1.0, c.speech_ratio)))

        warnings: list[str] = []
        if not chosen:
            warnings.append("no_good_candidates")
            # fallback: pick longest non-overlap segment (even if low speech ratio)
            best = None
            best_d = 0.0
            for idx, st, en, wp0 in segs:
                d = max(0.0, float(en) - float(st))
                if d > best_d:
                    best_d = d
                    best = (idx, st, en, wp0)
            if best is not None:
                idx, st, en, wp0 = best
                wav_path: Path | None = None
                if wp0:
                    with suppress(Exception):
                        p = Path(wp0).resolve()
                        if p.exists():
                            wav_path = p
                if wav_path is None:
                    wav_path = (tmp_dir / f"{speaker_id}_{idx:04d}.fallback.wav").resolve()
                    with suppress(Exception):
                        extract_audio_mono_16k(
                            src=dialogue_wav, dst=wav_path, start_s=float(st), end_s=float(en), timeout_s=120
                        )
                if wav_path is not None and wav_path.exists():
                    sc2 = _analyze_candidate(
                        wav_path,
                        start_s=st,
                        end_s=en,
                        speaker_id=speaker_id,
                        cfg=VoiceRefConfig(
                            target_s=cfg.target_s,
                            overlap_eps_s=cfg.overlap_eps_s,
                            min_candidate_s=0.0,
                            max_candidate_s=cfg.max_candidate_s,
                            min_speech_ratio=0.0,
                            silence_pad_ms=cfg.silence_pad_ms,
                            vad=cfg.vad,
                            index_name=cfg.index_name,
                        ),
                    )
                    if sc2 is not None:
                        chosen = [sc2]

        # Logging: rejected + selected
        for rj in rejected:
            logger.info(
                "voice_ref_reject",
                speaker_id=str(speaker_id),
                start_s=float(rj.get("start") or 0.0),
                end_s=float(rj.get("end") or 0.0),
                reason=str(rj.get("reason") or ""),
            )
        logger.info(
            "voice_ref_select",
            speaker_id=str(speaker_id),
            selected=[
                {"start_s": float(c.start_s), "end_s": float(c.end_s), "score": float(c.score)}
                for c in chosen
            ],
        )

        ref_out = (out_dir / f"{speaker_id}_ref.wav").resolve()
        tmp_ref = (out_dir / f".{speaker_id}.ref.tmp.{int(time.time()*1000)}.wav").resolve()
        dur_out = _concat_wavs([c.path for c in chosen], out_path=tmp_ref, cfg=cfg)
        if dur_out + 1e-6 < float(config.target_seconds):
            warnings.append(f"insufficient_audio:{dur_out:.2f}s")
        tmp_ref.replace(ref_out)
        _normalize_wav_rms(ref_out, target_rms_dbfs=float(config.target_rms_dbfs))

        out["items"][speaker_id] = {
            "ref_path": str(ref_out),
            "duration_sec": float(dur_out),
            "segments_used": [{"start_s": float(c.start_s), "end_s": float(c.end_s)} for c in chosen],
            "segments_used_count": int(len(chosen)),
            "warnings": warnings,
            "rejected": rejected,
        }
        logger.info(
            "voice_ref_done",
            speaker_id=str(speaker_id),
            duration_sec=float(dur_out),
            warnings=warnings,
        )

    with suppress(Exception):
        write_json(out_dir / "manifest.json", out, indent=2)
    return out

