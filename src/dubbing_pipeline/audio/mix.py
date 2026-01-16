from __future__ import annotations

import json
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.ffmpeg_safe import run_ffmpeg
from dubbing_pipeline.utils.log import logger


@dataclass(frozen=True, slots=True)
class MixParams:
    lufs_target: float = -16.0
    ducking: bool = True
    ducking_strength: float = 1.0  # 0.2..2.0 (scaled into sidechaincompress ratio/threshold)
    limiter: bool = True
    sample_rate: int = 48000


def _parse_loudnorm_json(stderr: str) -> dict[str, Any] | None:
    # loudnorm prints a JSON blob in stderr; extract the last {...}
    import re

    m = re.findall(r"\{[\s\S]*?\}", stderr or "")
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


def mix_dubbed_audio(
    *,
    background_wav: Path,
    tts_dialogue_wav: Path,
    out_wav: Path,
    params: MixParams | None = None,
    timeout_s: int = 1200,
) -> Path:
    """
    Professional-ish mixdown using ffmpeg:
    - optional sidechain ducking (bg ducks under TTS)
    - loudness normalize to LUFS (2-pass when possible)
    - limiter to prevent clipping

    Output is mono PCM WAV at `params.sample_rate`.
    """
    params = params or MixParams()
    s = get_settings()
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    def _wav_duration_s(p: Path) -> float:
        try:
            with wave.open(str(p), "rb") as wf:
                n = wf.getnframes()
                sr = wf.getframerate()
            return float(n) / float(sr) if sr else 0.0
        except Exception:
            return 0.0

    max_dur = max(
        _wav_duration_s(Path(background_wav)), _wav_duration_s(Path(tts_dialogue_wav)), 0.0
    )
    max_dur = max(0.1, float(max_dur))

    # Build filtergraph
    bg_stream = "0:a:0"
    tts_stream = "1:a:0"

    # Ducking tuned by strength
    strength = max(0.2, min(2.0, float(params.ducking_strength)))
    threshold = 0.02 / strength
    ratio = 8.0 * strength
    attack = 5
    release = 250

    fg = []
    # Workaround for ffmpeg 6.1 sidechaincompress label parsing:
    # keep TTS as a direct stream specifier (e.g. "1:a:0") rather than a named label.
    fg.append(f"[{bg_stream}]aresample={params.sample_rate},volume=0.90[bg];")
    if params.ducking:
        fg.append(
            f"[bg][{tts_stream}]"
            f"sidechaincompress=threshold={threshold:.5f}:ratio={ratio:.2f}:attack={attack}:release={release}"
            "[duck];"
        )
        fg.append(
            f"[duck][{tts_stream}]"
            "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0:weights='0.9 1.2'"
            "[mix0];"
        )
    else:
        fg.append(
            f"[bg][{tts_stream}]"
            "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0:weights='1.0 1.1'"
            "[mix0];"
        )

    # Loudnorm pass2 inserted later
    limiter = "alimiter=limit=0.891" if params.limiter else "anull"
    # Trim to finite duration (avoid apad infinite WAV output).
    fg.append(
        f"[mix0]aresample={params.sample_rate},atrim=0:{max_dur:.3f},asetpts=N/SR/TB,{limiter}[out]"
    )
    filtergraph_base = "".join(fg)

    def _run_with_loudnorm(loudnorm: str | None) -> subprocess.CompletedProcess[str] | None:
        fg_full = filtergraph_base
        if loudnorm:
            # Insert loudnorm just before limiter node by re-targeting:
            fg_full = filtergraph_base.replace(",asetpts=N/SR/TB,", f",asetpts=N/SR/TB,{loudnorm},")
        cmd = [
            str(s.ffmpeg_bin),
            "-y",
            "-i",
            str(background_wav),
            "-i",
            str(tts_dialogue_wav),
            "-filter_complex",
            fg_full,
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            str(int(params.sample_rate)),
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ]
        return run_ffmpeg(cmd, timeout_s=timeout_s, retries=0, capture=True)

    # Loudnorm: 2-pass if possible (for consistency), else single-pass.
    lufs = float(params.lufs_target)
    loud1 = f"loudnorm=I={lufs}:LRA=11:TP=-1.0:print_format=json"
    loud2 = None
    try:
        t0 = time.perf_counter()
        p1 = _run_with_loudnorm(loud1)
        stderr = (p1.stderr if p1 else "") or ""
        meas = _parse_loudnorm_json(stderr)
        if meas:
            loud2 = (
                "loudnorm="
                f"I={lufs}:LRA=11:TP=-1.0:"
                f"measured_I={meas.get('input_i')}:measured_LRA={meas.get('input_lra')}:"
                f"measured_TP={meas.get('input_tp')}:measured_thresh={meas.get('input_thresh')}:"
                "offset=0.0:linear=true:print_format=summary"
            )
            logger.info("[dp] mix: loudnorm pass1 ok (%.2fs)", time.perf_counter() - t0)
    except Exception as ex:
        logger.warning("[dp] mix: loudnorm pass1 failed (%s); using single-pass", ex)
        loud2 = f"loudnorm=I={lufs}:LRA=11:TP=-1.0:linear=true:print_format=summary"

    # Final render
    try:
        _run_with_loudnorm(loud2)
    except Exception as ex:
        raise RuntimeError(f"mix failed: {ex}") from ex

    return out_wav
