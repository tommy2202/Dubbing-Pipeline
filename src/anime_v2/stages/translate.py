from __future__ import annotations

from pathlib import Path

from anime_v2.utils.log import logger


def run(transcript_json: Path, ckpt_dir: Path, src_lang: str | None = None, tgt_lang: str = "en", **_) -> Path:
    """
    Translation stage.

    Stub for pipeline-v2; replace with actual implementation.
    """
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    out = ckpt_dir / "transcript_translated.json"
    logger.info(
        "[v2] translate.run(transcript=%s, src=%s, tgt=%s) -> %s",
        transcript_json,
        src_lang,
        tgt_lang,
        out,
    )
    return out

