from __future__ import annotations

"""
Backwards-compatible import path.

The jobs API router lives under `anime_v2.web.routes_jobs` (historical naming),
but ops/security tasks may refer to `anime_v2.api.routes_jobs`.
"""

from anime_v2.web.routes_jobs import router  # noqa: F401

