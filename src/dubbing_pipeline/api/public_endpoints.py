from __future__ import annotations

# Public or internally-authenticated API endpoints (no require_scope/require_role dependency).
# Keep this list in sync with routes_auth when those handlers manage auth inline.
PUBLIC_API_ALLOWLIST = {
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/register",
    "/api/auth/signup",
    "/api/auth/qr/redeem",
    "/api/auth/totp/setup",
    "/api/auth/totp/verify",
    "/api/auth/sessions",
    "/api/auth/sessions/{device_id}/revoke",
    "/api/auth/sessions/revoke_all",
    "/api/invites/redeem",
}
