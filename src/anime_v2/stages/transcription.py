from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path

from anime_v2.cache.store import cache_get, cache_put, make_key
from anime_v2.config import get_settings
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.runtime.model_manager import ModelManager
from anime_v2.utils.circuit import Circuit
from anime_v2.utils.log import logger
from anime_v2.utils.net import egress_guard
from anime_v2.utils.retry import retry_call
from anime_v2.utils.time import format_srt_timestamp


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
    job_id: str | None = None,
    audio_hash: str | None = None,
    word_timestamps: bool | None = None,
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
        raise ValueError(
            "Whisper 'translate' pathway outputs English only; set --tgt-lang en or use --no-translate."
        )

    lang_opt: str | None = None if src_lang.lower() == "auto" else src_lang

    logger.info(
        "[v2] Whisper transcribe: model=%s device=%s task=%s src_lang=%s tgt_lang=%s",
        model_name,
        device,
        task,
        src_lang,
        tgt_lang,
    )

    t0 = time.perf_counter()
    s = get_settings()
    cb = Circuit.get("whisper")

    def _fallback_chain(first: str) -> list[str]:
        order = ["large-v3", "medium", "small"]
        first = str(first)
        out = [first]
        for m in order:
            if m not in out:
                out.append(m)
        return out

    ckpt_path = srt_out.parent / ".checkpoint.json"
    if job_id:
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        meta_path = srt_out.with_suffix(".json")
        if srt_out.exists() and meta_path.exists() and stage_is_done(ckpt, "transcribe"):
            logger.info("[v2] transcribe stage checkpoint hit")
            return srt_out

    try:
        import whisper  # type: ignore  # noqa: F401
    except Exception as ex:  # pragma: no cover
        # Degraded mode: still produce an empty SRT + metadata so the pipeline can
        # persist artifacts even when Whisper isn't installed in the environment.
        #
        # NOTE: We intentionally do *not* hard-fail here because some environments
        # run pipeline wiring/tests without optional heavy ML deps installed.
        logger.warning("[v2] Whisper not installed (%s). Writing degraded SRT/metadata.", ex)
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
        if job_id:
            with suppress(Exception):
                write_ckpt(
                    job_id,
                    "transcribe",
                    {"srt": srt_out, "meta": meta_path},
                    {
                        "work_dir": str(srt_out.parent),
                        "model": model_name,
                        "device": device,
                        "task": task,
                    },
                    ckpt_path=ckpt_path,
                )
        return srt_out

    attempts: list[dict] = []
    chosen_model = None
    chosen_device = device
    breaker_state = cb.snapshot().state

    # Cross-job cache: if audio_hash provided, try reuse artifacts first.
    if audio_hash:
        key = make_key(
            "transcribe",
            {
                "audio": audio_hash,
                "model": model_name,
                "task": task,
                "src": src_lang,
                "tgt": tgt_lang,
            },
        )
        hit = cache_get(key)
        if hit:
            paths = hit.get("paths", {})
            with suppress(Exception):
                src_srt = Path(str(paths.get("srt")))
                src_meta = Path(str(paths.get("meta")))
                if src_srt.exists() and src_meta.exists():
                    srt_out.parent.mkdir(parents=True, exist_ok=True)
                    srt_out.write_bytes(src_srt.read_bytes())
                    srt_out.with_suffix(".json").write_bytes(src_meta.read_bytes())
                    logger.info("[v2] transcribe cache hit", key=key)
                    return srt_out

    for cand_model in _fallback_chain(model_name):
        # circuit open => degrade to CPU for this attempt (and skip if even CPU is blocked by circuit)
        breaker_state = cb.snapshot().state
        cand_device = chosen_device
        if not cb.allow():
            breaker_state = cb.snapshot().state
            if cand_device == "cuda":
                cand_device = "cpu"
            else:
                # already cpu and breaker open => try next model (or ultimately fail)
                attempts.append(
                    {
                        "model": cand_model,
                        "device": cand_device,
                        "skipped": True,
                        "reason": f"breaker_{breaker_state}",
                    }
                )
                continue

        def _do_once(*, cand_model=cand_model, cand_device=cand_device):
            with egress_guard():
                mm = ModelManager.instance()
                with mm.acquire_whisper(cand_model, cand_device) as model:
                    kw = {
                        "task": task,
                        "language": lang_opt,
                        "verbose": False,
                    }
                    # Word-level timestamps are optional and model-dependent; try only when requested.
                    want_words = (
                        bool(word_timestamps)
                        if word_timestamps is not None
                        else bool(get_settings().whisper_word_timestamps)
                    )
                    if want_words:
                        try:
                            return model.transcribe(str(audio_path), **kw, word_timestamps=True)
                        except TypeError:
                            # Older whisper implementations may not support this flag.
                            pass
                    return model.transcribe(str(audio_path), **kw)

        tries = {"n": 0}

        def _on_retry(n, delay, ex, *, tries=tries, cand_model=cand_model, cand_device=cand_device):
            tries["n"] = n
            logger.warning(
                "whisper_retry",
                model=cand_model,
                device=cand_device,
                attempt=n,
                delay_s=delay,
                error=str(ex),
            )

        try:
            result = retry_call(
                _do_once,
                retries=s.retry_max,
                base=s.retry_base_sec,
                cap=s.retry_cap_sec,
                jitter=True,
                on_retry=_on_retry,
            )
            cb.mark_success()
            chosen_model = cand_model
            chosen_device = cand_device
            attempts.append(
                {"model": cand_model, "device": cand_device, "ok": True, "retries": tries["n"]}
            )
            break
        except Exception as ex:
            cb.mark_failure()
            attempts.append(
                {"model": cand_model, "device": cand_device, "ok": False, "error": str(ex)}
            )
            logger.warning(
                "whisper_failed",
                model=cand_model,
                device=cand_device,
                error=str(ex),
                breaker=cb.snapshot().state,
            )
            continue

    if chosen_model is None:
        raise RuntimeError(f"whisper failed after retries/fallbacks; breaker={cb.snapshot().state}")

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
            rec = {
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
                "text": (s.get("text") or "").strip(),
                "avg_logprob": s.get("avg_logprob"),
                "no_speech_prob": s.get("no_speech_prob"),
            }
            # If available, include word-level timing (never required).
            words = s.get("words")
            if isinstance(words, list):
                rec_words = []
                for w in words:
                    if not isinstance(w, dict):
                        continue
                    with suppress(Exception):
                        rec_words.append(
                            {
                                "start": float(w.get("start", 0.0)),
                                "end": float(w.get("end", 0.0)),
                                "word": str(w.get("word") or "").strip(),
                                "prob": (
                                    w.get("probability") if "probability" in w else w.get("prob")
                                ),
                            }
                        )
                if rec_words:
                    rec["words"] = rec_words
            seg_details.append(rec)
        except Exception:
            continue

    meta = {
        "model_name": chosen_model or model_name,
        "device": chosen_device,
        "task": task,
        "requested_src_lang": src_lang,
        "detected_language": detected_lang,
        "tgt_lang": tgt_lang,
        "segments": len(segments),
        "segments_detail": seg_details,
        "audio_duration_s": audio_duration_s,
        "wall_time_s": time.perf_counter() - t0,
        "attempts": attempts,
        "fallback_used": (chosen_model != model_name) if chosen_model else True,
        "breaker_state": cb.snapshot().state,
    }
    meta_path = srt_out.with_suffix(".json")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    if audio_hash:
        with suppress(Exception):
            key = make_key(
                "transcribe",
                {
                    "audio": audio_hash,
                    "model": chosen_model or model_name,
                    "task": task,
                    "src": src_lang,
                    "tgt": tgt_lang,
                },
            )
            cache_put(key, {"srt": srt_out, "meta": meta_path}, meta={"created_at": time.time()})

    if job_id:
        with suppress(Exception):
            write_ckpt(
                job_id,
                "transcribe",
                {"srt": srt_out, "meta": meta_path},
                {
                    "work_dir": str(srt_out.parent),
                    "model": model_name,
                    "device": device,
                    "task": task,
                },
                ckpt_path=ckpt_path,
            )

    logger.info("[v2] Wrote SRT → %s", srt_out)
    logger.info("[v2] Wrote metadata → %s", meta_path)
    return srt_out


# Backwards-compatible stage entrypoint name (if other code calls it)
def run(wav: Path, ckpt_dir: Path, **kwargs) -> Path:  # pragma: no cover
    srt_out = ckpt_dir / f"{wav.stem}.srt"
    return transcribe(audio_path=wav, srt_out=srt_out, **kwargs)
