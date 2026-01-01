from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from anime_v2.utils.config import get_settings
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.stages.tts_engine import CoquiXTTS, choose_similar_voice


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

    # Engine (lazy-loaded per run)
    engine: CoquiXTTS | None = None
    try:
        engine = CoquiXTTS()
    except Exception as ex:
        logger.warning("[v2] TTS engine unavailable: %s", ex)

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
        speaker_wav = settings.tts_speaker_wav or speaker_rep_wav.get(speaker_id)

        synthesized = False
        if engine is not None and speaker_wav is not None and speaker_wav.exists():
            try:
                engine.synthesize(
                    text,
                    language=settings.tts_lang,
                    speaker_wav=speaker_wav,
                    out_path=raw_clip,
                )
                synthesized = True
            except Exception as ex:
                logger.warning("[v2] clone failed; using preset %s (%s)", speaker_id, ex)
                synthesized = False

        if not synthesized and engine is not None:
            # Choose best preset if we have speaker embedding; else default preset
            preset = settings.tts_speaker or "default"
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
                engine.synthesize(
                    text,
                    language=settings.tts_lang,
                    speaker_id=preset,
                    out_path=raw_clip,
                )
                synthesized = True
            except Exception as ex:
                logger.warning("[v2] preset synth failed (%s). Writing silence clip.", ex)
                synthesized = False

        if not synthesized:
            _write_silence_wav(raw_clip, duration_s=max(0.0, float(l["end"]) - float(l["start"])))

        # Normalize to 16kHz mono PCM for alignment
        try:
            _ffmpeg_to_pcm16k(raw_clip, clip)
        except Exception:
            clip = raw_clip
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

    render_aligned_track(lines, clip_paths, wav_out)

    # Optional metadata
    try:
        write_json(
            out_dir / "tts_manifest.json",
            {"clips": [str(p) for p in clip_paths], "wav_out": str(wav_out), "lines": lines},
        )
    except Exception:
        pass

    logger.info("[v2] TTS done → %s", wav_out)
    return wav_out

