"""
Deprecated legacy web app entrypoint.

This module used to provide an alternate FastAPI app with:
- wildcard CORS
- legacy token-in-URL/cookie auth

To avoid conflicts and to ensure one canonical, hardened implementation, this file now
re-exports the canonical server app from `anime_v2.server`.
"""

from anime_v2.server import app  # noqa: F401
