from __future__ import annotations

import os
import subprocess
import time
import wave
from pathlib import Path

from anime_v2.utils.config import get_settings
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.stages.character_store import CharacterStore
from anime_v2.stages.tts_engine import CoquiXTTS, choose_similar_voice
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.utils.circuit import Circuit
from anime_v2.utils.retry import retry_call
from anime_v2.cache.store import cache_get, cache_put, make_key


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
        start_s, end_s = [p.strip() for p in lines[1].split("-->", 1)]
        start = parse_ts(start_s)
        end = parse_ts(end_s)
        cue_text = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        cues.append({"start": start, "end": end, "speaker_id": "Speaker1", "text": cue_text})
    return cues


def _ffmpeg_to_pcm16k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg cannot safely overwrite input in-place
    if src.resolve() == dst.resolve():
        tmp = dst.with_suffix(".tmp.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(tmp)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp.replace(dst)
        return
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
        subprocess.run(["espeak-ng", "-w", str(out_path), text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        raise RuntimeError(f"espeak-ng failed: {ex}") from ex


def render_aligned_track(lines: list[dict], clip_paths: list[Path], out_wav: Path, *, sr: int = 16000) -> None:
    """
    Render a single aligned WAV track by padding silence between segments.
    Best-effort: if clips overlap, later clips overwrite earlier samples.
    """
    if not lines or not clip_paths:
        _write_silence_wav(out_wav, duration_s=0.0, sr=sr)
        return

    end_t = max(float(l["end"]) for l in lines)
    total_frames = max(1, int(end_t * sr))
    buf = bytearray(b"\x00\x00" * total_frames)

    for l, clip in zip(lines, clip_paths):
        start = float(l["start"])
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
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / ".checkpoint.json"

    # Optional per-speaker preset mapping (voice bank map):
    #   {"SPEAKER_01": "alice", "SPEAKER_02": "bob"}
    voice_map: dict[str, str] = {}
    try:
        voice_map_path = os.environ.get("VOICE_BANK_MAP") or os.environ.get("VOICE_MAP")
        if voice_map_path:
            vm = read_json(Path(voice_map_path), default={})
            if isinstance(vm, dict):
                voice_map = {str(k): str(v) for k, v in vm.items() if str(k).strip() and str(v).strip()}
    except Exception:
        voice_map = {}

    # Optional rich voice map (per-job mapping) for per-speaker overrides.
    # Format: {"items":[{"character_id": "...", "speaker_strategy": "preset|zero-shot", "tts_speaker": "...", "tts_speaker_wav": "..."}]}
    per_speaker_wav_override: dict[str, Path] = {}
    per_speaker_preset_override: dict[str, str] = {}
    try:
        vmj = os.environ.get("VOICE_MAP_JSON") or ""
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
                                "speaker_id": str(s.get("speaker") or s.get("speaker_id") or "SPEAKER_01"),
                                "text": str(s.get("text") or ""),
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
                    try:
                        speaker_embeddings[str(sid)] = Path(str(p))
                    except Exception:
                        pass
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
    try:
        store.load()
    except Exception:
        pass

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
        try:
            progress_cb(0, total)
        except Exception:
            pass

    for i, l in enumerate(lines):
        if cancel_cb is not None:
            try:
                if bool(cancel_cb()):
                    raise TTSCanceled()
            except TTSCanceled:
                raise
            except Exception:
                # ignore cancel callback errors
                pass

        text = str(l.get("text", "") or "").strip()
        speaker_id = str(l.get("speaker_id") or settings.tts_speaker or "default")
        if not text:
            # keep timing, but skip synthesis (silence clip)
            clip = clips_dir / f"{i:04d}_{speaker_id}.wav"
            _write_silence_wav(clip, duration_s=max(0.0, float(l["end"]) - float(l["start"])))
            _ffmpeg_to_pcm16k(clip, clip)
            clip_paths.append(clip)
            if progress_cb is not None:
                try:
                    progress_cb(i + 1, total)
                except Exception:
                    pass
            continue

        raw_clip = clips_dir / f"{i:04d}_{speaker_id}.raw.wav"
        clip = clips_dir / f"{i:04d}_{speaker_id}.wav"

        # Choose clone speaker wav:
        speaker_wav = per_speaker_wav_override.get(speaker_id) or settings.tts_speaker_wav or speaker_rep_wav.get(speaker_id)
        if speaker_wav is None:
            try:
                c = store.characters.get(speaker_id)
                if c and c.speaker_wavs:
                    for p in c.speaker_wavs:
                        pp = Path(str(p))
                        if pp.exists():
                            speaker_wav = pp
                            break
            except Exception:
                pass

        synthesized = False
        fallback_used = None

        def _retry_wrap(fn_name: str, fn):
            def _on_retry(n, delay, ex):
                logger.warning("tts_retry", method=fn_name, attempt=n, delay_s=delay, error=str(ex))

            return retry_call(fn, retries=int(os.environ.get("RETRY_MAX", "3")), base=float(os.environ.get("RETRY_BASE_SEC", "0.5")), cap=float(os.environ.get("RETRY_CAP_SEC", "8.0")), jitter=True, on_retry=_on_retry)

        # If breaker is open, skip XTTS and go straight to fallbacks.
        if engine is not None and cb.allow():
            # 1) XTTS clone (if speaker_wav)
            if speaker_wav is not None and speaker_wav.exists():
                try:
                    _retry_wrap(
                        "xtts_clone",
                        lambda: engine.synthesize(text, language=settings.tts_lang, speaker_wav=speaker_wav, out_path=raw_clip),
                    )
                    cb.mark_success()
                    synthesized = True
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning("tts_xtts_clone_failed", error=str(ex), breaker=cb.snapshot().state)
                    synthesized = False

            # 2) XTTS preset
            if not synthesized:
                # Choose best preset if we have speaker embedding; else default preset
                preset = per_speaker_preset_override.get(speaker_id) or voice_map.get(speaker_id) or (settings.tts_speaker or "default")
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
                    _retry_wrap("xtts_preset", lambda: engine.synthesize(text, language=settings.tts_lang, speaker_id=preset, out_path=raw_clip))
                    cb.mark_success()
                    synthesized = True
                except Exception as ex:
                    cb.mark_failure()
                    logger.warning("tts_xtts_preset_failed", error=str(ex), breaker=cb.snapshot().state)
                    synthesized = False
        else:
            logger.info("tts_breaker_open_or_engine_missing", breaker=cb.snapshot().state, engine=bool(engine))

        # 3) Basic English single-speaker (Coqui) (still requires COQUI_TOS_AGREED)
        # Use a common Coqui model as fallback.
        if not synthesized:
            try:
                from anime_v2.runtime.model_manager import ModelManager
                from anime_v2.runtime.device_allocator import pick_device
                from anime_v2.gates.license import require_coqui_tos

                require_coqui_tos()
                basic_model = os.environ.get("TTS_BASIC_MODEL") or "tts_models/en/ljspeech/tacotron2-DDC"
                dev = pick_device("auto")
                tts_basic = ModelManager.instance().get_tts(basic_model, dev)

                def _basic():
                    try:
                        return tts_basic.tts_to_file(text=text, file_path=str(raw_clip))
                    except TypeError:
                        return tts_basic.tts_to_file(text=text, path=str(raw_clip))

                _retry_wrap("basic_tts", _basic)
                synthesized = True
                fallback_used = "basic_tts"
            except Exception as ex:
                logger.warning("tts_basic_failed", error=str(ex))

        # 4) espeak-ng last resort
        if not synthesized:
            try:
                _retry_wrap("espeak", lambda: _espeak_fallback(text, raw_clip))
                synthesized = True
                fallback_used = "espeak"
            except Exception as ex:
                logger.warning("tts_espeak_failed", error=str(ex))
                synthesized = False

        if not synthesized:
            _write_silence_wav(raw_clip, duration_s=max(0.0, float(l["end"]) - float(l["start"])))

        # Normalize to 16kHz mono PCM for alignment
        try:
            _ffmpeg_to_pcm16k(raw_clip, clip)
        except Exception:
            clip = raw_clip

        # Optional: retime clip to fit subtitle window (avoid early/late speech).
        try:
            from anime_v2.stages.align import retime_tts  # lazy import

            target_dur = max(0.05, float(l["end"]) - float(l["start"]))
            clip = retime_tts(clip, target_duration_s=target_dur, max_stretch=float(max_stretch))
        except Exception:
            pass
        clip_paths.append(clip)
        if progress_cb is not None:
            try:
                progress_cb(i + 1, total)
            except Exception:
                pass

    # Output paths
    if wav_out is None:
        # default: <stem>.tts.wav (stem is output folder name)
        wav_out = out_dir / f"{out_dir.name}.tts.wav"

    # Cross-job cache (coarse): if audio_hash provided and tts config stable, reuse tts wav + manifest.
    if audio_hash:
        sig = os.environ.get("SPEAKER_SIGNATURE")  # optional precomputed override
        if not sig:
            from anime_v2.utils.hashio import speaker_signature

            sig = speaker_signature(settings.tts_lang, settings.tts_speaker, settings.tts_speaker_wav)
        key = make_key("tts", {"audio": audio_hash, "tts_model": settings.tts_model, "lang": settings.tts_lang, "sig": sig})
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
                pass

    if job_id:
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        manifest = out_dir / "tts_manifest.json"
        if wav_out.exists() and manifest.exists() and stage_is_done(ckpt, "tts"):
            logger.info("[v2] tts stage checkpoint hit")
            return wav_out

    render_aligned_track(lines, clip_paths, wav_out)

    # Optional metadata
    try:
        write_json(
            out_dir / "tts_manifest.json",
            {"clips": [str(p) for p in clip_paths], "wav_out": str(wav_out), "lines": lines},
        )
    except Exception:
        pass

    if job_id:
        try:
            write_ckpt(job_id, "tts", {"tts_wav": wav_out, "manifest": out_dir / "tts_manifest.json"}, {"work_dir": str(out_dir)}, ckpt_path=ckpt_path)
        except Exception:
            pass

    if audio_hash:
        try:
            sig = os.environ.get("SPEAKER_SIGNATURE") or ""
            if not sig:
                from anime_v2.utils.hashio import speaker_signature

                sig = speaker_signature(settings.tts_lang, settings.tts_speaker, settings.tts_speaker_wav)
            key = make_key("tts", {"audio": audio_hash, "tts_model": settings.tts_model, "lang": settings.tts_lang, "sig": sig})
            cache_put(key, {"tts_wav": wav_out, "manifest": out_dir / "tts_manifest.json"}, meta={"created_at": time.time()})
        except Exception:
            pass

    logger.info("[v2] TTS done → %s", wav_out)
    return wav_out

