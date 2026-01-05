"""
Canonical authentication helpers (v2).

The server wires auth via:
- JWT access tokens (HS256)
- rotating refresh tokens stored server-side (AuthStore)
- optional signed session cookie for browser UI
- CSRF double-submit for cookie flows
"""

from __future__ import annotations

from .refresh_tokens import (
    RefreshTokenError,
    issue_and_store_refresh_token,
    revoke_refresh_token_best_effort,
    rotate_refresh_token,
)

__all__ = [
    "RefreshTokenError",
    "issue_and_store_refresh_token",
    "rotate_refresh_token",
    "revoke_refresh_token_best_effort",
]
