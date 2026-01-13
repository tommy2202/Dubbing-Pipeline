"""
Backwards-compatible shim.

The canonical settings live in `dubbing_pipeline.config` (pydantic-settings).
Keep this module for existing imports across stages.
"""

from __future__ import annotations

from dubbing_pipeline.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
