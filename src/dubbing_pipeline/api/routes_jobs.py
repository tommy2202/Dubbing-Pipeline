"""
Backwards-compatible import path.

The jobs API router lives under `dubbing_pipeline.web.routes_jobs` (historical naming),
but ops/security tasks may refer to `dubbing_pipeline.api.routes_jobs`.
"""

from __future__ import annotations

from dubbing_pipeline.web.routes_jobs import router

__all__ = ["router"]
