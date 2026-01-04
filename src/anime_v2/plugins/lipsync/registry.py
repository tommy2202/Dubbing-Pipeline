from __future__ import annotations

from pathlib import Path

from anime_v2.plugins.lipsync.base import LipSyncPlugin
from anime_v2.plugins.lipsync.wav2lip_plugin import get_wav2lip_plugin


def resolve_lipsync_plugin(
    name: str,
    *,
    wav2lip_dir: Path | None = None,
    wav2lip_checkpoint: Path | None = None,
) -> LipSyncPlugin | None:
    n = str(name or "off").strip().lower()
    if n in {"off", "none", ""}:
        return None
    if n == "wav2lip":
        return get_wav2lip_plugin(wav2lip_dir=wav2lip_dir, wav2lip_checkpoint=wav2lip_checkpoint)
    return None

