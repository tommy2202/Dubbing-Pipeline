# Policy Enforcement

This project enforces access in layers. The policy module is the single
entrypoint for route-level checks and delegates to existing enforcement
modules (remote access, visibility, quotas, invites).

## Deep gate: ASGI remote access

Remote access is enforced in ASGI middleware. This blocks disallowed
clients before any route handlers run.

- Source: `dubbing_pipeline.api.remote_access`
- Middleware: `RemoteAccessASGIMiddleware`

This is the primary (deep) gate. Route dependencies may still call
`policy.dep_request_allowed()` for defense-in-depth.

## Policy gate: route dependencies + wrappers

Routes standardize access through policy helpers:

- `dep_user()` returns the authenticated user.
- `dep_invite_only()` enforces invite-only access.
- `dep_request_allowed()` re-checks remote access posture.
- `require_invite_member(user)` wraps current invite gating.
- `require_quota_for_upload(...)` / `require_quota_for_submit(...)`
  delegate to `QuotaEnforcer`.
- `audit_policy_event(...)` emits coarse audit events (no transcripts or tokens).

These functions **wrap** existing enforcement modules. They do not
re-implement policy logic.

## Resource gate: visibility checks

Use policy wrappers for resource visibility:

- `require_can_view_job(user, job)`
- `require_can_view_artifact(user, artifact, job)`
- `require_can_view_library_item(user, item)`

These delegate to `dubbing_pipeline.security.visibility`.

## Non-negotiable rule

Routes MUST NOT directly call visibility or quota modules.
Routes MUST call policy wrappers instead.
