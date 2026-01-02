from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.ffmpeg_safe import run_ffmpeg
from anime_v2.utils.io import atomic_write_text
from anime_v2.utils.log import logger
from anime_v2.utils.vad import VADConfig, detect_speech_segments


@dataclass(frozen=True, slots=True)
class Region:
    start: float
    end: float
    kind: str  # music|singing|unknown|op|ed
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _wav_duration_s(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            n = wf.getnframes()
            sr = wf.getframerate()
        return float(n) / float(sr) if sr else 0.0
    except Exception:
        return 0.0


def _read_pcm16_mono(path: Path) -> tuple[list[float], int] | tuple[None, int]:
    """
    Read mono int16 wav into float samples [-1..1]. Returns (samples, sr).
    """
    try:
        with wave.open(str(path), "rb") as wf:
            sr = int(wf.getframerate())
            ch = int(wf.getnchannels())
            sw = int(wf.getsampwidth())
            if ch != 1 or sw != 2:
                return None, sr
            frames = wf.readframes(wf.getnframes())
        n = len(frames) // 2
        samples = []
        for i in range(0, n * 2, 2):
            v = int.from_bytes(frames[i : i + 2], "little", signed=True)
            samples.append(float(v) / 32768.0)
        return samples, sr
    except Exception:
        return None, 0


def _rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    s2 = 0.0
    for x in samples:
        s2 += float(x * x)
    return math.sqrt(s2 / float(len(samples)))


def _spectral_features_numpy(samples: list[float], sr: int) -> tuple[float | None, float | None, float | None]:
    """
    Returns (centroid_hz, flatness, rolloff_hz) using numpy if available.
    """
    try:
        import numpy as np  # type: ignore
    except Exception:
        return None, None, None
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < 256 or sr <= 0:
            return None, None, None
        # Hann window FFT
        w = np.hanning(x.size)
        X = np.fft.rfft(x * w)
        mag = np.abs(X) + 1e-9
        freqs = np.fft.rfftfreq(x.size, 1.0 / float(sr))
        # centroid
        centroid = float((freqs * mag).sum() / mag.sum())
        # flatness
        flat = float(np.exp(np.mean(np.log(mag))) / (np.mean(mag)))
        # rolloff (85%)
        cdf = np.cumsum(mag)
        thr = 0.85 * float(cdf[-1])
        idx = int(np.searchsorted(cdf, thr))
        roll = float(freqs[min(idx, freqs.size - 1)])
        return centroid, flat, roll
    except Exception:
        return None, None, None


def _coverage_ratio(intervals: list[tuple[float, float]], *, start: float, end: float) -> float:
    if end <= start:
        return 0.0
    tot = 0.0
    for s, e in intervals:
        ov = max(0.0, min(end, float(e)) - max(start, float(s)))
        tot += ov
    return max(0.0, min(1.0, tot / (end - start)))


def _merge_regions(regs: list[Region], *, gap_s: float = 0.5) -> list[Region]:
    if not regs:
        return []
    regs2 = sorted(regs, key=lambda r: (float(r.start), float(r.end)))
    out: list[Region] = []
    cur = regs2[0]
    for r in regs2[1:]:
        if float(r.start) <= float(cur.end) + float(gap_s):
            cur = Region(
                start=float(cur.start),
                end=max(float(cur.end), float(r.end)),
                kind=str(cur.kind),
                confidence=max(float(cur.confidence), float(r.confidence)),
                reason=f"{cur.reason};{r.reason}",
            )
        else:
            out.append(cur)
            cur = r
    out.append(cur)
    return out


def analyze_audio_for_music_regions(
    audio_path: Path,
    *,
    mode: str = "auto",
    window_s: float = 1.0,
    hop_s: float = 0.5,
    threshold: float = 0.70,
    vad_cfg: VADConfig | None = None,
) -> list[Region]:
    """
    Offline-first music/singing detector with fallbacks.

    Strategies:
    - "classifier": (reserved; currently falls back to heuristic)
    - "heuristic": uses RMS + VAD speech ratio + optional numpy FFT features
    - "auto": prefers classifier if available, else heuristic
    """
    audio_path = Path(audio_path)
    dur = _wav_duration_s(audio_path)
    if dur <= 0:
        return []

    m = str(mode or "auto").strip().lower()
    if m not in {"auto", "heuristic", "classifier"}:
        m = "auto"

    used = "heuristic"
    # Hook for future classifier integration (optional)
    if m == "classifier":
        used = "heuristic"

    cfg = vad_cfg or VADConfig()
    # Prefer webrtcvad-based speech detection when available; energy-only fallback is not
    # reliable for music detection (it flags any loud audio as "speech").
    use_webrtc = False
    try:
        import webrtcvad  # type: ignore  # noqa: F401

        use_webrtc = True
    except Exception:
        use_webrtc = False

    speech: list[tuple[float, float]] = []
    if use_webrtc:
        try:
            speech = detect_speech_segments(audio_path, cfg)
        except Exception:
            speech = []

    logger.info(
        "music_detect_start",
        audio=str(audio_path),
        mode=m,
        detector=used,
        duration_s=float(dur),
        window_s=float(window_s),
        hop_s=float(hop_s),
        threshold=float(threshold),
        vad_segments=len(speech),
        webrtcvad=bool(use_webrtc),
    )

    regs: list[Region] = []
    w = max(0.5, float(window_s))
    h = max(0.2, float(hop_s))

    # Read windows via wave to avoid loading entire file.
    try:
        wf = wave.open(str(audio_path), "rb")
    except Exception:
        return []
    sr = int(wf.getframerate())
    ch = int(wf.getnchannels())
    sw = int(wf.getsampwidth())
    if ch != 1 or sw != 2 or sr <= 0:
        logger.warning("music_detect_expected_pcm16_mono16k", sr=sr, ch=ch, sw=sw)
    try:
        for k in range(int(max(1, math.ceil(dur / h)))):
            start = float(k) * h
            end = min(dur, start + w)
            if end - start < 0.25:
                break

            i0 = max(0, int(start * sr))
            i1 = max(i0 + 1, int(end * sr))
            wf.setpos(min(i0, max(0, wf.getnframes() - 1)))
            buf = wf.readframes(max(1, i1 - i0))
            n = len(buf) // 2
            win = []
            for i in range(0, n * 2, 2):
                v = int.from_bytes(buf[i : i + 2], "little", signed=True)
                win.append(float(v) / 32768.0)

            rms = _rms(win)
            speech_ratio = _coverage_ratio(speech, start=start, end=end) if use_webrtc else None
            centroid, flat, roll = _spectral_features_numpy(win, sr)

            # Simple scoring. If webrtcvad is available we can use speech_ratio strongly.
            # Otherwise rely on spectral stats more heavily (offline, no heavy deps).
            score = 0.0
            if speech_ratio is not None:
                score += (1.0 - float(speech_ratio)) * 0.70
                if rms >= 0.03:
                    score += min(1.0, (rms - 0.03) / 0.12) * 0.15
                if flat is not None:
                    score += max(0.0, min(1.0, float(flat))) * 0.08
                if centroid is not None:
                    score += max(0.0, min(1.0, float(centroid) / 2500.0)) * 0.07
            else:
                if rms >= 0.03:
                    score += min(1.0, (rms - 0.03) / 0.12) * 0.25
                if flat is not None:
                    score += max(0.0, min(1.0, float(flat))) * 0.25
                if centroid is not None:
                    score += max(0.0, min(1.0, float(centroid) / 2500.0)) * 0.25
                if roll is not None:
                    score += max(0.0, min(1.0, float(roll) / 6000.0)) * 0.25

            score = max(0.0, min(1.0, score))
            if score >= float(threshold):
                kind = "music"
                sr_s = f"{float(speech_ratio):.2f}" if speech_ratio is not None else "na"
                reason = f"heuristic score={score:.3f} speech_ratio={sr_s} rms={rms:.3f}"
                if flat is not None:
                    reason += f" flat={float(flat):.3f}"
                if centroid is not None:
                    reason += f" centroid={float(centroid):.0f}"
                if roll is not None:
                    reason += f" rolloff={float(roll):.0f}"
                regs.append(
                    Region(
                        start=float(start),
                        end=float(end),
                        kind=kind,
                        confidence=float(score),
                        reason=reason,
                    )
                )
    finally:
        try:
            wf.close()
        except Exception:
            pass

    merged = _merge_regions(regs, gap_s=max(0.2, h))
    logger.info("music_detect_done", regions=len(merged))
    return merged


def detect_op_ed(
    audio_path: Path,
    *,
    music_regions: list[Region],
    seconds: int = 90,
    threshold: float = 0.70,
) -> dict[str, Region | None]:
    """
    Best-effort OP/ED specialization:
    - Candidate OP: [0..N]
    - Candidate ED: [dur-N..dur]
    - Confirm if a music region overlaps sufficiently and meets threshold.
    """
    audio_path = Path(audio_path)
    dur = _wav_duration_s(audio_path)
    n = max(10.0, float(seconds))
    out: dict[str, Region | None] = {"op": None, "ed": None}
    if dur <= 0:
        return out

    def best_in_range(a: float, b: float) -> Region | None:
        best = None
        best_c = -1.0
        for r in music_regions:
            if r.end <= a or r.start >= b:
                continue
            if r.confidence >= threshold and r.confidence > best_c:
                best = r
                best_c = float(r.confidence)
        return best

    op = best_in_range(0.0, min(dur, n))
    ed = best_in_range(max(0.0, dur - n), dur)
    if op is not None:
        out["op"] = Region(
            start=float(op.start),
            end=float(op.end),
            kind="op",
            confidence=float(op.confidence),
            reason="op_ed_confirmed:" + str(op.reason),
        )
    if ed is not None:
        out["ed"] = Region(
            start=float(ed.start),
            end=float(ed.end),
            kind="ed",
            confidence=float(ed.confidence),
            reason="op_ed_confirmed:" + str(ed.reason),
        )
    return out


def should_suppress_segment(start_s: float, end_s: float, regions: list[Region] | list[dict]) -> bool:
    """
    Suppress dubbing when a segment overlaps any detected music region.
    """
    start_s = float(start_s)
    end_s = float(end_s)
    if end_s <= start_s:
        return False
    for r in regions:
        try:
            rs = float(r.start) if isinstance(r, Region) else float(r.get("start", 0.0))  # type: ignore[union-attr]
            re = float(r.end) if isinstance(r, Region) else float(r.get("end", 0.0))  # type: ignore[union-attr]
        except Exception:
            continue
        if re <= start_s or rs >= end_s:
            continue
        return True
    return False


def build_music_preserving_bed(
    *,
    background_wav: Path,
    original_wav: Path,
    regions: list[Region] | list[dict],
    out_wav: Path,
) -> Path:
    """
    Build a bed track that uses:
    - background_wav outside music regions (e.g. demucs no_vocals)
    - original_wav inside music regions (preserves singing)
    """
    s = get_settings()
    background_wav = Path(background_wav)
    original_wav = Path(original_wav)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if not regions:
        try:
            from anime_v2.utils.io import atomic_copy

            atomic_copy(background_wav, out_wav)
        except Exception:
            out_wav.write_bytes(background_wav.read_bytes())
        return out_wav

    # Build expression: between(t,a,b)+between(t,c,d)+...
    parts = []
    for r in regions:
        try:
            a0 = float(r.start) if isinstance(r, Region) else float(r.get("start", 0.0))  # type: ignore[union-attr]
            b0 = float(r.end) if isinstance(r, Region) else float(r.get("end", 0.0))  # type: ignore[union-attr]
        except Exception:
            continue
        a = max(0.0, float(a0))
        b = max(a, float(b0))
        parts.append(f"between(t,{a:.3f},{b:.3f})")
    cond = "+".join(parts) if parts else "0"
    bg_vol = f"volume='if({cond},0,1)':eval=frame"
    orig_vol = f"volume='if({cond},1,0)':eval=frame"
    # Mix the two (they are mutually exclusive by design)
    run_ffmpeg(
        [
            str(s.ffmpeg_bin),
            "-y",
            "-i",
            str(background_wav),
            "-i",
            str(original_wav),
            "-filter_complex",
            f"[0:a]{bg_vol}[bg];[1:a]{orig_vol}[orig];[bg][orig]amix=inputs=2:normalize=0[out]",
            "-map",
            "[out]",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        timeout_s=600,
        retries=0,
        capture=True,
    )
    return out_wav


def write_regions_json(regions: list[Region], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps({"version": 1, "regions": [r.to_dict() for r in regions]}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def write_oped_json(oped: dict[str, Region | None], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 1, "op": None, "ed": None}
    if oped.get("op") is not None:
        payload["op"] = oped["op"].to_dict()  # type: ignore[union-attr]
    if oped.get("ed") is not None:
        payload["ed"] = oped["ed"].to_dict()  # type: ignore[union-attr]
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

