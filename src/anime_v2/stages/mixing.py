from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class MixConfig:
    profile: str = "streaming"  # streaming|broadcast|simple
    separate_vocals: bool = False
    emit: tuple[str, ...] = ("mkv", "mp4")  # mkv,mp4
    demucs_timeout_s: int = 600
    enable_demucs_env: bool = bool(int(os.environ.get("ENABLE_DEMUCS", "0") or "0"))


def _ffprobe_duration_s(path: Path) -> float | None:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(p.stdout)
        dur = float(data["format"]["duration"])
        return dur if dur > 0 else None
    except Exception:
        return None


def _srt_ok(srt: Path | None) -> Path | None:
    if srt is None:
        return None
    try:
        if not srt.exists() or srt.stat().st_size == 0:
            return None
    except Exception:
        return None
    return srt


def _parse_loudnorm_json(stderr: str) -> dict[str, Any] | None:
    # loudnorm prints a JSON blob in stderr; extract the last {...}
    m = re.findall(r"\{[\s\S]*?\}", stderr)
    if not m:
        return None
    for cand in reversed(m):
        try:
            data = json.loads(cand)
            if isinstance(data, dict) and "input_i" in data:
                return data
        except Exception:
            continue
    return None


def _build_filtergraph(
    *,
    bg_stream: str,
    tts_stream: str,
    loudnorm: str | None,
    limiter: bool,
    vid_dur: float | None,
) -> tuple[str, str]:
    # Sidechaincompress parameters tuned for "duck ~8–12 dB during speech"
    # We pre-attenuate JP a bit, then compress keyed by TTS.
    threshold = 0.02
    ratio = 10
    attack = 5
    release = 250

    # Post bus processing
    bus = "mix"
    fg = []
    # NOTE: ffmpeg 6.1 in this repo treats some intermediate labels as stream specifiers
    # when used with sidechaincompress. Work around by referencing the TTS input
    # directly as a stream specifier (e.g. "1:a:0" or "2:a:0") everywhere.
    fg.append(f"[{bg_stream}]aresample=48000,volume=0.85[bg];")

    # Ducking bus: sidechaincompress reduces bg when tts is present (tts is key input)
    fg.append(
        f"[bg][{tts_stream}]"
        f"sidechaincompress=threshold={threshold}:ratio={ratio}:attack={attack}:release={release}"
        "[duck];"
    )

    # Mix bed + TTS (boost TTS a bit for intelligibility)
    fg.append(f"[duck][{tts_stream}]amix=inputs=2:normalize=0:weights='0.9 1.1'[{bus}0];")

    # Ensure audio is padded/truncated to match video duration when known
    if vid_dur is not None:
        fg.append(f"[{bus}0]apad,atrim=0:{vid_dur:.3f},asetpts=N/SR/TB[{bus}1];")
    else:
        fg.append(f"[{bus}0]apad,asetpts=N/SR/TB[{bus}1];")

    # Loudness normalize
    if loudnorm:
        fg.append(f"[{bus}1]{loudnorm}[{bus}2];")
        out_bus = f"{bus}2"
    else:
        out_bus = f"{bus}1"

    # Soft limiter / compressor for peaks
    if limiter:
        # alimiter limit is linear; 0.891 ~= -1.0 dBFS
        fg.append(f"[{out_bus}]alimiter=limit=0.891[{bus}out];")
    else:
        fg.append(f"[{out_bus}]anull[{bus}out];")

    return "".join(fg), f"{bus}out"


def _run_demucs_if_enabled(*, audio_wav: Path, out_dir: Path, timeout_s: int) -> Path | None:
    """
    Run demucs to produce a background bed with vocals reduced.
    Returns path to "no_vocals.wav" if available, else None.
    """
    try:
        import demucs  # type: ignore  # noqa: F401
    except Exception:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    demucs_out = out_dir / "demucs"
    demucs_out.mkdir(parents=True, exist_ok=True)
    # demucs CLI writes outputs under <out>/<model>/<track>/
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        "htdemucs",
        "--two-stems",
        "vocals",
        "-o",
        str(demucs_out),
        str(audio_wav),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout_s, capture_output=True, text=True)
    except Exception as ex:
        logger.warning("[v2] demucs failed/timeout (%s); skipping separation", ex)
        return None

    # Find no_vocals.wav
    # Common layout: demucs/htdemucs/<stem>/no_vocals.wav
    stem = audio_wav.stem
    candidate = demucs_out / "htdemucs" / stem / "no_vocals.wav"
    if candidate.exists():
        return candidate
    # Fallback: any no_vocals.wav under demucs_out
    for p in demucs_out.rglob("no_vocals.wav"):
        if p.exists():
            return p
    return None


def mix(
    *,
    video_in: Path,
    tts_wav: Path,
    srt: Path | None,
    out_dir: Path,
    cfg: MixConfig,
) -> dict[str, Path]:
    """
    Produce broadcast-quality outputs:
      - MKV: copy video, AAC audio, SRT subs (soft)
      - MP4: H.264 video, AAC audio, mov_text subs (soft) when possible
    Returns dict of produced outputs by format.
    """
    video_in = Path(video_in)
    tts_wav = Path(tts_wav)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = (cfg.profile or "streaming").lower()
    emit = {e.strip().lower() for e in (cfg.emit or ()) if e and str(e).strip()}
    if not emit:
        emit = {"mkv", "mp4"}

    srt = _srt_ok(srt)
    vid_dur = _ffprobe_duration_s(video_in)

    # Decide loudness target
    loudnorm_target = None
    if profile == "broadcast":
        loudnorm_target = -23
    elif profile == "streaming":
        loudnorm_target = -16
    elif profile == "simple":
        loudnorm_target = None
    else:
        loudnorm_target = -16

    # Optional demucs: derive bed from separated no_vocals.wav instead of original audio
    bg_input = video_in  # default: use video audio
    bg_is_wav = False
    if cfg.separate_vocals and cfg.enable_demucs_env:
        try:
            # Extract original audio to a demucs-friendly WAV (stereo 44.1k)
            demucs_wav = out_dir / "orig_audio_44k.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_in), "-vn", "-ac", "2", "-ar", "44100", str(demucs_wav)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            bed = _run_demucs_if_enabled(audio_wav=demucs_wav, out_dir=out_dir, timeout_s=int(cfg.demucs_timeout_s))
            if bed is not None and bed.exists():
                bg_input = bed
                bg_is_wav = True
                logger.info("[v2] mixing: using demucs bed %s", bed)
            else:
                logger.info("[v2] mixing: demucs not available; using baseline ducking")
        except Exception as ex:
            logger.warning("[v2] mixing: demucs path failed (%s); using baseline ducking", ex)

    # Build filtergraph (two-pass loudnorm when enabled)
    outputs: dict[str, Path] = {}

    def _run_once(out_path: Path, *, container: str, vcodec: str, acodec: str, scodec: str | None, loudnorm_filter: str | None) -> None:
        # Inputs:
        # 0: video (with audio) OR bg wav (if demucs)
        # 1: tts wav
        # 2: srt (optional)
        cmd = ["ffmpeg", "-y"]
        if bg_is_wav:
            # input0: video (for v), input1: bed wav, input2: tts wav
            cmd += ["-i", str(video_in), "-i", str(bg_input), "-i", str(tts_wav)]
            bg_label = "1:a:0"
            tts_label = "2:a:0"
            video_map = ["-map", "0:v:0"]
        else:
            # input0: video (v+a), input1: tts wav
            cmd += ["-i", str(video_in), "-i", str(tts_wav)]
            bg_label = "0:a:0"
            tts_label = "1:a:0"
            video_map = ["-map", "0:v:0"]

        if srt is not None:
            cmd += ["-i", str(srt)]
            srt_idx = 3 if bg_is_wav else 2
        else:
            srt_idx = None

        filtergraph, out_bus = _build_filtergraph(
            bg_stream=bg_label,
            tts_stream=tts_label,
            loudnorm=loudnorm_filter,
            limiter=True,
            vid_dur=vid_dur,
        )

        cmd += ["-filter_complex", filtergraph]
        cmd += video_map
        cmd += ["-map", f"[{out_bus}]"]
        if srt_idx is not None:
            cmd += ["-map", f"{srt_idx}:s:0"]

        # video
        cmd += ["-c:v", vcodec]
        if vcodec == "libx264":
            cmd += ["-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

        # audio
        cmd += ["-c:a", acodec, "-b:a", "192k", "-metadata:s:a:0", "language=eng"]

        # subs (optional)
        if srt_idx is not None and scodec is not None:
            cmd += ["-c:s", scodec, "-disposition:s:0", "default", "-metadata:s:s:0", "language=eng"]

        # duration cap (keeps container sane)
        if vid_dur is not None:
            cmd += ["-t", f"{vid_dur:.3f}"]

        cmd += [str(out_path)]
        subprocess.run(cmd, check=True)

    # Loudnorm 2-pass for streaming/broadcast
    loudnorm_pass2: str | None = None
    if loudnorm_target is not None:
        # Pass 1: compute measurements (audio-only null output)
        try:
            t0 = time.perf_counter()
            loud1 = f"loudnorm=I={loudnorm_target}:LRA=11:TP=-1.0:print_format=json"
            # Minimal pass: audio-only, no subs/video. Use baseline bed selection.
            if bg_is_wav:
                cmd = ["ffmpeg", "-y", "-i", str(bg_input), "-i", str(tts_wav), "-filter_complex"]
                fg, out_bus = _build_filtergraph(bg_stream="0:a:0", tts_stream="1:a:0", loudnorm=loud1, limiter=False, vid_dur=vid_dur)
            else:
                cmd = ["ffmpeg", "-y", "-i", str(video_in), "-i", str(tts_wav), "-filter_complex"]
                fg, out_bus = _build_filtergraph(bg_stream="0:a:0", tts_stream="1:a:0", loudnorm=loud1, limiter=False, vid_dur=vid_dur)
            cmd += [fg]
            cmd += ["-map", f"[{out_bus}]", "-f", "null", "-"]
            p = subprocess.run(cmd, check=True, capture_output=True, text=True)
            meas = _parse_loudnorm_json(p.stderr)
            if meas:
                loudnorm_pass2 = (
                    "loudnorm="
                    f"I={loudnorm_target}:LRA=11:TP=-1.0:"
                    f"measured_I={meas.get('input_i')}:measured_LRA={meas.get('input_lra')}:"
                    f"measured_TP={meas.get('input_tp')}:measured_thresh={meas.get('input_thresh')}:"
                    f"offset={meas.get('target_offset')}:linear=true:print_format=summary"
                )
                logger.info("[v2] mixing: loudnorm pass1 ok (%.2fs)", time.perf_counter() - t0)
        except Exception as ex:
            logger.warning("[v2] mixing: loudnorm pass1 failed (%s); using single-pass", ex)
            loudnorm_pass2 = f"loudnorm=I={loudnorm_target}:LRA=11:TP=-1.0:linear=true:print_format=summary"

    # Emit outputs
    if "mkv" in emit:
        out_mkv = out_dir / "dub.mkv"
        # MKV: copy video, AAC audio, SRT subs
        _run_once(out_mkv, container="mkv", vcodec="copy", acodec="aac", scodec="srt", loudnorm_filter=loudnorm_pass2)
        outputs["mkv"] = out_mkv

    if "mp4" in emit:
        out_mp4 = out_dir / "dub.mp4"
        # MP4: H.264 + AAC, subtitles as mov_text when present
        _run_once(
            out_mp4,
            container="mp4",
            vcodec="libx264",
            acodec="aac",
            scodec="mov_text",
            loudnorm_filter=loudnorm_pass2,
        )
        outputs["mp4"] = out_mp4

    return outputs

