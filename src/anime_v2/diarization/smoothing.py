from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class Scene:
    start: float
    end: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SmoothingChange:
    start: float
    end: float
    speaker_from: str
    speaker_to: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _wav_duration_s(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
        return float(n) / float(sr) if sr else 0.0
    except Exception:
        return 0.0


def _rms_from_pcm16(buf: bytes) -> float:
    if not buf:
        return 0.0
    n = len(buf) // 2
    if n <= 0:
        return 0.0
    s2 = 0.0
    for i in range(0, n * 2, 2):
        v = int.from_bytes(buf[i : i + 2], "little", signed=True)
        x = float(v) / 32768.0
        s2 += x * x
    return math.sqrt(s2 / float(n))


def _spectral_centroid_numpy(buf: bytes, sr: int) -> float | None:
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None
    try:
        n = len(buf) // 2
        if n < 256:
            return None
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        w = np.hanning(x.size)
        X = np.fft.rfft(x * w)
        mag = np.abs(X) + 1e-9
        freqs = np.fft.rfftfreq(x.size, 1.0 / float(sr))
        return float((freqs * mag).sum() / mag.sum())
    except Exception:
        return None


def detect_scenes_audio(
    wav_path: Path,
    *,
    window_s: float = 0.5,
    hop_s: float = 0.25,
    silence_rms: float = 0.008,
    min_silence_s: float = 0.6,
    energy_jump: float = 2.2,
    centroid_jump_hz: float = 900.0,
    min_scene_s: float = 4.0,
) -> list[Scene]:
    """
    Audio-only scene boundary detection (offline, fast).

    Heuristics:
    - long silence boundaries
    - sudden RMS jump + optional spectral centroid jump
    """
    p = Path(wav_path)
    dur = _wav_duration_s(p)
    if dur <= 0:
        return []
    try:
        wf = wave.open(str(p), "rb")
    except Exception:
        return [Scene(start=0.0, end=float(dur), reason="fallback_full")]

    sr = int(wf.getframerate())
    ch = int(wf.getnchannels())
    sw = int(wf.getsampwidth())
    if ch != 1 or sw != 2:
        logger.info("scene_detect_non_pcm16_mono", sr=sr, ch=ch, sw=sw)
    win = max(0.2, float(window_s))
    hop = max(0.1, float(hop_s))

    frames_win = max(1, int(sr * win))
    frames_hop = max(1, int(sr * hop))

    # rolling stats
    bounds: list[tuple[float, str]] = []
    prev_rms = None
    prev_cent = None

    # silence tracking
    silence_run = 0.0
    t = 0.0
    try:
        i0 = 0
        n_total = int(wf.getnframes())
        while i0 < n_total:
            wf.setpos(i0)
            buf = wf.readframes(min(frames_win, n_total - i0))
            rms = _rms_from_pcm16(buf)
            cent = _spectral_centroid_numpy(buf, sr)

            # silence boundary
            if rms <= float(silence_rms):
                silence_run += hop
            else:
                if silence_run >= float(min_silence_s) and t > 0.0:
                    bounds.append((max(0.0, t - silence_run / 2.0), "silence"))
                silence_run = 0.0

            # jump boundary
            if prev_rms is not None:
                ratio = (rms + 1e-6) / (prev_rms + 1e-6)
                cent_jump = (
                    abs(float(cent) - float(prev_cent))
                    if (cent is not None and prev_cent is not None)
                    else 0.0
                )
                if ratio >= float(energy_jump) and cent_jump >= float(centroid_jump_hz):
                    bounds.append((t, "energy+spectral_jump"))

            prev_rms = rms
            prev_cent = cent if cent is not None else prev_cent
            t += hop
            i0 += frames_hop
    finally:
        try:
            wf.close()
        except Exception:
            pass

    # dedupe bounds close together
    bounds.sort(key=lambda x: x[0])
    merged: list[tuple[float, str]] = []
    for bt, reason in bounds:
        if not merged or (bt - merged[-1][0]) > 1.0:
            merged.append((bt, reason))
        else:
            merged[-1] = (merged[-1][0], merged[-1][1] + "+" + reason)

    # build scenes
    cuts = [0.0] + [max(0.0, min(float(dur), float(bt))) for bt, _ in merged] + [float(dur)]
    cuts = sorted(set(cuts))
    scenes: list[Scene] = []
    for a, b in zip(cuts, cuts[1:], strict=False):
        if (b - a) < float(min_scene_s) and scenes:
            # extend previous
            prev = scenes[-1]
            scenes[-1] = Scene(start=prev.start, end=b, reason=prev.reason)
        else:
            scenes.append(Scene(start=a, end=b, reason="audio"))
    if not scenes:
        scenes = [Scene(start=0.0, end=float(dur), reason="fallback_full")]
    return scenes


def smooth_speakers_in_scenes(
    utts: list[dict[str, Any]],
    scenes: list[Scene],
    *,
    min_turn_s: float = 0.6,
    surround_gap_s: float = 0.4,
    conf_key: str = "conf",
) -> tuple[list[dict[str, Any]], list[SmoothingChange]]:
    """
    Scene-aware speaker smoothing:
    - Merge micro-turns (<min_turn_s) that are surrounded by the same speaker.
    - Prefer changing low-confidence micro-turns first when conf is present.
    """
    if not utts:
        return [], []
    utts2 = [dict(u) for u in sorted(utts, key=lambda x: (float(x.get("start", 0.0)), float(x.get("end", 0.0))))]
    changes: list[SmoothingChange] = []

    def _scene_index(t: float) -> int:
        for i, sc in enumerate(scenes):
            if float(sc.start) <= t < float(sc.end):
                return i
        return max(0, len(scenes) - 1)

    for i in range(1, len(utts2) - 1):
        u = utts2[i]
        prev = utts2[i - 1]
        nxt = utts2[i + 1]
        s = float(u.get("start", 0.0))
        e = float(u.get("end", s))
        dur = max(0.0, e - s)
        if dur <= 0:
            continue
        # must be within same scene
        si = _scene_index(s)
        if _scene_index(float(prev.get("start", 0.0))) != si or _scene_index(float(nxt.get("start", 0.0))) != si:
            continue
        sp = str(u.get("speaker") or "")
        sp_prev = str(prev.get("speaker") or "")
        sp_next = str(nxt.get("speaker") or "")
        if not sp or not sp_prev or not sp_next:
            continue
        if sp_prev != sp_next:
            continue
        if sp == sp_prev:
            continue

        gap_prev = max(0.0, s - float(prev.get("end", 0.0)))
        gap_next = max(0.0, float(nxt.get("start", 0.0)) - e)
        if gap_prev > float(surround_gap_s) or gap_next > float(surround_gap_s):
            continue

        conf = u.get(conf_key)
        try:
            conf_f = float(conf) if conf is not None else None
        except Exception:
            conf_f = None

        # decide to merge
        if dur < float(min_turn_s) or (conf_f is not None and conf_f < 0.5 and dur < float(min_turn_s) * 1.5):
            u["speaker_original"] = sp
            u["speaker"] = sp_prev
            changes.append(
                SmoothingChange(
                    start=s,
                    end=e,
                    speaker_from=sp,
                    speaker_to=sp_prev,
                    reason="micro_turn_surrounded",
                )
            )

    return utts2, changes


def write_speaker_smoothing_report(
    out_path: Path,
    *,
    scenes: list[Scene],
    changes: list[SmoothingChange],
    enabled: bool,
    config: dict[str, Any],
) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "enabled": bool(enabled),
        "config": dict(config),
        "scenes": [s.to_dict() for s in scenes],
        "changes": [c.to_dict() for c in changes],
    }
    atomic_write_text(out_path, json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

