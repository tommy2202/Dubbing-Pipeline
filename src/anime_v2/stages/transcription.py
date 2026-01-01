from __future__ import annotations

import json
import time
from pathlib import Path

from anime_v2.utils.log import logger
from anime_v2.utils.time import format_srt_timestamp
from anime_v2.utils.net import egress_guard
from anime_v2.runtime.model_manager import ModelManager


def _write_srt(segments: list[dict], srt_path: Path) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = format_srt_timestamp(float(seg["start"]))
            end = format_srt_timestamp(float(seg["end"]))
            text = (seg.get("text") or "").strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def transcribe(
    audio_path: Path,
    srt_out: Path,
    device: str,
    model_name: str,
    task: str,
    src_lang: str,
    *,
    tgt_lang: str = "en",
) -> Path:
    """
    Whisper transcription/translation producing SRT and JSON metadata next to it.

    - task:
        - "translate": Whisper translate pathway (outputs English). If src_lang != "auto", pass it.
        - "transcribe": plain transcription, in src_lang (or autodetect if src_lang="auto")
    """
    task = task.lower().strip()
    if task not in {"translate", "transcribe"}:
        raise ValueError(f"task must be translate|transcribe, got {task!r}")

    if task == "translate" and tgt_lang.lower() != "en":
        raise ValueError("Whisper 'translate' pathway outputs English only; set --tgt-lang en or use --no-translate.")

    lang_opt: str | None
    if src_lang.lower() == "auto":
        lang_opt = None
    else:
        lang_opt = src_lang

    logger.info(
        "[v2] Whisper transcribe: model=%s device=%s task=%s src_lang=%s tgt_lang=%s",
        model_name,
        device,
        task,
        src_lang,
        tgt_lang,
    )

    t0 = time.perf_counter()

    try:
        import whisper  # type: ignore
    except Exception as ex:  # pragma: no cover
        # Degraded mode: still produce an empty SRT + metadata so the pipeline can
        # persist artifacts even when Whisper isn't installed in the environment.
        logger.warning(
            "[v2] Whisper not installed (%s). Writing placeholder SRT/metadata.",
            ex,
        )
        srt_out.parent.mkdir(parents=True, exist_ok=True)
        srt_out.write_text("", encoding="utf-8")
        meta = {
            "model_name": model_name,
            "device": device,
            "task": task,
            "requested_src_lang": src_lang,
            "detected_language": None,
            "tgt_lang": tgt_lang,
            "segments": 0,
            "audio_duration_s": None,
            "wall_time_s": time.perf_counter() - t0,
            "error": "whisper_not_installed",
        }
        meta_path = srt_out.with_suffix(".json")
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        return srt_out

    with egress_guard():
        mm = ModelManager.instance()
        with mm.acquire_whisper(model_name, device) as model:
            result = model.transcribe(
                str(audio_path),
                task=task,
                language=lang_opt,
                verbose=False,
            )

    segments = list(result.get("segments") or [])
    _write_srt(segments, srt_out)

    # Persist JSON metadata next to the SRT
    detected_lang = result.get("language")
    audio_duration_s = 0.0
    if segments:
        try:
            audio_duration_s = float(max(float(s.get("end", 0.0)) for s in segments))
        except Exception:
            audio_duration_s = 0.0

    seg_details = []
    for s in segments:
        try:
            seg_details.append(
                {
                    "start": float(s.get("start", 0.0)),
                    "end": float(s.get("end", 0.0)),
                    "text": (s.get("text") or "").strip(),
                    "avg_logprob": s.get("avg_logprob"),
                    "no_speech_prob": s.get("no_speech_prob"),
                }
            )
        except Exception:
            continue

    meta = {
        "model_name": model_name,
        "device": device,
        "task": task,
        "requested_src_lang": src_lang,
        "detected_language": detected_lang,
        "tgt_lang": tgt_lang,
        "segments": len(segments),
        "segments_detail": seg_details,
        "audio_duration_s": audio_duration_s,
        "wall_time_s": time.perf_counter() - t0,
    }
    meta_path = srt_out.with_suffix(".json")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    logger.info("[v2] Wrote SRT → %s", srt_out)
    logger.info("[v2] Wrote metadata → %s", meta_path)
    return srt_out


# Backwards-compatible stage entrypoint name (if other code calls it)
def run(wav: Path, ckpt_dir: Path, **kwargs) -> Path:  # pragma: no cover
    srt_out = ckpt_dir / f"{wav.stem}.srt"
    return transcribe(audio_path=wav, srt_out=srt_out, **kwargs)

