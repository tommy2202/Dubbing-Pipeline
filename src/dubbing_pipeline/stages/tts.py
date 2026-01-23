from __future__ import annotations

import subprocess
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any

from dubbing_pipeline.cache.store import cache_get, cache_put, make_key
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from dubbing_pipeline.stages.tts_engine import CoquiXTTS, choose_similar_voice
from dubbing_pipeline.utils.circuit import Circuit
from dubbing_pipeline.utils.ffmpeg_safe import run_ffmpeg
from dubbing_pipeline.utils.io import atomic_copy, read_json, write_json
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.retry import retry_call


class TTSCanceled(Exception):
    pass


def _parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = [b for b in text.split("\n\n") if b.strip()]
    cues: list[dict] = []

    def parse_ts(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = (p.strip() for p in lines[1].split("-->", 1))
        start = parse_ts(start_s)
        end = parse_ts(end_s)
        cue_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append({"start": start, "end": end, "speaker_id": "Speaker1", "text": cue_text})
    return cues


def _ffmpeg_to_pcm16k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg cannot safely overwrite input in-place
    s = get_settings()
    if src.resolve() == dst.resolve():
        tmp = dst.with_suffix(".tmp.wav")
        run_ffmpeg(
            [str(s.ffmpeg_bin), "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(tmp)],
            timeout_s=120,
            retries=1,
            capture=True,
        )
        tmp.replace(dst)
        return
    run_ffmpeg(
        [str(s.ffmpeg_bin), "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
        timeout_s=120,
        retries=1,
        capture=True,
    )


def _emotion_controls(text: str, *, mode: str) -> tuple[str, float, float, float]:
    """
    Backwards-compatible wrapper around Tier-3B expressive policy.
    """
    from dubbing_pipeline.expressive.prosody import categorize

    t = str(text or "")
    m = (mode or "off").strip().lower()
    if m not in {"off", "auto", "tags"}:
        m = "off"
    if m == "tags":
        import re

        mm = re.match(r"^\s*\[([a-zA-Z0-9_\-]+)\]\s*(.*)$", t)
        if mm:
            tag = mm.group(1).lower()
            t2 = mm.group(2)
            presets = {
                "happy": (1.02, 1.06, 1.10),
                "sad": (0.95, 0.94, 0.85),
                "angry": (1.05, 1.02, 1.25),
                "calm": (0.98, 0.98, 0.90),
            }
            if tag in presets:
                r0, p0, e0 = presets[tag]
                return t2, r0, p0, e0
            return t2, 1.0, 1.0, 1.0
        return t, 1.0, 1.0, 1.0
    if m == "auto":
        cat, _sig = categorize(rms=None, pitch_hz=None, text=t)
        if cat == "excited":
            return t, 1.02, 1.05, 1.10
        if cat == "angry":
            return t, 1.04, 1.02, 1.20
        if cat == "sad":
            return t, 0.96, 0.97, 0.90
        if cat == "calm":
            return t, 0.99, 0.99, 0.93
    return t, 1.0, 1.0, 1.0


def _write_silence_wav(path: Path, *, duration_s: float, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(0, int(duration_s * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # pcm_s16le
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * frames)


def _espeak_fallback(text: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["espeak-ng", "-w", str(out_path), text],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        raise RuntimeError(f"espeak-ng failed: {ex}") from ex


def render_aligned_track(
    lines: list[dict], clip_paths: list[Path], out_wav: Path, *, sr: int = 16000
) -> None:
    """
    Render a single aligned WAV track by padding silence between segments.
    Best-effort: if clips overlap, later clips overwrite earlier samples.
    """
    if not lines or not clip_paths:
        _write_silence_wav(out_wav, duration_s=0.0, sr=sr)
        return

    end_t = max(float(line["end"]) for line in lines)
    total_frames = max(1, int(end_t * sr))
    buf = bytearray(b"\x00\x00" * total_frames)

    for line, clip in zip(lines, clip_paths, strict=False):
        start = float(line["start"])
        start_i = max(0, int(start * sr))
        try:
            with wave.open(str(clip), "rb") as wf:
                if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != sr:
                    raise ValueError("clip must be 16kHz mono pcm16")
                frames = wf.readframes(wf.getnframes())
        except Exception:
            continue

        n = len(frames) // 2
        end_i = min(total_frames, start_i + n)
        dst_off = start_i * 2
        src_off = 0
        buf[dst_off : end_i * 2] = frames[src_off : (end_i - start_i) * 2]

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(buf))


def run(
    *,
    out_dir: Path,
    transcript_srt: Path | None = None,
    translated_json: Path | None = None,
    diarization_json: Path | None = None,
    wav_out: Path | None = None,
    voice_map_json_path: Path | None = None,
    tts_lang: str | None = None,
    tts_speaker: str | None = None,
    tts_speaker_wav: Path | None = None,
    voice_mode: str | None = None,
    no_clone: bool | None = None,
    two_pass_enabled: bool | None = None,
    two_pass_phase: str | None = None,
    series_slug: str | None = None,
    speaker_character_map: dict[str, str] | None = None,
    voice_ref_dir: Path | None = None,
    voice_store_dir: Path | None = None,
    voice_memory: bool | None = None,
    voice_memory_dir: Path | None = None,
    voice_match_threshold: float | None = None,
    voice_auto_enroll: bool | None = None,
    voice_character_map: Path | None = None,
    review_state_path: Path | None = None,
    tts_provider: str | None = None,
    emotion_mode: str | None = None,
    speech_rate: float | None = None,
    pitch: float | None = None,
    energy: float | None = None,
    expressive: str | None = None,
    expressive_strength: float | None = None,
    expressive_debug: bool | None = None,
    source_audio_wav: Path | None = None,
    music_regions_path: Path | None = None,
    director: bool | None = None,
    director_strength: float | None = None,
    pacing: bool | None = None,
    pacing_min_ratio: float | None = None,
    pacing_max_ratio: float | None = None,
    timing_tolerance: float | None = None,
    timing_debug: bool | None = None,
    progress_cb=None,
    cancel_cb=None,
    max_stretch: float = 0.15,
    job_id: str | None = None,
    audio_hash: str | None = None,
    **_,
) -> Path:
    """
    TTS stage:
      - reads translated lines (preferred) or falls back to SRT cues
      - tries cloning via speaker wav (from extracted speaker refs or env TTS_SPEAKER_WAV)
      - on failure, falls back to preset voice ID (default or best match)
      - writes per-line clips and a combined aligned track: <stem>.tts.wav
    """
    settings = get_settings()
    eff_tts_lang = (tts_lang or settings.tts_lang) or "en"
    eff_tts_speaker = (tts_speaker or settings.tts_speaker) or "default"
    eff_tts_speaker_wav: Path | None = tts_speaker_wav or settings.tts_speaker_wav
    eff_voice_map_json_path: Path | None = voice_map_json_path or (
        Path(settings.voice_map_json) if settings.voice_map_json else None
    )
    eff_voice_mode = (voice_mode or settings.voice_mode or "clone").strip().lower()
    eff_no_clone = bool(no_clone) if no_clone is not None else False
    if eff_voice_mode not in {"clone", "preset", "single"}:
        eff_voice_mode = "clone"
    eff_voice_ref_dir = voice_ref_dir or settings.voice_ref_dir
    eff_voice_store_dir = Path(voice_store_dir or settings.voice_store_dir).resolve()
    eff_voice_memory = (
        bool(voice_memory) if voice_memory is not None else bool(settings.voice_memory)
    )
    eff_voice_memory_dir = Path(voice_memory_dir or settings.voice_memory_dir).resolve()
    eff_voice_character_map = (
        Path(voice_character_map).resolve()
        if voice_character_map is not None
        else (
            Path(settings.voice_character_map).resolve()
            if getattr(settings, "voice_character_map", None)
            else None
        )
    )
    eff_tts_provider = (tts_provider or settings.tts_provider or "auto").strip().lower()
    if eff_tts_provider not in {"auto", "xtts", "basic", "espeak"}:
        eff_tts_provider = "auto"
    eff_emotion_mode = (emotion_mode or settings.emotion_mode or "off").strip().lower()
    eff_rate = float(speech_rate if speech_rate is not None else settings.speech_rate)
    eff_pitch = float(pitch if pitch is not None else settings.pitch)
    eff_energy = float(energy if energy is not None else settings.energy)
    eff_expressive = (expressive or settings.expressive or "off").strip().lower()
    if eff_expressive not in {"off", "auto", "source-audio", "text-only"}:
        eff_expressive = "off"
    eff_expressive_strength = float(
        expressive_strength if expressive_strength is not None else settings.expressive_strength
    )
    eff_expressive_debug = bool(
        expressive_debug if expressive_debug is not None else bool(settings.expressive_debug)
    )
    eff_director = (
        bool(director) if director is not None else bool(getattr(settings, "director", False))
    )
    eff_director_strength = float(
        director_strength
        if director_strength is not None
        else float(getattr(settings, "director_strength", 0.5))
    )
    eff_pacing = bool(pacing) if pacing is not None else bool(settings.pacing)
    eff_pacing_min = (
        float(pacing_min_ratio)
        if pacing_min_ratio is not None
        else float(settings.pacing_min_ratio)
    )
    eff_pacing_max = (
        float(pacing_max_ratio)
        if pacing_max_ratio is not None
        else float(settings.pacing_max_ratio)
    )
    eff_tol = (
        float(timing_tolerance)
        if timing_tolerance is not None
        else float(settings.timing_tolerance)
    )
    eff_debug = bool(timing_debug) if timing_debug is not None else bool(settings.timing_debug)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / ".checkpoint.json"

    # Optional per-speaker preset mapping (voice bank map):
    #   {"SPEAKER_01": "alice", "SPEAKER_02": "bob"}
    voice_map: dict[str, str] = {}
    try:
        voice_map_path = settings.voice_bank_map_path or settings.voice_map_path
        if voice_map_path:
            vm = read_json(Path(voice_map_path), default={})
            if isinstance(vm, dict):
                voice_map = {
                    str(k): str(v) for k, v in vm.items() if str(k).strip() and str(v).strip()
                }
    except Exception:
        voice_map = {}

    # Optional rich voice map (per-job mapping) for per-speaker overrides.
    # Format: {"items":[{"character_id": "...", "speaker_strategy": "preset|zero-shot", "tts_speaker": "...", "tts_speaker_wav": "..."}]}
    per_speaker_wav_override: dict[str, Path] = {}
    per_speaker_preset_override: dict[str, str] = {}
    per_speaker_voice_mode: dict[str, str] = {}
    per_speaker_keep_original: set[str] = set()
    try:
        vmj = str(eff_voice_map_json_path) if eff_voice_map_json_path else ""
        if vmj:
            data = read_json(Path(vmj), default={})
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                for it in data.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    cid = str(it.get("character_id") or "").strip()
                    strat = str(it.get("speaker_strategy") or it.get("strategy") or "").strip().lower()
                    if not cid:
                        continue
                    if strat in {"original", "keep-original", "keep_original", "keep"}:
                        per_speaker_keep_original.add(cid)
                        per_speaker_voice_mode[cid] = "original"
                        continue
                    if strat in {"preset"}:
                        per_speaker_voice_mode[cid] = "preset"
                    if strat in {"zero-shot", "zeroshot", "clone"}:
                        per_speaker_voice_mode[cid] = "clone"
                    if strat in {"zero-shot", "zeroshot", "clone"}:
                        wp = str(it.get("tts_speaker_wav") or "").strip()
                        if wp:
                            p = Path(wp)
                            if p.exists():
                                per_speaker_wav_override[cid] = p
                    if strat in {"preset"}:
                        spk = str(it.get("tts_speaker") or "").strip()
                        if spk:
                            per_speaker_preset_override[cid] = spk
    except Exception:
        per_speaker_wav_override = {}
        per_speaker_preset_override = {}
        per_speaker_voice_mode = {}
        per_speaker_keep_original = set()

    # Determine lines to synthesize
    lines: list[dict]
    if translated_json and translated_json.exists():
        data = read_json(translated_json, default={})
        if isinstance(data, dict):
            if isinstance(data.get("lines"), list):
                lines = list(data.get("lines", []))
            elif isinstance(data.get("segments"), list):
                # Accept translation manager output: segments with "speaker"
                segs = list(data.get("segments", []))
                lines = []
                for s in segs:
                    try:
                        lines.append(
                            {
                                "start": float(s["start"]),
                                "end": float(s["end"]),
                                "speaker_id": str(
                                    s.get("speaker") or s.get("speaker_id") or "SPEAKER_01"
                                ),
                                "text": str(s.get("text") or ""),
                                "text_pre_fit": str(s.get("text_pre_fit") or ""),
                            }
                        )
                    except Exception:
                        continue
            else:
                lines = []
        else:
            lines = []
    elif transcript_srt and transcript_srt.exists():
        lines = _parse_srt(transcript_srt)
    else:
        lines = []

    # Tier-2B: review loop integration (locked segments).
    locked: dict[int, dict[str, str]] = {}
    try:
        rsp = (
            Path(review_state_path).resolve()
            if review_state_path is not None
            else (out_dir / "review" / "state.json")
        )
        if rsp.exists():
            st = read_json(rsp, default={})
            segs = st.get("segments", []) if isinstance(st, dict) else []
            if isinstance(segs, list):
                for s in segs:
                    if not isinstance(s, dict):
                        continue
                    if str(s.get("status") or "") != "locked":
                        continue
                    sid = int(s.get("segment_id") or 0)
                    ap = str(s.get("audio_path_current") or "")
                    ct = str(s.get("chosen_text") or "")
                    if sid > 0 and ap:
                        locked[sid] = {"audio_path": ap, "chosen_text": ct}
    except Exception:
        locked = {}

    # Speaker embeddings from diarization artifacts (optional; used for preset matching).
    speaker_embeddings: dict[str, Path] = {}
    if diarization_json and diarization_json.exists():
        diar = read_json(diarization_json, default={})
        if isinstance(diar, dict):
            emb_map = diar.get("speaker_embeddings", {})
            if isinstance(emb_map, dict):
                for sid, p in emb_map.items():
                    with suppress(Exception):
                        speaker_embeddings[str(sid)] = Path(str(p))

    # Tier-2A voice memory store (optional)
    vm_store = None
    vm_manual: dict[str, str] = {}
    if eff_voice_memory:
        try:
            from dubbing_pipeline.voice_memory.store import VoiceMemoryStore

            vm_store = VoiceMemoryStore(eff_voice_memory_dir)
            if eff_voice_character_map and eff_voice_character_map.exists():
                data = read_json(eff_voice_character_map, default={})
                if isinstance(data, dict):
                    vm_manual = {
                        str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()
                    }
        except Exception as ex:
            logger.warning("[dp] voice-memory unavailable; continuing without it (%s)", ex)
            vm_store = None

    # Engine (lazy-loaded per run)
    engine: CoquiXTTS | None = None
    cb = Circuit.get("tts")
    try:
        engine = CoquiXTTS()
    except Exception as ex:
        logger.warning("[dp] XTTS unavailable: %s", ex)

    clips_dir = out_dir / "tts_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: list[Path] = []
    music_suppressed = 0
    _director_plans = []
    speaker_report: dict[str, dict[str, Any]] = {}
    _saved_character_refs: set[tuple[str, str]] = set()

    voice_db_embeddings_dir = (settings.voice_db_path.parent / "embeddings").resolve()

    def _rep(speaker_id: str) -> dict[str, Any]:
        sid = str(speaker_id or "default")
        rec = speaker_report.get(sid)
        if rec is None:
            rec = {
                "speaker_id": sid,
                "segments": 0,
                "clone_attempted": False,
                "clone_succeeded": False,
                "refs_used": [],
                "providers": {},
                "fallback_reasons": [],
            }
            speaker_report[sid] = rec
        return rec

    def _note_segment(
        *,
        speaker_id: str,
        provider: str,
        ref_path: Path | None,
        clone_attempted: bool,
        clone_succeeded: bool,
        fallback_reason: str | None,
    ) -> None:
        r = _rep(speaker_id)
        r["segments"] = int(r.get("segments") or 0) + 1
        r["clone_attempted"] = bool(r.get("clone_attempted") or False) or bool(clone_attempted)
        r["clone_succeeded"] = bool(r.get("clone_succeeded") or False) or bool(clone_succeeded)
        prov = str(provider or "unknown")
        provs = r.get("providers")
        if not isinstance(provs, dict):
            provs = {}
            r["providers"] = provs
        provs[prov] = int(provs.get(prov) or 0) + 1
        if ref_path is not None:
            sref = str(Path(ref_path).resolve())
            refs = r.get("refs_used")
            if not isinstance(refs, list):
                refs = []
                r["refs_used"] = refs
            if sref not in refs:
                refs.append(sref)
        if fallback_reason:
            fr = str(fallback_reason)
            frs = r.get("fallback_reasons")
            if not isinstance(frs, list):
                frs = []
                r["fallback_reasons"] = frs
            if fr not in frs:
                frs.append(fr)

    # Feature D: per-job overrides (speaker override; optional)
    speaker_overrides: dict[str, str] = {}
    try:
        from dubbing_pipeline.review.overrides import load_overrides

        ov = load_overrides(out_dir)
        sp = ov.get("speaker_overrides", {}) if isinstance(ov, dict) else {}
        if isinstance(sp, dict):
            speaker_overrides = {str(k): str(v) for k, v in sp.items() if str(v).strip()}
    except Exception:
        speaker_overrides = {}

    # Tier-Next A/B: optional music preservation (skip dubbing in these regions)
    music_regions: list[dict] = []
    if music_regions_path is not None:
        try:
            data = read_json(Path(music_regions_path), default={})
            regs = data.get("regions", []) if isinstance(data, dict) else []
            if isinstance(regs, list):
                music_regions = [r for r in regs if isinstance(r, dict)]
        except Exception:
            music_regions = []

    total = len(lines)
    if progress_cb is not None:
        with suppress(Exception):
            progress_cb(0, total)

    for i, line in enumerate(lines):
        should_cancel = False
        if cancel_cb is not None:
            try:
                should_cancel = bool(cancel_cb())
            except Exception:
                # ignore cancel callback errors
                should_cancel = False
        if should_cancel:
            raise TTSCanceled()

        seg_start = float(line.get("start", 0.0))
        seg_end = float(line.get("end", 0.0))
        text = str(line.get("text", "") or "").strip()
        text, emo_rate, emo_pitch, emo_energy = _emotion_controls(text, mode=eff_emotion_mode)
        rate_mul = float(eff_rate) * float(emo_rate)
        pitch_mul = float(eff_pitch) * float(emo_pitch)
        energy_mul = float(eff_energy) * float(emo_energy)

        # Segment index is 1-based; allow per-segment forced character_id.
        forced = speaker_overrides.get(str(i + 1))
        speaker_id = str(forced or line.get("speaker_id") or eff_tts_speaker or "default")
        if eff_voice_mode == "single":
            speaker_id = str(eff_tts_speaker or "default")
        pause_tail_ms = 0

        # Feature K: per-character delivery profiles (voice memory + optional project profile overlay).
        # Defaults preserve behavior unless a profile field is explicitly set.
        delivery: dict[str, Any] = {}
        try:
            dp_path = (out_dir / "analysis" / "delivery_profiles.json").resolve()
            if dp_path.exists():
                dpx = read_json(dp_path, default={})
                chars = dpx.get("characters") if isinstance(dpx, dict) else None
                if isinstance(chars, dict):
                    row = chars.get(str(speaker_id))
                    if isinstance(row, dict):
                        delivery.update(dict(row))
        except Exception:
            delivery = {}
        try:
            if vm_store is not None:
                cid = vm_manual.get(speaker_id, speaker_id)
                vmd = vm_store.get_delivery_profile(str(cid))
                if isinstance(vmd, dict) and vmd:
                    delivery.update(dict(vmd))
        except Exception:
            pass

        # speaking rate multiplier (applied to prosody rate)
        try:
            rm = delivery.get("rate_mul")
            if rm is not None and str(rm).strip() != "":
                rate_mul *= float(rm)
        except Exception:
            pass

        pause_style = str(delivery.get("pause_style") or "").strip().lower()
        if pause_style:
            line["pause_style"] = pause_style
        pref_vm = str(delivery.get("preferred_voice_mode") or "").strip().lower()
        if pref_vm in {"clone", "preset", "single"}:
            line["preferred_voice_mode"] = pref_vm
            if pref_vm == "single":
                speaker_id = str(eff_tts_speaker or "default")

        # Tier-3B expressive mode (optional; OFF by default)
        if eff_expressive != "off":
            try:
                from dubbing_pipeline.expressive.policy import plan_for_segment, write_plan_json
                from dubbing_pipeline.expressive.prosody import ProsodyFeatures, analyze_segment

                feats: ProsodyFeatures | None = None
                seg_id = int(i + 1)
                start_s = float(line.get("start", 0.0))
                end_s = float(line.get("end", 0.0))
                if eff_expressive == "source-audio" and source_audio_wav is not None:
                    exp_tmp = out_dir / "expressive" / "tmp"
                    exp_tmp.mkdir(parents=True, exist_ok=True)
                    seg_wav = exp_tmp / f"{seg_id:04d}.wav"
                    if not seg_wav.exists():
                        feats = analyze_segment(
                            source_audio_wav=Path(source_audio_wav),
                            start_s=start_s,
                            end_s=end_s,
                            text=text,
                            out_wav=seg_wav,
                            pitch=True,
                        )
                    else:
                        # If it exists, compute only cheap stats
                        feats = analyze_segment(
                            source_audio_wav=Path(source_audio_wav),
                            start_s=start_s,
                            end_s=end_s,
                            text=text,
                            out_wav=seg_wav,
                            pitch=False,
                        )
                elif eff_expressive in {"auto", "text-only"}:
                    feats = None

                seg_strength = float(eff_expressive_strength)
                try:
                    es = delivery.get("expressive_strength")
                    if es is not None and str(es).strip() != "":
                        seg_strength = float(es)
                except Exception:
                    pass
                plan = plan_for_segment(
                    segment_id=seg_id,
                    mode=eff_expressive,
                    strength=float(seg_strength),
                    text=text,
                    features=feats,
                )
                rate_mul *= float(plan.rate_mul)
                pitch_mul *= float(plan.pitch_mul)
                energy_mul *= float(plan.energy_mul)
                pause_tail_ms = max(pause_tail_ms, int(getattr(plan, "pause_tail_ms", 0) or 0))
                if eff_expressive_debug:
                    out_plan = out_dir / "expressive" / "plans" / f"{seg_id:04d}.json"
                    write_plan_json(plan, out_plan, features=feats)
            except Exception:
                pass

        # Tier-Next G: Dub Director mode (optional; OFF by default)
        if eff_director:
            with suppress(Exception):
                from dubbing_pipeline.expressive.director import plan_for_segment

                seg_id = int(i + 1)
                d_strength = float(eff_director_strength)
                try:
                    es = delivery.get("expressive_strength")
                    if es is not None and str(es).strip() != "":
                        d_strength = float(es)
                except Exception:
                    pass
                plan = plan_for_segment(
                    segment_id=seg_id,
                    text=text,
                    start_s=float(line.get("start", 0.0)),
                    end_s=float(line.get("end", 0.0)),
                    source_audio_wav=(
                        Path(source_audio_wav) if source_audio_wav is not None else None
                    ),
                    strength=float(d_strength),
                )
                rate_mul *= float(plan.rate_mul)
                pitch_mul *= float(plan.pitch_mul)
                energy_mul *= float(plan.energy_mul)
                pause_tail_ms = max(pause_tail_ms, int(getattr(plan, "pause_tail_ms", 0) or 0))
                _director_plans.append(plan)

        # Apply pause-style scaling (only if we actually have a pause tail).
        if pause_tail_ms > 0:
            mul = 1.0
            if pause_style == "tight":
                mul = 0.6
            elif pause_style in {"", "default", "normal"}:
                mul = 1.0
            elif pause_style == "dramatic":
                mul = 1.5
            pause_tail_ms = int(max(0, min(800, int(float(pause_tail_ms) * float(mul)))))
        if not text:
            # keep timing, but skip synthesis (silence clip)
            clip = clips_dir / f"{i:04d}_{speaker_id}.wav"
            _write_silence_wav(clip, duration_s=max(0.0, float(line["end"]) - float(line["start"])))
            _ffmpeg_to_pcm16k(clip, clip)
            _note_segment(
                speaker_id=speaker_id,
                provider="silence",
                ref_path=None,
                clone_attempted=False,
                clone_succeeded=False,
                fallback_reason="empty_text",
            )
            clip_paths.append(clip)
            if progress_cb is not None:
                with suppress(Exception):
                    progress_cb(i + 1, total)
            continue

        # If this segment overlaps a detected music region, suppress dubbing (silence clip)
        try:
            from dubbing_pipeline.audio.music_detect import should_suppress_segment

            if music_regions and should_suppress_segment(seg_start, seg_end, music_regions):
                clip = clips_dir / f"{i:04d}_{speaker_id}.wav"
                _write_silence_wav(clip, duration_s=max(0.0, seg_end - seg_start))
                _ffmpeg_to_pcm16k(clip, clip)
                clip_paths.append(clip)
                music_suppressed += 1
                _note_segment(
                    speaker_id=speaker_id,
                    provider="silence",
                    ref_path=None,
                    clone_attempted=False,
                    clone_succeeded=False,
                    fallback_reason="music_suppressed",
                )
                logger.info("music_suppress_segment", idx=i + 1, start_s=seg_start, end_s=seg_end)
                if progress_cb is not None:
                    with suppress(Exception):
                        progress_cb(i + 1, total)
                continue
        except Exception:
            pass

        raw_clip = clips_dir / f"{i:04d}_{speaker_id}.raw.wav"
        clip = clips_dir / f"{i:04d}_{speaker_id}.wav"

        # If locked, reuse locked audio and skip synthesis.
        lock_rec = locked.get(i + 1)
        if lock_rec:
            p0 = Path(str(lock_rec.get("audio_path") or ""))
            if p0.exists():
                try:
                    chosen = str(lock_rec.get("chosen_text") or "").strip()
                    if chosen:
                        line["text"] = chosen
                    tmp_locked = clips_dir / f"{i:04d}_{speaker_id}.locked.wav"
                    atomic_copy(p0, tmp_locked)
                    _ffmpeg_to_pcm16k(tmp_locked, clip)
                    clip_paths.append(clip)
                    _note_segment(
                        speaker_id=speaker_id,
                        provider="locked",
                        ref_path=None,
                        clone_attempted=False,
                        clone_succeeded=False,
                        fallback_reason=None,
                    )
                    if progress_cb is not None:
                        with suppress(Exception):
                            progress_cb(i + 1, total)
                    continue
                except Exception:
                    # fall through to normal synthesis
                    pass

        # Choose clone speaker wav priority:
        # 1) explicit per-speaker override (job map / UI override)
        # 2) persistent character ref (series-scoped) when speaker->character mapping exists
        # 3) per-speaker extracted ref (this job; voice_ref_dir)
        # 4) global TTS_SPEAKER_WAV
        # 5) default voice preset (handled later via preset selection)
        speaker_wav = per_speaker_wav_override.get(speaker_id)
        # Per-segment voice mode (Feature K + Tier-2A): do not mutate job-level defaults.
        seg_voice_mode = (
            str(line.get("preferred_voice_mode") or eff_voice_mode or "clone").strip().lower()
        )
        if seg_voice_mode not in {"clone", "preset", "single"}:
            seg_voice_mode = "clone"
        if eff_no_clone and seg_voice_mode == "clone":
            seg_voice_mode = "preset"
        per_mode = per_speaker_voice_mode.get(speaker_id)
        if per_mode in {"clone", "preset", "single"}:
            seg_voice_mode = per_mode
        if per_mode == "original" or speaker_id in per_speaker_keep_original:
            clip = clips_dir / f"{i:04d}_{speaker_id}.wav"
            _write_silence_wav(clip, duration_s=max(0.0, seg_end - seg_start))
            _ffmpeg_to_pcm16k(clip, clip)
            clip_paths.append(clip)
            _note_segment(
                speaker_id=speaker_id,
                provider="silence",
                ref_path=None,
                clone_attempted=False,
                clone_succeeded=False,
                fallback_reason="keep_original",
            )
            if progress_cb is not None:
                with suppress(Exception):
                    progress_cb(i + 1, total)
            continue

        # 2) Persistent character ref (series-scoped), if mapped.
        if (
            speaker_wav is None
            and seg_voice_mode == "clone"
            and str(series_slug or "").strip()
            and isinstance(speaker_character_map, dict)
        ):
            try:
                from dubbing_pipeline.voice_store.store import get_character_ref

                cslug = str(speaker_character_map.get(str(speaker_id)) or "").strip()
                if cslug:
                    cref = get_character_ref(
                        str(series_slug),
                        cslug,
                        voice_store_dir=eff_voice_store_dir,
                    )
                    if cref is not None and cref.exists():
                        speaker_wav = cref
                        logger.info(
                            "character_ref_used",
                            series=str(series_slug),
                            character=str(cslug),
                            path=str(cref),
                            speaker_id=str(speaker_id),
                        )
            except Exception:
                pass

        # 3) Per-job voice reference directory: prefer extracted refs for this job.
        if speaker_wav is None and eff_voice_ref_dir and seg_voice_mode == "clone":
            with suppress(Exception):
                base = Path(eff_voice_ref_dir).expanduser()
                cand1 = base / f"{speaker_id}.wav"
                cand2 = base / speaker_id / "ref.wav"
                for c in (cand1, cand2):
                    if c.exists() and c.is_file():
                        speaker_wav = c
                        logger.info("speaker_ref_used", speaker_id=str(speaker_id), path=str(c))
                        break

        # Optional: legacy voice-memory delivery preferences only (do NOT use it as a ref store).
        if vm_store is not None:
            try:
                cid = vm_manual.get(speaker_id, speaker_id)
                for c in vm_store.list_characters():
                    if str(c.get("character_id") or "") != str(cid):
                        continue
                    pref_mode = ""
                    if not str(line.get("preferred_voice_mode") or "").strip():
                        pref_mode = str(c.get("voice_mode") or "").strip().lower()
                    pref_preset = str(c.get("preset_voice_id") or "").strip()
                    if pref_mode in {"clone", "preset", "single"}:
                        seg_voice_mode = pref_mode
                    if pref_preset:
                        per_speaker_preset_override[speaker_id] = pref_preset
                    break
            except Exception:
                pass
        # Global default clone speaker wav (lowest priority among ref sources).
        if speaker_wav is None:
            speaker_wav = eff_tts_speaker_wav

        # NOTE: persistent refs are stored via `dubbing_pipeline.voice_store` (series/character scoped).

        synthesized = False
        provider_used = "silence"
        clone_attempted = False
        clone_succeeded = False
        ref_used: Path | None = None
        if seg_voice_mode == "clone" and speaker_wav is not None and Path(speaker_wav).exists():
            # Record which reference WAV was selected even if XTTS is unavailable and we fall back.
            ref_used = Path(speaker_wav)
        fallback_reason: str | None = None

        # Best-effort auto-enroll: if mapping exists but no persistent ref exists yet, save the job ref.
        # Opt-in via `voice_memory` (privacy-safe default: off).
        if (
            eff_voice_memory
            and seg_voice_mode == "clone"
            and ref_used is not None
            and str(series_slug or "").strip()
            and isinstance(speaker_character_map, dict)
        ):
            try:
                from dubbing_pipeline.voice_store.store import get_character_ref, save_character_ref

                cslug = str(speaker_character_map.get(str(speaker_id)) or "").strip()
                if cslug and (str(series_slug), cslug) not in _saved_character_refs:
                    if get_character_ref(str(series_slug), cslug, voice_store_dir=eff_voice_store_dir) is None:
                        save_character_ref(
                            str(series_slug),
                            cslug,
                            ref_used,
                            job_id=str(job_id or ""),
                            metadata={
                                "display_name": "",
                                "created_by": "",
                                "source": "auto_enroll",
                            },
                            voice_store_dir=eff_voice_store_dir,
                        )
                        _saved_character_refs.add((str(series_slug), cslug))
            except Exception:
                pass

        def _retry_wrap(fn_name: str, fn):
            def _on_retry(n, delay, ex):
                logger.warning("tts_retry", method=fn_name, attempt=n, delay_s=delay, error=str(ex))

            return retry_call(
                fn,
                retries=int(settings.retry_max),
                base=float(settings.retry_base_sec),
                cap=float(settings.retry_cap_sec),
                jitter=True,
                on_retry=_on_retry,
            )

        # If breaker is open, skip XTTS and go straight to fallbacks.
        if (
            engine is not None
            and cb.allow()
            and seg_voice_mode == "clone"
            and eff_tts_provider in {"auto", "xtts"}
        ):
            # 1) XTTS clone (if speaker_wav)
            if speaker_wav is not None and speaker_wav.exists():
                clone_attempted = True
                try:
                    _retry_wrap(
                        "xtts_clone",
                        lambda text=text, speaker_wav=speaker_wav, raw_clip=raw_clip: engine.synthesize(
                            text,
                            language=eff_tts_lang,
                            speaker_wav=speaker_wav,
                            out_path=raw_clip,
                        ),
                    )
                    cb.mark_success()
                    synthesized = True
                    provider_used = "xtts_clone"
                    clone_succeeded = True
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning(
                        "tts_xtts_clone_failed", error=str(ex), breaker=cb.snapshot().state
                    )
                    synthesized = False
                    fallback_reason = f"xtts_clone_failed:{ex}"

            # 2) XTTS preset
            if (
                not synthesized
                and seg_voice_mode in {"clone", "preset"}
                and eff_tts_provider in {"auto", "xtts"}
            ):
                # Choose best preset if we have speaker embedding; else default preset
                preset = (
                    per_speaker_preset_override.get(speaker_id)
                    or voice_map.get(speaker_id)
                    or (eff_tts_speaker or "default")
                )
                emb_path = speaker_embeddings.get(speaker_id)
                if emb_path and emb_path.exists():
                    best = choose_similar_voice(
                        emb_path,
                        preset_dir=settings.voice_preset_dir,
                        db_path=settings.voice_db_path,
                        embeddings_dir=voice_db_embeddings_dir,
                    )
                    if best:
                        preset = best
                try:
                    _retry_wrap(
                        "xtts_preset",
                        lambda text=text, preset=preset, raw_clip=raw_clip: engine.synthesize(
                            text,
                            language=eff_tts_lang,
                            speaker_id=preset,
                            out_path=raw_clip,
                        ),
                    )
                    cb.mark_success()
                    synthesized = True
                    provider_used = "xtts_preset"
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning(
                        "tts_xtts_preset_failed", error=str(ex), breaker=cb.snapshot().state
                    )
                    synthesized = False
                    if fallback_reason is None:
                        fallback_reason = f"xtts_preset_failed:{ex}"
        else:
            logger.info(
                "tts_breaker_open_or_engine_missing",
                breaker=cb.snapshot().state,
                engine=bool(engine),
            )
            if seg_voice_mode == "clone" and eff_tts_provider in {"auto", "xtts"}:
                fallback_reason = fallback_reason or "xtts_unavailable"

        # 3) Basic English single-speaker (Coqui) (still requires COQUI_TOS_AGREED)
        # Use a common Coqui model as fallback.
        if not synthesized and eff_tts_provider in {"auto", "xtts", "basic"}:
            try:
                from dubbing_pipeline.gates.license import require_coqui_tos
                from dubbing_pipeline.runtime.device_allocator import pick_device
                from dubbing_pipeline.runtime.model_manager import ModelManager

                require_coqui_tos()
                basic_model = str(settings.tts_basic_model)
                dev = pick_device("auto")
                tts_basic = ModelManager.instance().get_tts(basic_model, dev)

                def _basic(tts_basic=tts_basic, text=text, raw_clip=raw_clip):
                    try:
                        return tts_basic.tts_to_file(text=text, file_path=str(raw_clip))
                    except TypeError:
                        return tts_basic.tts_to_file(text=text, path=str(raw_clip))

                _retry_wrap("basic_tts", _basic)
                synthesized = True
                provider_used = "basic_tts"
            except Exception as ex:
                logger.warning("tts_basic_failed", error=str(ex))
                if fallback_reason is None:
                    fallback_reason = f"basic_tts_failed:{ex}"

        # 4) espeak-ng last resort
        if not synthesized and eff_tts_provider in {"auto", "xtts", "basic", "espeak"}:
            try:
                _retry_wrap(
                    "espeak", lambda text=text, raw_clip=raw_clip: _espeak_fallback(text, raw_clip)
                )
                synthesized = True
                provider_used = "espeak"
            except Exception as ex:
                logger.warning("tts_espeak_failed", error=str(ex))
                synthesized = False
                if fallback_reason is None:
                    fallback_reason = f"espeak_failed:{ex}"

        if not synthesized:
            _write_silence_wav(
                raw_clip, duration_s=max(0.0, float(line["end"]) - float(line["start"]))
            )
            provider_used = "silence"

        # Normalize to 16kHz mono PCM for alignment
        try:
            _ffmpeg_to_pcm16k(raw_clip, clip)
        except Exception:
            clip = raw_clip

        # Optional expressive controls (best-effort; never required)
        with suppress(Exception):
            from dubbing_pipeline.expressive.policy import apply_prosody_ffmpeg

            clip = apply_prosody_ffmpeg(
                clip,
                ffmpeg_bin=Path(settings.ffmpeg_bin),
                rate=rate_mul,
                pitch=pitch_mul,
                energy=energy_mul,
            )

        # Feature K: add a pause tail (best-effort) after prosody controls.
        if pause_tail_ms > 0:
            with suppress(Exception):
                from dubbing_pipeline.utils.ffmpeg_safe import ffprobe_duration_seconds

                d0 = float(ffprobe_duration_seconds(Path(clip)))
                tail_s = float(pause_tail_ms) / 1000.0
                outp = Path(clip).with_suffix(".pause.wav")
                run_ffmpeg(
                    [
                        str(settings.ffmpeg_bin),
                        "-y",
                        "-i",
                        str(clip),
                        "-filter:a",
                        f"apad=pad_dur={tail_s:.3f}",
                        "-t",
                        f"{(d0 + tail_s):.3f}",
                        str(outp),
                    ],
                    timeout_s=120,
                    retries=0,
                    capture=True,
                )
                clip = outp

        # Optional Tier-1 C pacing controls (opt-in).
        if eff_pacing:
            try:
                from dubbing_pipeline.timing.fit_text import shorten_english
                from dubbing_pipeline.timing.pacing import (
                    measure_wav_seconds,
                    pad_or_trim_wav,
                    time_stretch_wav,
                )

                target_dur = max(0.05, float(line["end"]) - float(line["start"]))
                actual_dur = measure_wav_seconds(clip)
                actions: list[dict] = []
                atempo_ratio_used: float | None = None
                tts_speed_used: float | None = None
                did_hard_trim = False
                did_pad = False

                # Too long: 1) try TTS speed control (best-effort, only for XTTS engine)
                if actual_dur > target_dur * (1.0 + eff_tol):
                    ratio = max(1.0, float(actual_dur) / float(target_dur))
                    speed = min(max(1.0, ratio), float(eff_pacing_max))
                    if engine is not None and cb.allow() and eff_tts_provider in {"auto", "xtts"}:
                        try:
                            raw2 = raw_clip.with_suffix(".speed.raw.wav")
                            actions.append({"kind": "tts_speed", "speed": float(speed)})
                            tts_speed_used = float(speed)
                            # Prefer preset path for speed-control re-synth (clone w/ speed is model-dependent)
                            _retry_wrap(
                                "xtts_speed",
                                lambda text=text, raw2=raw2, speed=speed: engine.synthesize(
                                    text,
                                    language=eff_tts_lang,
                                    speaker_id=eff_tts_speaker,
                                    out_path=raw2,
                                    speed=float(speed),
                                ),
                            )
                            # Normalize then continue with updated clip
                            try:
                                _ffmpeg_to_pcm16k(raw2, clip)
                            except Exception:
                                clip = raw2
                            actual_dur = measure_wav_seconds(clip)
                        except Exception as ex:
                            actions.append({"kind": "tts_speed_failed", "error": str(ex)})

                # 2) atempo time-stretch within safe bounds
                if actual_dur > target_dur * (1.0 + eff_tol):
                    ratio = float(actual_dur) / float(target_dur)
                    ratio = max(float(eff_pacing_min), min(float(eff_pacing_max), float(ratio)))
                    actions.append({"kind": "atempo", "ratio": float(ratio)})
                    atempo_ratio_used = float(ratio)
                    stretched = clip.with_suffix(".pacing.stretch.wav")
                    stretched = time_stretch_wav(
                        clip,
                        stretched,
                        ratio,
                        min_ratio=float(eff_pacing_min),
                        max_ratio=float(eff_pacing_max),
                        timeout_s=120,
                    )
                    clip = stretched
                    actual_dur = measure_wav_seconds(clip)

                # 3) shorten and re-synthesize once (rule-based) if still too long
                if actual_dur > target_dur * (1.0 + eff_tol):
                    shorter = shorten_english(text)
                    if shorter and shorter != text and engine is not None and cb.allow():
                        try:
                            actions.append(
                                {"kind": "shorten_resynth", "before": text, "after": shorter}
                            )
                            raw3 = raw_clip.with_suffix(".short.raw.wav")
                            _retry_wrap(
                                "xtts_short",
                                lambda shorter=shorter, raw3=raw3: engine.synthesize(
                                    shorter,
                                    language=eff_tts_lang,
                                    speaker_id=eff_tts_speaker,
                                    out_path=raw3,
                                ),
                            )
                            try:
                                _ffmpeg_to_pcm16k(raw3, clip)
                            except Exception:
                                clip = raw3
                            actual_dur = measure_wav_seconds(clip)
                        except Exception as ex:
                            actions.append({"kind": "shorten_resynth_failed", "error": str(ex)})

                # 4) hard cap if still too long
                if actual_dur > target_dur * (1.0 + eff_tol):
                    actions.append({"kind": "hard_trim"})
                    did_hard_trim = True
                    capped = clip.with_suffix(".pacing.cap.wav")
                    clip = pad_or_trim_wav(clip, capped, target_dur, timeout_s=120)
                    actual_dur = measure_wav_seconds(clip)
                    logger.warning(
                        "[dp] pacing: hard-capped segment",
                        idx=i,
                        target_s=target_dur,
                        actual_s=actual_dur,
                    )

                # 5) pad if too short
                if actual_dur < target_dur * (1.0 - eff_tol):
                    actions.append({"kind": "pad"})
                    did_pad = True
                    padded = clip.with_suffix(".pacing.pad.wav")
                    clip = pad_or_trim_wav(clip, padded, target_dur, timeout_s=120)
                    actual_dur = measure_wav_seconds(clip)

                # Persist pacing summary into the line for QA/reporting (small + deterministic).
                with suppress(Exception):
                    line["pacing"] = {
                        "enabled": True,
                        "target_s": float(target_dur),
                        "actual_s": float(actual_dur),
                        "ratio": (
                            (float(actual_dur) / float(target_dur)) if target_dur > 0 else None
                        ),
                        "min_ratio": float(eff_pacing_min),
                        "max_ratio": float(eff_pacing_max),
                        "tolerance": float(eff_tol),
                        "actions": [
                            str(a.get("kind") or "") for a in actions if isinstance(a, dict)
                        ],
                        "atempo_ratio": (
                            float(atempo_ratio_used) if atempo_ratio_used is not None else None
                        ),
                        "tts_speed": float(tts_speed_used) if tts_speed_used is not None else None,
                        "hard_trim": bool(did_hard_trim),
                        "padded": bool(did_pad),
                    }
                if eff_debug:
                    try:
                        seg_dir = out_dir / "segments"
                        seg_dir.mkdir(parents=True, exist_ok=True)
                        from dubbing_pipeline.utils.io import write_json as _wj

                        _wj(
                            seg_dir / f"{i:04d}.json",
                            {
                                "idx": i,
                                "start": float(line.get("start", 0.0)),
                                "end": float(line.get("end", 0.0)),
                                "original_text": str(line.get("src_text") or ""),
                                "translated_text_pre_fit": str(line.get("text_pre_fit") or ""),
                                "translated_text": str(line.get("text") or ""),
                                "target_seconds": float(target_dur),
                                "actual_seconds": float(actual_dur),
                                "actions": actions,
                            },
                        )
                    except Exception:
                        pass
            except Exception:
                # If pacing layer fails, fall back to legacy retime below.
                pass
        else:
            # Legacy retime (existing behavior): librosa when available, else pad/trim.
            with suppress(Exception):
                from dubbing_pipeline.stages.align import retime_tts  # lazy import

                target_dur = max(0.05, float(line["end"]) - float(line["start"]))
                clip = retime_tts(
                    clip, target_duration_s=target_dur, max_stretch=float(max_stretch)
                )
        clip_paths.append(clip)
        _note_segment(
            speaker_id=speaker_id,
            provider=provider_used,
            ref_path=ref_used,
            clone_attempted=clone_attempted,
            clone_succeeded=clone_succeeded,
            fallback_reason=fallback_reason,
        )
        if progress_cb is not None:
            with suppress(Exception):
                progress_cb(i + 1, total)

    # Output paths
    if wav_out is None:
        # default: <stem>.tts.wav (stem is output folder name)
        wav_out = out_dir / f"{out_dir.name}.tts.wav"

    # Cross-job cache (coarse): if audio_hash provided and tts config stable, reuse tts wav + manifest.
    if audio_hash:
        sig = settings.speaker_signature  # optional precomputed override
        if not sig:
            from dubbing_pipeline.utils.hashio import speaker_signature

            sig = speaker_signature(eff_tts_lang, eff_tts_speaker, eff_tts_speaker_wav)
        key = make_key(
            "tts",
            {
                "audio": audio_hash,
                "tts_model": settings.tts_model,
                "lang": eff_tts_lang,
                "sig": sig,
            },
        )
        hit = cache_get(key)
        if hit:
            paths = hit.get("paths", {})
            try:
                src_wav = Path(str(paths.get("tts_wav")))
                src_manifest = Path(str(paths.get("manifest")))
                if src_wav.exists() and src_manifest.exists():
                    out_dir.mkdir(parents=True, exist_ok=True)
                    wav_out.write_bytes(src_wav.read_bytes())
                    (out_dir / "tts_manifest.json").write_bytes(src_manifest.read_bytes())
                    logger.info("[dp] tts cache hit", key=key)
                    return wav_out
            except Exception:
                pass

    if job_id:
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        manifest = out_dir / "tts_manifest.json"
        if wav_out.exists() and manifest.exists() and stage_is_done(ckpt, "tts"):
            logger.info("[dp] tts stage checkpoint hit")
            return wav_out

    render_aligned_track(lines, clip_paths, wav_out)
    if eff_director:
        with suppress(Exception):
            from dubbing_pipeline.expressive.director import write_director_plans_jsonl

            outp = out_dir / "expressive" / "director_plans.jsonl"
            write_director_plans_jsonl(_director_plans, outp)
    if music_regions:
        logger.info(
            "music_suppress_summary",
            suppressed_segments=int(music_suppressed),
            total_segments=int(total),
            music_regions=int(len(music_regions)),
        )

    # Optional metadata
    with suppress(Exception):
        write_json(
            out_dir / "tts_manifest.json",
            {
                "clips": [str(p) for p in clip_paths],
                "wav_out": str(wav_out),
                "lines": lines,
                "speaker_report": speaker_report,
                "two_pass_clone": bool(two_pass_enabled),
                "two_pass_phase": (str(two_pass_phase) if two_pass_phase is not None else None),
                "series_slug": (str(series_slug) if series_slug is not None else None),
                "voice_mode": str(eff_voice_mode),
                "no_clone": bool(eff_no_clone),
                "voice_ref_dir": (str(eff_voice_ref_dir) if eff_voice_ref_dir else None),
                "voice_store_dir": str(eff_voice_store_dir),
                "tts_provider": str(eff_tts_provider),
            },
        )

    if job_id:
        with suppress(Exception):
            write_ckpt(
                job_id,
                "tts",
                {"tts_wav": wav_out, "manifest": out_dir / "tts_manifest.json"},
                {"work_dir": str(out_dir)},
                ckpt_path=ckpt_path,
            )

    if audio_hash:
        with suppress(Exception):
            sig = settings.speaker_signature or ""
            if not sig:
                from dubbing_pipeline.utils.hashio import speaker_signature

                sig = speaker_signature(eff_tts_lang, eff_tts_speaker, eff_tts_speaker_wav)
            key = make_key(
                "tts",
                {
                    "audio": audio_hash,
                    "tts_model": settings.tts_model,
                    "lang": eff_tts_lang,
                    "sig": sig,
                },
            )
            cache_put(
                key,
                {"tts_wav": wav_out, "manifest": out_dir / "tts_manifest.json"},
                meta={"created_at": time.time()},
            )

    logger.info("[dp] TTS done → %s", wav_out)
    return wav_out
