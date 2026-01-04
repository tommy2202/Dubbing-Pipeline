from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.log import logger
from anime_v2.utils.net import egress_guard


@dataclass(frozen=True, slots=True)
class TranslationConfig:
    mt_engine: str = "auto"  # auto|whisper|marian|nllb
    mt_lowconf_thresh: float = -0.45
    glossary_path: str | None = None
    style_path: str | None = None
    show_id: str | None = None
    whisper_model: str = field(default_factory=lambda: str(get_settings().whisper_model))
    audio_path: str | None = None
    device: str = "cpu"
    # Streaming context bridging: best-effort prompt/hint for providers that support it.
    # This MUST NOT contain secrets; it is derived from prior segment text.
    context_hint: str | None = None


def _read_glossary(path: str | None, *, show_id: str | None = None) -> list[tuple[str, str]]:
    if not path:
        return []
    p = Path(path)
    if p.is_dir():
        # support per-show directory layouts
        candidates = []
        if show_id:
            candidates.append(p / f"{show_id}.tsv")
            candidates.append(p / show_id / "glossary.tsv")
        candidates.append(p / "glossary.tsv")
        for c in candidates:
            if c.exists():
                p = c
                break
    if not p.exists():
        return []
    out: list[tuple[str, str]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        out.append((parts[0].strip(), parts[1].strip()))
    return [(a, b) for a, b in out if a and b]


def _read_style(path: str | None, *, show_id: str | None = None) -> dict[str, Any]:
    """
    Lightweight style loader:
      - If PyYAML is available, parse YAML
      - Else parse a trivial key: value format
    """
    if not path:
        return {}
    p = Path(path)
    if p.is_dir():
        candidates = []
        if show_id:
            candidates.append(p / f"{show_id}.yaml")
            candidates.append(p / show_id / "style.yaml")
        candidates.append(p / "style.yaml")
        for c in candidates:
            if c.exists():
                p = c
                break
    if not p.exists():
        return {}
    raw = p.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        out: dict[str, Any] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        return out


_HONORIFIC_RE = re.compile(
    r"\b([A-Za-z][A-Za-z']*)[- ]?(san|chan|sama|kun|sensei)\b", re.IGNORECASE
)


def _apply_style(text: str, style: dict[str, Any]) -> str:
    honorific = str(
        style.get("honorifics", "") or style.get("honorific_policy", "") or "keep"
    ).lower()
    profanity = str(style.get("profanity", "allow")).lower()

    out = text
    if honorific == "drop":
        out = _HONORIFIC_RE.sub(r"\1", out)

    if profanity == "mask":
        # Very small default list; allow overriding with style.profanity_words
        words = style.get("profanity_words") or style.get("profanity_list") or []
        if isinstance(words, str):
            words = [w.strip() for w in words.split(",") if w.strip()]
        if not isinstance(words, list):
            words = []
        if not words:
            words = ["fuck", "shit", "bitch", "asshole"]
        for w in words:
            if not w:
                continue
            out = re.sub(rf"(?i)\b{re.escape(w)}\b", "****", out)

    return out


def _glossary_required_terms(
    src_text: str, glossary: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    req = []
    for src, tgt in glossary:
        if src and src in src_text:
            req.append((src, tgt))
    return req


def _glossary_respected(tgt_text: str, required: list[tuple[str, str]]) -> bool:
    low = tgt_text.lower()
    return all(tgt.lower() in low for _, tgt in required)


def _glossary_inject(
    src_text: str, required: list[tuple[str, str]]
) -> tuple[str, list[dict[str, Any]]]:
    """
    Force glossary terms into MT input by substituting src term with desired tgt term.
    Returns modified src_text and annotations.
    """
    out = src_text
    ann = []
    for src, tgt in required:
        if src in out:
            out = out.replace(src, tgt)
            ann.append({"src": src, "tgt": tgt, "kind": "inject"})
    return out, ann


def _translate_marian(text: str, src_lang: str, tgt_lang: str) -> str:
    from anime_v2.stages.translate import _try_make_pipeline  # type: ignore
    from anime_v2.utils.config import get_settings

    settings = get_settings()
    cache_dir = str(settings.transformers_cache) if settings.transformers_cache else None
    model = f"Helsinki-NLP/opus-mt-{src_lang}-{tgt_lang}"
    with egress_guard():
        pipe = _try_make_pipeline(model, cache_dir=cache_dir)
    out = pipe([text])[0].get("translation_text", "")
    return str(out or "")


def _translate_nllb(text: str, src_lang: str, tgt_lang: str) -> str:
    from anime_v2.stages.translate import _translate_with_nllb  # type: ignore
    from anime_v2.utils.config import get_settings

    settings = get_settings()
    cache_dir = str(settings.transformers_cache) if settings.transformers_cache else None
    with egress_guard():
        out = _translate_with_nllb([text], src_lang, tgt_lang, cache_dir=cache_dir)[0]
    return str(out or "")


def _whisper_translate(
    audio_path: str, *, device: str, model_name: str, src_lang: str, context_hint: str | None = None
) -> list[dict]:
    try:
        import whisper  # type: ignore
    except Exception as ex:
        raise RuntimeError(f"whisper not installed: {ex}") from ex
    lang_opt = None if src_lang.lower() == "auto" else src_lang
    with egress_guard():
        model = whisper.load_model(model_name, device=device)
        # Whisper supports a lightweight prompt for improved coherence across windows.
        # (Used by streaming context bridging; offline-only.)
        kw: dict[str, Any] = {"task": "translate", "language": lang_opt, "verbose": False}
        if context_hint and str(context_hint).strip():
            kw["initial_prompt"] = str(context_hint).strip()
        res = model.transcribe(audio_path, **kw)
    segs = res.get("segments") or []
    out = []
    for s in segs:
        out.append(
            {
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
                "text": (s.get("text") or "").strip(),
                "avg_logprob": s.get("avg_logprob"),
            }
        )
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def translate_segments(
    segments: list[dict[str, Any]],
    src_lang: str,
    tgt_lang: str,
    cfg: TranslationConfig,
) -> list[dict[str, Any]]:
    """
    Inputs segments:
      {start,end,speaker,text,logprob?}
    Outputs enriched segments (preserving start/end/speaker):
      {start,end,speaker,text,engine,conf,glossary_ok,glossary_applied,lowconf,fallback_used}
    """
    if not segments:
        return []

    glossary = _read_glossary(cfg.glossary_path, show_id=cfg.show_id)
    style = _read_style(cfg.style_path, show_id=cfg.show_id)

    engine = (cfg.mt_engine or "auto").lower()
    if engine not in {"auto", "whisper", "marian", "nllb"}:
        engine = "auto"

    # Prepare baseline Whisper translate segments (single pass) when possible.
    whisper_segs: list[dict] = []
    whisper_ok = False
    if (engine in {"auto", "whisper"}) and tgt_lang.lower() == "en":
        try:
            if not cfg.audio_path:
                raise RuntimeError("audio_path not provided for whisper translate")
            whisper_segs = _whisper_translate(
                cfg.audio_path,
                device=cfg.device,
                model_name=cfg.whisper_model,
                src_lang=src_lang,
                context_hint=cfg.context_hint,
            )
            whisper_ok = True
        except Exception:
            whisper_ok = False

    out: list[dict[str, Any]] = []
    for seg in segments:
        start = float(seg["start"])
        end = float(seg["end"])
        speaker = str(seg.get("speaker") or seg.get("speaker_id") or "SPEAKER_01")
        src_text = str(seg.get("text") or "")
        src_lp = seg.get("logprob")
        try:
            src_lp = float(src_lp) if src_lp is not None else None
        except Exception:
            src_lp = None

        required = _glossary_required_terms(src_text, glossary)
        glossary_ann: list[dict[str, Any]] = []

        base_text = ""
        base_conf = src_lp
        base_engine = "none"

        # Choose base translation
        if engine in {"marian", "nllb"}:
            base_engine = engine
        elif engine in {"auto", "whisper"} and whisper_ok:
            base_engine = "whisper"
        else:
            base_engine = "marian" if src_lang.lower() != "auto" else "nllb"

        if base_engine == "whisper":
            # Align whisper translate by time overlap
            chunks = []
            confs = []
            for w in whisper_segs:
                ov = _overlap(start, end, float(w["start"]), float(w["end"]))
                if ov > 0.0:
                    chunks.append(str(w.get("text") or ""))
                    with suppress(Exception):
                        confs.append(float(w.get("avg_logprob")))
            base_text = " ".join([c.strip() for c in chunks if c.strip()]).strip()
            base_conf = (sum(confs) / len(confs)) if confs else base_conf
        else:
            # Direct MT (line-by-line)
            injected, glossary_ann = _glossary_inject(src_text, required)
            try:
                if base_engine == "marian":
                    base_text = _translate_marian(injected, src_lang, tgt_lang)
                else:
                    base_text = _translate_nllb(injected, src_lang, tgt_lang)
            except Exception as ex:
                logger.warning("MT base translation failed (%s)", ex)
                base_text = ""

        lowconf = base_conf is not None and float(base_conf) < float(cfg.mt_lowconf_thresh)
        glossary_ok = True if not required else _glossary_respected(base_text, required)

        final_text = base_text.strip()
        final_engine = base_engine
        fallback_used = False

        # Fallback for low confidence or glossary mismatch: use Marian/NLLB line-by-line.
        if (engine == "auto" and (not final_text or lowconf or not glossary_ok)) or engine in {
            "marian",
            "nllb",
        }:
            preferred = "marian" if engine == "auto" else engine
            injected, glossary_ann2 = _glossary_inject(src_text, required)
            glossary_ann.extend(glossary_ann2)
            try:
                if preferred == "marian":
                    final_text = _translate_marian(injected, src_lang, tgt_lang).strip()
                    final_engine = "marian"
                else:
                    final_text = _translate_nllb(injected, src_lang, tgt_lang).strip()
                    final_engine = "nllb"
                fallback_used = True
            except Exception as ex:
                logger.warning("Fallback MT failed (%s)", ex)
                # best-effort keep base
                final_text = final_text or src_text

        # Always keep something for non-empty source
        if src_text.strip() and not final_text:
            final_text = src_text

        final_text = _apply_style(final_text, style)

        # Re-check glossary after possible fallback/styling
        glossary_ok = True if not required else _glossary_respected(final_text, required)

        out.append(
            {
                "start": start,
                "end": end,
                "speaker": speaker,
                "src_text": src_text,
                "text": final_text,
                "engine": final_engine,
                "conf": base_conf,
                "lowconf": bool(lowconf),
                "glossary_ok": bool(glossary_ok),
                "glossary_applied": glossary_ann,
                "fallback_used": bool(fallback_used),
            }
        )

    return out
