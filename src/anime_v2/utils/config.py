"""
Backwards-compatible shim.

The canonical settings live in `anime_v2.config` (pydantic-settings).
Keep this module for existing imports across stages.
"""

from __future__ import annotations

from anime_v2.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
