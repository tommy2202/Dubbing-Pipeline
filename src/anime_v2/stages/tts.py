from __future__ import annotations

import subprocess
import time
import wave
from contextlib import suppress
from pathlib import Path

from anime_v2.cache.store import cache_get, cache_put, make_key
from anime_v2.config import get_settings
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.tts_engine import CoquiXTTS, choose_similar_voice
from anime_v2.timing.pacing import atempo_chain
from anime_v2.utils.circuit import Circuit
from anime_v2.utils.ffmpeg_safe import run_ffmpeg
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.retry import retry_call


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


def _apply_prosody_ffmpeg(
    wav_in: Path, *, rate: float = 1.0, pitch: float = 1.0, energy: float = 1.0
) -> Path:
    """
    Optional lightweight prosody controls via ffmpeg filters.

    - rate: tempo multiplier (1.0 = unchanged)
    - pitch: pitch multiplier (1.0 = unchanged), implemented via asetrate trick
    - energy: volume multiplier (1.0 = unchanged)
    """
    wav_in = Path(wav_in)
    s = get_settings()

    r = max(0.5, min(2.0, float(rate)))
    p = max(0.8, min(1.25, float(pitch)))
    e = max(0.2, min(3.0, float(energy)))

    if abs(r - 1.0) < 0.01 and abs(p - 1.0) < 0.01 and abs(e - 1.0) < 0.01:
        return wav_in

    out = wav_in.with_suffix(".prosody.wav")

    filters: list[str] = []
    # Pitch shift without changing duration: asetrate then compensate with atempo.
    if abs(p - 1.0) >= 0.01:
        filters.append(f"asetrate=16000*{p:.4f}")
        filters.append(atempo_chain(1.0 / p))
    if abs(r - 1.0) >= 0.01:
        filters.append(atempo_chain(r))
    if abs(e - 1.0) >= 0.01:
        filters.append(f"volume={e:.3f}")
    filt = ",".join(filters)

    try:
        run_ffmpeg(
            [
                str(s.ffmpeg_bin),
                "-y",
                "-i",
                str(wav_in),
                "-af",
                filt,
                "-ac",
                "1",
                "-ar",
                "16000",
                str(out),
            ],
            timeout_s=120,
            retries=0,
            capture=True,
        )
        return out
    except Exception:
        return wav_in


def _emotion_controls(text: str, *, mode: str) -> tuple[str, float, float, float]:
    """
    Returns (clean_text, rate_mul, pitch_mul, energy_mul).
    """
    t = str(text or "")
    m = (mode or "off").strip().lower()
    if m not in {"off", "auto", "tags"}:
        m = "off"

    if m == "tags":
        # Accept a simple prefix tag: "[happy] hello"
        import re

        mm = re.match(r"^\s*\[([a-zA-Z0-9_\-]+)\]\s*(.*)$", t)
        if mm:
            tag = mm.group(1).lower()
            t = mm.group(2)
            presets = {
                "happy": (1.02, 1.06, 1.10),
                "sad": (0.95, 0.94, 0.85),
                "angry": (1.05, 1.02, 1.25),
                "calm": (0.98, 0.98, 0.90),
            }
            if tag in presets:
                r0, p0, e0 = presets[tag]
                return t, r0, p0, e0
        return t, 1.0, 1.0, 1.0

    if m == "auto":
        # Tiny offline heuristic based on punctuation.
        r0 = 1.0
        p0 = 1.0
        e0 = 1.0
        if "!" in t:
            p0 *= 1.05
            e0 *= 1.10
        if "?" in t:
            p0 *= 1.05
        if "..." in t or "…" in t:
            r0 *= 0.96
            e0 *= 0.92
        return t, r0, p0, e0

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
    voice_ref_dir: Path | None = None,
    voice_store_dir: Path | None = None,
    voice_memory: bool | None = None,
    voice_memory_dir: Path | None = None,
    voice_match_threshold: float | None = None,
    voice_auto_enroll: bool | None = None,
    voice_character_map: Path | None = None,
    tts_provider: str | None = None,
    emotion_mode: str | None = None,
    speech_rate: float | None = None,
    pitch: float | None = None,
    energy: float | None = None,
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
      - tries cloning via speaker wav (from diarization segments or env TTS_SPEAKER_WAV)
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
    if eff_voice_mode not in {"clone", "preset", "single"}:
        eff_voice_mode = "clone"
    eff_voice_ref_dir = voice_ref_dir or settings.voice_ref_dir
    eff_voice_store_dir = Path(voice_store_dir or settings.voice_store_dir).resolve()
    eff_voice_memory = (
        bool(voice_memory) if voice_memory is not None else bool(settings.voice_memory)
    )
    eff_voice_memory_dir = Path(
        voice_memory_dir
        or getattr(settings, "voice_memory_dir", (Path.cwd() / "data" / "voice_memory"))
    ).resolve()
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
    try:
        vmj = str(eff_voice_map_json_path) if eff_voice_map_json_path else ""
        if vmj:
            data = read_json(Path(vmj), default={})
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                for it in data.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    cid = str(it.get("character_id") or "").strip()
                    strat = str(it.get("speaker_strategy") or "").strip().lower()
                    if not cid:
                        continue
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

    # Speaker -> representative wav from diarization segments (longest segment)
    speaker_rep_wav: dict[str, Path] = {}
    speaker_embeddings: dict[str, Path] = {}
    if diarization_json and diarization_json.exists():
        diar = read_json(diarization_json, default={})
        if isinstance(diar, dict):
            segs = diar.get("segments", [])
            emb_map = diar.get("speaker_embeddings", {})
            if isinstance(emb_map, dict):
                for sid, p in emb_map.items():
                    with suppress(Exception):
                        speaker_embeddings[str(sid)] = Path(str(p))
            if isinstance(segs, list):
                best: dict[str, tuple[float, Path]] = {}
                for s in segs:
                    try:
                        sid = str(s.get("speaker_id") or "Speaker1")
                        dur = float(s["end"]) - float(s["start"])
                        p = Path(str(s["wav_path"]))
                        if sid not in best or dur > best[sid][0]:
                            best[sid] = (dur, p)
                    except Exception:
                        continue
                speaker_rep_wav = {sid: p for sid, (_, p) in best.items()}

    # CharacterStore speaker_wavs (cross-episode persistence)
    store = CharacterStore.default()
    with suppress(Exception):
        store.load()

    # Tier-2A voice memory store (optional)
    vm_store = None
    vm_manual: dict[str, str] = {}
    if eff_voice_memory:
        try:
            from anime_v2.voice_memory.store import VoiceMemoryStore

            vm_store = VoiceMemoryStore(eff_voice_memory_dir)
            if eff_voice_character_map and eff_voice_character_map.exists():
                data = read_json(eff_voice_character_map, default={})
                if isinstance(data, dict):
                    vm_manual = {
                        str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()
                    }
        except Exception as ex:
            logger.warning("[v2] voice-memory unavailable; continuing without it (%s)", ex)
            vm_store = None

    # Engine (lazy-loaded per run)
    engine: CoquiXTTS | None = None
    cb = Circuit.get("tts")
    try:
        engine = CoquiXTTS()
    except Exception as ex:
        logger.warning("[v2] XTTS unavailable: %s", ex)

    clips_dir = out_dir / "tts_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: list[Path] = []

    voice_db_embeddings_dir = (settings.voice_db_path.parent / "embeddings").resolve()

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

        text = str(line.get("text", "") or "").strip()
        text, emo_rate, emo_pitch, emo_energy = _emotion_controls(text, mode=eff_emotion_mode)
        rate_mul = float(eff_rate) * float(emo_rate)
        pitch_mul = float(eff_pitch) * float(emo_pitch)
        energy_mul = float(eff_energy) * float(emo_energy)
        speaker_id = str(line.get("speaker_id") or eff_tts_speaker or "default")
        if eff_voice_mode == "single":
            speaker_id = str(eff_tts_speaker or "default")
        if not text:
            # keep timing, but skip synthesis (silence clip)
            clip = clips_dir / f"{i:04d}_{speaker_id}.wav"
            _write_silence_wav(clip, duration_s=max(0.0, float(line["end"]) - float(line["start"])))
            _ffmpeg_to_pcm16k(clip, clip)
            clip_paths.append(clip)
            if progress_cb is not None:
                with suppress(Exception):
                    progress_cb(i + 1, total)
            continue

        raw_clip = clips_dir / f"{i:04d}_{speaker_id}.raw.wav"
        clip = clips_dir / f"{i:04d}_{speaker_id}.wav"

        # Choose clone speaker wav:
        speaker_wav = (
            per_speaker_wav_override.get(speaker_id)
            or eff_tts_speaker_wav
            or speaker_rep_wav.get(speaker_id)
        )
        # Tier-2A: prefer voice-memory refs for stable cross-episode identity.
        if vm_store is not None:
            try:
                # allow optional manual speaker_id -> character_id mapping
                cid = vm_manual.get(speaker_id, speaker_id)
                ref = vm_store.best_ref(cid)
                if ref is not None and ref.exists():
                    speaker_wav = ref
                # Allow per-character preferences to override effective voice_mode/preset.
                for c in vm_store.list_characters():
                    if str(c.get("character_id") or "") != str(cid):
                        continue
                    pref_mode = str(c.get("voice_mode") or "").strip().lower()
                    pref_preset = str(c.get("preset_voice_id") or "").strip()
                    if pref_mode in {"clone", "preset", "single"}:
                        eff_voice_mode = pref_mode
                    if pref_preset:
                        per_speaker_preset_override[speaker_id] = pref_preset
                    break
            except Exception:
                pass
        # Optional voice reference directory: prefer stable refs (e.g. <ref_dir>/<speaker_id>.wav)
        if speaker_wav is None and eff_voice_ref_dir:
            with suppress(Exception):
                base = Path(eff_voice_ref_dir).expanduser()
                cand1 = base / f"{speaker_id}.wav"
                cand2 = base / speaker_id / "ref.wav"
                for c in (cand1, cand2):
                    if c.exists() and c.is_file():
                        speaker_wav = c
                        break
        if speaker_wav is None:
            with suppress(Exception):
                c = store.characters.get(speaker_id)
                if c and c.speaker_wavs:
                    for p in c.speaker_wavs:
                        pp = Path(str(p))
                        if pp.exists():
                            speaker_wav = pp
                            break

        # Persist reference wavs to a stable store (best-effort, no-op on failure).
        if speaker_wav is not None and speaker_wav.exists():
            with suppress(Exception):
                dest_dir = (eff_voice_store_dir / speaker_id).resolve()
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / "ref.wav"
                if not dest.exists():
                    dest.write_bytes(Path(speaker_wav).read_bytes())

        synthesized = False

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
            and eff_voice_mode == "clone"
            and eff_tts_provider in {"auto", "xtts"}
        ):
            # 1) XTTS clone (if speaker_wav)
            if speaker_wav is not None and speaker_wav.exists():
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
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning(
                        "tts_xtts_clone_failed", error=str(ex), breaker=cb.snapshot().state
                    )
                    synthesized = False

            # 2) XTTS preset
            if (
                not synthesized
                and eff_voice_mode in {"clone", "preset"}
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
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning(
                        "tts_xtts_preset_failed", error=str(ex), breaker=cb.snapshot().state
                    )
                    synthesized = False
        else:
            logger.info(
                "tts_breaker_open_or_engine_missing",
                breaker=cb.snapshot().state,
                engine=bool(engine),
            )

        # 3) Basic English single-speaker (Coqui) (still requires COQUI_TOS_AGREED)
        # Use a common Coqui model as fallback.
        if not synthesized and eff_tts_provider in {"auto", "xtts", "basic"}:
            try:
                from anime_v2.gates.license import require_coqui_tos
                from anime_v2.runtime.device_allocator import pick_device
                from anime_v2.runtime.model_manager import ModelManager

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
            except Exception as ex:
                logger.warning("tts_basic_failed", error=str(ex))

        # 4) espeak-ng last resort
        if not synthesized and eff_tts_provider in {"auto", "xtts", "basic", "espeak"}:
            try:
                _retry_wrap(
                    "espeak", lambda text=text, raw_clip=raw_clip: _espeak_fallback(text, raw_clip)
                )
                synthesized = True
            except Exception as ex:
                logger.warning("tts_espeak_failed", error=str(ex))
                synthesized = False

        if not synthesized:
            _write_silence_wav(
                raw_clip, duration_s=max(0.0, float(line["end"]) - float(line["start"]))
            )

        # Normalize to 16kHz mono PCM for alignment
        try:
            _ffmpeg_to_pcm16k(raw_clip, clip)
        except Exception:
            clip = raw_clip

        # Optional expressive controls (best-effort; never required)
        with suppress(Exception):
            clip = _apply_prosody_ffmpeg(clip, rate=rate_mul, pitch=pitch_mul, energy=energy_mul)

        # Optional Tier-1 C pacing controls (opt-in).
        if eff_pacing:
            try:
                from anime_v2.timing.fit_text import shorten_english
                from anime_v2.timing.pacing import (
                    measure_wav_seconds,
                    pad_or_trim_wav,
                    time_stretch_wav,
                )

                target_dur = max(0.05, float(line["end"]) - float(line["start"]))
                actual_dur = measure_wav_seconds(clip)
                actions: list[dict] = []

                # Too long: 1) try TTS speed control (best-effort, only for XTTS engine)
                if actual_dur > target_dur * (1.0 + eff_tol):
                    ratio = max(1.0, float(actual_dur) / float(target_dur))
                    speed = min(max(1.0, ratio), float(eff_pacing_max))
                    if engine is not None and cb.allow() and eff_tts_provider in {"auto", "xtts"}:
                        try:
                            raw2 = raw_clip.with_suffix(".speed.raw.wav")
                            actions.append({"kind": "tts_speed", "speed": float(speed)})
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
                    capped = clip.with_suffix(".pacing.cap.wav")
                    clip = pad_or_trim_wav(clip, capped, target_dur, timeout_s=120)
                    actual_dur = measure_wav_seconds(clip)
                    logger.warning(
                        "[v2] pacing: hard-capped segment",
                        idx=i,
                        target_s=target_dur,
                        actual_s=actual_dur,
                    )

                # 5) pad if too short
                if actual_dur < target_dur * (1.0 - eff_tol):
                    actions.append({"kind": "pad"})
                    padded = clip.with_suffix(".pacing.pad.wav")
                    clip = pad_or_trim_wav(clip, padded, target_dur, timeout_s=120)
                    actual_dur = measure_wav_seconds(clip)
                if eff_debug:
                    try:
                        seg_dir = out_dir / "segments"
                        seg_dir.mkdir(parents=True, exist_ok=True)
                        from anime_v2.utils.io import write_json as _wj

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
                from anime_v2.stages.align import retime_tts  # lazy import

                target_dur = max(0.05, float(line["end"]) - float(line["start"]))
                clip = retime_tts(
                    clip, target_duration_s=target_dur, max_stretch=float(max_stretch)
                )
        clip_paths.append(clip)
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
            from anime_v2.utils.hashio import speaker_signature

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
                    logger.info("[v2] tts cache hit", key=key)
                    return wav_out
            except Exception:
                ...

    if job_id:
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        manifest = out_dir / "tts_manifest.json"
        if wav_out.exists() and manifest.exists() and stage_is_done(ckpt, "tts"):
            logger.info("[v2] tts stage checkpoint hit")
            return wav_out

    render_aligned_track(lines, clip_paths, wav_out)

    # Optional metadata
    with suppress(Exception):
        write_json(
            out_dir / "tts_manifest.json",
            {"clips": [str(p) for p in clip_paths], "wav_out": str(wav_out), "lines": lines},
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
                from anime_v2.utils.hashio import speaker_signature

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

    logger.info("[v2] TTS done → %s", wav_out)
    return wav_out
