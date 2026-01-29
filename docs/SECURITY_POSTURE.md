# Security Posture (Defense in Depth)

This system is designed for **trusted, internal sharing** (friends/team). It is **not**
intended for public, anonymous access. The posture assumes a controlled environment
with explicit user invitations and private network access.

## Defense in depth (three layers)

### Deep: Remote access enforcement (ASGI middleware)

The **outermost layer** is enforced at the ASGI middleware level:

- `RemoteAccessASGIMiddleware` evaluates the access mode (off/tailscale/cloudflare).
- In **tailscale** mode, only Tailscale CGNAT + localhost are allowed by default.
- In **cloudflare** mode, a valid Cloudflare Access JWT is required.

This layer blocks requests before they reach any route handlers.

### Mid: Policy enforcement dependency layer

Sensitive routers attach policy dependencies:

- `policy.require_request_allowed` (defense in depth)
- `policy.require_invite_member` (invite-only access)

This prevents accidental “forgot to enforce policy” mistakes when adding new routes.

### Per-resource: Visibility + ownership checks

Per-resource checks ensure that even authenticated users cannot access
private artifacts they do not own:

- Jobs, files, and library items are guarded by centralized visibility helpers.
- Shared items are visible only when explicitly set to `shared`.

This layer protects against guessing IDs or paths for private resources.

## Threat model (summary)

- **Trusted users only**: accounts are invite-based and intended for friends/team.
- **No public exposure**: remote access should be via Tailscale or Cloudflare Access.
- **Defense in depth**: middleware + policy dependencies + resource-level checks.
- **Privacy defaults**: private-by-default content and redacted logging.

If your deployment requires stronger isolation (multi-tenant, public access),
do not expose this server directly; place it behind a hardened identity-aware
proxy and review the policy controls carefully.
