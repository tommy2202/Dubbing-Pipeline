from __future__ import annotations

from typing import Any

from anime_v2.utils.config import get_settings
from anime_v2.utils.log import logger


_TERMINAL_PUNCT = set(".!?…。！？")


def _detect_lang(text: str) -> str | None:
    try:
        from langdetect import detect  # type: ignore

        return detect(text)
    except Exception as ex:
        logger.warning("[v2] langdetect unavailable/failed (%s)", ex)
        return None


def _ensure_terminal_punct(src: str, translated: str) -> str:
    src = src.strip()
    translated = translated.strip()
    if not src or not translated:
        return translated
    last = src[-1]
    if last in _TERMINAL_PUNCT and translated[-1] not in _TERMINAL_PUNCT:
        return translated + last
    return translated


def _try_make_pipeline(model_name: str, *, cache_dir=None):
    try:
        from transformers import pipeline  # type: ignore

        return pipeline("translation", model=model_name, device=-1, cache_dir=cache_dir)
    except Exception as ex:
        raise RuntimeError(f"Failed to load translation pipeline for {model_name}: {ex}") from ex


def _nllb_lang(lang: str) -> str:
    # Minimal mapping for common languages; extend as needed.
    m = {
        "en": "eng_Latn",
        "ja": "jpn_Jpan",
        "fr": "fra_Latn",
        "de": "deu_Latn",
        "es": "spa_Latn",
        "it": "ita_Latn",
        "pt": "por_Latn",
        "ru": "rus_Cyrl",
        "ko": "kor_Hang",
        "zh": "zho_Hans",
    }
    return m.get(lang, lang)


def _translate_with_nllb(texts: list[str], src: str, tgt: str, *, cache_dir=None) -> list[str]:
    """
    NLLB requires forced language tokens.
    """
    try:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, pipeline  # type: ignore
    except Exception as ex:
        raise RuntimeError(f"transformers not installed for NLLB: {ex}") from ex

    model_name = "facebook/nllb-200-distilled-600M"
    src_code = _nllb_lang(src)
    tgt_code = _nllb_lang(tgt)

    tok = AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name, cache_dir=cache_dir)
    tok.src_lang = src_code
    forced_bos = tok.convert_tokens_to_ids(tgt_code)
    if forced_bos is None:
        raise RuntimeError(f"Unsupported NLLB tgt lang code: {tgt_code}")

    trans = pipeline("translation", model=model, tokenizer=tok, device=-1)
    outs = trans(texts, generate_kwargs={"forced_bos_token_id": forced_bos})
    return [o.get("translation_text", "") for o in outs]


def translate_lines(lines: list[dict[str, Any]], src_lang: str, tgt_lang: str) -> list[dict[str, Any]]:
    """
    Translate `lines` while preserving segmentation.

    lines: [{start,end,speaker_id,text}]
    Returns same shape with `text` translated (best-effort).
    """
    if not lines:
        return []

    # Detect source lang if needed
    effective_src = src_lang
    if src_lang.lower() == "auto":
        probe = next((str(l.get("text", "")).strip() for l in lines if str(l.get("text", "")).strip()), "")
        detected = _detect_lang(probe) if probe else None
        effective_src = detected or "auto"
        logger.info("[v2] translate: detected src_lang=%s", effective_src)

    if effective_src.lower() == tgt_lang.lower():
        logger.info("[v2] translate: src==tgt (%s); skipping translation", effective_src)
        return lines

    settings = get_settings()
    cache_dir = str(settings.transformers_cache) if settings.transformers_cache else None

    # Collect texts and translate in batches
    texts = [str(l.get("text", "") or "") for l in lines]

    model_override = settings.translation_model
    if model_override:
        logger.info("[v2] translate: using TRANSLATION_MODEL=%s", model_override)
        try:
            pipe = _try_make_pipeline(model_override, cache_dir=cache_dir)
            out_texts = [o.get("translation_text", "") for o in pipe(texts)]
        except Exception as ex:
            logger.warning("[v2] translate: model override failed (%s); returning original text", ex)
            return lines
    else:
        # Try Marian first
        if effective_src.lower() == "auto":
            logger.warning("[v2] translate: src_lang=auto but detection failed; returning original text")
            return lines

        marian = f"Helsinki-NLP/opus-mt-{effective_src}-{tgt_lang}"
        try:
            logger.info("[v2] translate: trying Marian model %s", marian)
            pipe = _try_make_pipeline(marian, cache_dir=cache_dir)
            out_texts = [o.get("translation_text", "") for o in pipe(texts)]
        except Exception as ex:
            logger.warning("[v2] translate: Marian unavailable (%s). Falling back to NLLB.", ex)
            try:
                out_texts = _translate_with_nllb(texts, effective_src, tgt_lang, cache_dir=cache_dir)
            except Exception as ex2:
                logger.warning("[v2] translate: NLLB failed (%s); returning original text", ex2)
                return lines

    # Preserve terminal punctuation if dropped
    new_lines: list[dict[str, Any]] = []
    for l, t_src, t_out in zip(lines, texts, out_texts):
        new_l = dict(l)
        new_l["text"] = _ensure_terminal_punct(t_src, t_out)
        new_lines.append(new_l)
    return new_lines
