import pathlib
import json
from typing import Optional
from anime_v1.utils import logger, checkpoints


def _try_load_mt(src_lang: Optional[str], tgt_lang: str):
    """Best-effort load of a translation pipeline.

    Priority:
    - M2M100 418M (smaller, decent quality)
    - MarianMT for specific pairs (Helsinki-NLP opus-mt)
    Returns (tokenizer, model, preprocess_fn) or (None, None, None) if unavailable.
    """
    try:
        from transformers import M2M100ForConditionalGeneration, M2M100Tokenizer
        model_name = "facebook/m2m100_418M"
        tok = M2M100Tokenizer.from_pretrained(model_name)
        mdl = M2M100ForConditionalGeneration.from_pretrained(model_name)

        def _prep(texts: list[str]):
            # Set forced target language
            tok.src_lang = (src_lang or "auto")
            return tok(texts, return_tensors="pt", padding=True)

        return (tok, mdl, _prep)
    except Exception as ex:
        logger.info("M2M100 not available (%s), trying MarianMT…", ex)

    # MarianMT fallback: try pair-specific model if tgt is English
    try:
        from transformers import MarianMTModel, MarianTokenizer
        pair = None
        if tgt_lang == "en" and src_lang:
            pair = f"Helsinki-NLP/opus-mt-{src_lang}-en"
        if pair is None:
            # generic multilingual to English model as last resort
            pair = "Helsinki-NLP/opus-mt-mul-en"
        tok = MarianTokenizer.from_pretrained(pair)
        mdl = MarianMTModel.from_pretrained(pair)

        def _prep(texts: list[str]):
            return tok(texts, return_tensors="pt", padding=True)

        return (tok, mdl, _prep)
    except Exception as ex:
        logger.warning("No offline MT model available (%s)", ex)
        return (None, None, None)


def _translate_texts(texts: list[str], src_lang: Optional[str], tgt_lang: str) -> list[str]:
    tok, mdl, prep = _try_load_mt(src_lang, tgt_lang)
    if tok is None:
        # No model: return source texts unchanged
        return texts
    try:
        inputs = prep(texts)
        if tok.__class__.__name__.startswith("M2M100"):
            # M2M requires forced_bos_token_id to control target
            forced_id = tok.get_lang_id(tgt_lang)
            gen = mdl.generate(**inputs, forced_bos_token_id=forced_id, max_new_tokens=200)
            out = tok.batch_decode(gen, skip_special_tokens=True)
            return out
        else:
            gen = mdl.generate(**inputs, max_new_tokens=200)
            out = tok.batch_decode(gen, skip_special_tokens=True)
            return out
    except Exception as ex:
        logger.warning("MT generation failed (%s); using original text.", ex)
        return texts


def run(transcript_json: pathlib.Path, ckpt_dir: pathlib.Path, *, src_lang: Optional[str], tgt_lang: str, enabled: bool) -> pathlib.Path:
    """Translate transcript segments from src_lang → tgt_lang.

    If enabled is False, returns the input unchanged.
    """
    if not enabled:
        return transcript_json

    data = json.loads(transcript_json.read_text())
    segments = data.get("segments", [])
    if not segments:
        logger.info("Translation: no segments; skip.")
        return transcript_json

    src_texts = [seg.get("text", "") for seg in segments]
    # If tgt_lang is English but ASR already produced English, skip
    if tgt_lang == "en" and all(t.strip() == "" or all(ord(c) < 128 for c in t) for t in src_texts):
        logger.info("Translation appears unnecessary (already English); skipping.")
        return transcript_json

    tgt_texts = _translate_texts(src_texts, src_lang, tgt_lang)
    for seg, new_txt in zip(segments, tgt_texts):
        seg["text"] = new_txt

    out = ckpt_dir / "transcript.translated.json"
    checkpoints.save(data, out)
    logger.info("Wrote translated transcript → %s", out)
    return out
