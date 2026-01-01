from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.log import logger
from anime_v2.utils.vad import VADConfig, detect_speech_segments


@dataclass(frozen=True, slots=True)
class AlignConfig:
    aligner: str = "auto"  # auto|aeneas|heuristic
    max_stretch: float = 0.15
    vad: VADConfig = VADConfig()
    snap_ms: int = 300
    wpm_min: int = 120
    wpm_max: int = 220


_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def _is_japanese(text: str) -> bool:
    return bool(_JA_RE.search(text or ""))


def _count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _snap(value_s: float, anchors: list[float], *, window_s: float) -> float:
    if not anchors:
        return value_s
    best = None
    best_d = 1e9
    for a in anchors:
        d = abs(a - value_s)
        if d <= window_s and d < best_d:
            best = a
            best_d = d
    return float(best) if best is not None else value_s


def retime_tts(wav_in: Path, *, target_duration_s: float, max_stretch: float = 0.15) -> Path:
    """
    Time-stretch a 16kHz mono WAV to fit target_duration_s.
    - Uses librosa.effects.time_stretch when available
    - Caps rate deviation to +/- max_stretch
    - If still short, pads silence; if long, trims
    """
    wav_in = Path(wav_in)
    if target_duration_s <= 0:
        return wav_in

    try:
        import numpy as np  # type: ignore
        import soundfile as sf  # type: ignore
    except Exception:
        return wav_in

    try:
        y, sr = sf.read(str(wav_in), dtype="float32", always_2d=False)
        if sr != 16000:
            # Avoid resampling here; upstream should normalize
            return wav_in
        if y.ndim != 1:
            y = y[:, 0]
    except Exception:
        return wav_in

    cur = float(len(y)) / 16000.0
    if cur <= 0:
        return wav_in

    # desired rate (librosa rate>1 => faster => shorter)
    rate = cur / float(target_duration_s)
    min_rate = 1.0 - float(max_stretch)
    max_rate = 1.0 + float(max_stretch)
    rate_clamped = float(min(max(rate, min_rate), max_rate))

    y_out = y
    try:
        import librosa  # type: ignore

        if abs(rate_clamped - 1.0) >= 0.01:
            y_out = librosa.effects.time_stretch(y_out, rate=rate_clamped)
    except Exception:
        # if librosa isn't available, fall back to pad/trim only
        y_out = y

    target_n = int(round(target_duration_s * 16000))
    if target_n <= 0:
        return wav_in

    if len(y_out) < target_n:
        pad = np.zeros((target_n - len(y_out),), dtype=np.float32)
        y_out = np.concatenate([y_out, pad], axis=0)
    elif len(y_out) > target_n:
        y_out = y_out[:target_n]

    out = wav_in.with_suffix(".retimed.wav")
    try:
        sf.write(str(out), y_out, 16000, subtype="PCM_16")
        return out
    except Exception:
        return wav_in


def _aeneas_align(
    audio_path: Path, fragments: list[str], *, lang: str
) -> list[tuple[float, float] | None]:
    """
    Returns list of (start,end) for each fragment index, or None if alignment failed for that fragment.
    """
    from tempfile import TemporaryDirectory

    try:
        from aeneas.executetask import ExecuteTask  # type: ignore
        from aeneas.task import Task  # type: ignore
    except Exception as ex:
        raise RuntimeError(f"aeneas not installed: {ex}") from ex

    with TemporaryDirectory(prefix="anime_v2_align_") as td:
        td_p = Path(td)
        txt = td_p / "fragments.txt"
        out_json = td_p / "syncmap.json"
        txt.write_text("\n".join(fragments), encoding="utf-8")

        # aeneas language codes: eng, jpn, ...
        cfg = f"task_language={lang}|is_text_type=plain|os_task_file_format=json"
        task = Task(config_string=cfg)
        task.audio_file_path_absolute = str(audio_path)
        task.text_file_path_absolute = str(txt)
        task.sync_map_file_path_absolute = str(out_json)

        ExecuteTask(task).execute()
        task.output_sync_map_file()

        import json

        data = json.loads(out_json.read_text(encoding="utf-8"))
        fr = (data.get("fragments") or []) if isinstance(data, dict) else []
        aligned: list[tuple[float, float] | None] = []
        for f in fr:
            try:
                b = float(f.get("begin", "0"))
                e = float(f.get("end", "0"))
                if e <= b:
                    aligned.append(None)
                else:
                    aligned.append((b, e))
            except Exception:
                aligned.append(None)
        # Ensure length matches requested fragments
        if len(aligned) < len(fragments):
            aligned.extend([None] * (len(fragments) - len(aligned)))
        return aligned[: len(fragments)]


def realign_srt(
    audio_path: Path, segments: list[dict[str, Any]], cfg: AlignConfig
) -> list[dict[str, Any]]:
    """
    Adjust segment start/end to match speech in audio_path.
    Preserves all segment fields; updates start/end and adds alignment metadata.
    """
    audio_path = Path(audio_path)
    if not segments:
        return []
    if not audio_path.exists():
        return segments

    aligner = (cfg.aligner or "auto").lower()
    if aligner not in {"auto", "aeneas", "heuristic"}:
        aligner = "auto"

    # Sort by current time order
    segs = [dict(s) for s in segments]
    segs.sort(key=lambda s: (float(s.get("start", 0.0)), float(s.get("end", 0.0))))

    # Silence anchors via VAD: snap boundaries to nearest speech boundary
    speech = detect_speech_segments(audio_path, cfg.vad)
    anchors = sorted({t for s, e in speech for t in (float(s), float(e))})
    snap_window_s = float(cfg.snap_ms) / 1000.0

    # aeneas path
    used = "heuristic"
    if aligner in {"auto", "aeneas"}:
        # Prefer aligning with JP-ish text when present; else EN
        fragments = []
        langs = []
        for s in segs:
            t = str(s.get("align_text") or s.get("src_text") or s.get("text") or "").strip()
            fragments.append(t if t else " ")
            langs.append("jpn" if _is_japanese(t) else "eng")
        # Single language required by aeneas per task; pick majority
        lang = "jpn" if langs.count("jpn") >= langs.count("eng") else "eng"
        try:
            times = _aeneas_align(audio_path, fragments, lang=lang)
            for s, te in zip(segs, times, strict=False):
                if te is None:
                    continue
                s["start"] = float(te[0])
                s["end"] = float(te[1])
                s["aligned_by"] = "aeneas"
            used = "aeneas"
        except Exception as ex:
            logger.warning("[v2] align: aeneas failed (%s); falling back to heuristic", ex)

    # Heuristic refinement (always run; it also enforces no-overlap + snapping)
    for i, s in enumerate(segs):
        st = float(s.get("start", 0.0))
        en = float(s.get("end", 0.0))
        if en <= st:
            en = st + 0.5

        st2 = _snap(st, anchors, window_s=snap_window_s)
        en2 = en  # end snapping is useful too, but keep gentler to avoid chopping speech
        en2 = _snap(en2, anchors, window_s=snap_window_s)

        # Enforce reasonable reading speed for EN-ish text; keep within next start.
        text = str(s.get("text") or "").strip()
        words = _count_words(text)
        if words > 0:
            dur = max(0.1, en2 - st2)
            wpm = words / (dur / 60.0)
            if wpm > cfg.wpm_max:
                target = words / (cfg.wpm_max / 60.0)
                en2 = st2 + target
            elif wpm < cfg.wpm_min:
                target = words / (cfg.wpm_min / 60.0)
                en2 = st2 + target

        # Never overlap next line
        if i + 1 < len(segs):
            next_st = float(segs[i + 1].get("start", en2))
            en2 = min(en2, next_st - 0.01)

        # Ensure minimum positive duration
        if en2 <= st2:
            en2 = (
                min(st2 + 0.3, float(segs[i + 1].get("start", st2 + 0.3)) - 0.01)
                if i + 1 < len(segs)
                else st2 + 0.3
            )

        s["start"] = max(0.0, float(st2))
        s["end"] = max(float(s["start"]) + 0.05, float(en2))
        s.setdefault("aligned_by", used if used != "heuristic" else "heuristic")

    # Final pass: enforce monotonic starts/ends and no overlaps
    prev_end = 0.0
    for i, s in enumerate(segs):
        st = max(prev_end, float(s.get("start", 0.0)))
        en = float(s.get("end", st + 0.3))
        if en <= st:
            en = st + 0.3
        if i + 1 < len(segs):
            en = min(en, float(segs[i + 1].get("start", en)) - 0.01)
        s["start"] = st
        s["end"] = max(st + 0.05, en)
        prev_end = s["end"]

    return segs
