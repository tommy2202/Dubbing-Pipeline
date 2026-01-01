from __future__ import annotations

from anime_v2.config import get_settings
from anime_v2.utils.log import logger

_coqui_logged = False


def require_coqui_tos() -> None:
    """
    Explicit license gating for Coqui XTTS (CPML).
    Refuse to initialize unless COQUI_TOS_AGREED=1.
    """
    global _coqui_logged
    s = get_settings()
    if not bool(s.coqui_tos_agreed):
        raise RuntimeError("COQUI_TOS_AGREED must be set to 1 to use Coqui TTS.")
    if not _coqui_logged:
        logger.info("Coqui TTS license acknowledged via COQUI_TOS_AGREED=1")
        _coqui_logged = True
