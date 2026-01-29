# Trusted Shared Library Policy

This document describes the enforcement model for invite-only access, private-by-default
jobs, and controlled sharing inside the Trusted Shared Library.

## 1) Invite-only access

- Public self-signups are disabled (`/auth/register`, `/auth/signup`).
- New users are created only via invite redemption (`/api/invites/redeem`).
- Admins manage invites (`/api/admin/invites`).

## 2) Private-by-default jobs

- New jobs default to **private** unless explicitly set to `shared`.
- Private jobs and artifacts are visible only to the owner or admins.
- Shared jobs are visible to other authenticated users *only* when explicitly shared.

### Share toggle semantics

- Visibility is `private` or `shared` (legacy `public` is treated as `shared`).
- Sharing can be disabled globally with:
  - `ALLOW_SHARED_LIBRARY=0`
- Admins can always set visibility; non-admins are blocked when sharing is disabled.

## 3) Quotas and throttles

The system enforces per-user quotas to prevent abuse and resource exhaustion:

- **Max upload bytes** (per upload)
- **Max storage bytes** (per user)
- **Jobs per day** (per user)
- **Max concurrent jobs** (per user)
- **Daily processing minutes** (per user)

When a quota is exceeded, the API responds with **HTTP 429** and a structured error:

```json
{
  "detail": {
    "error": "quota_exceeded",
    "action": "jobs.submit",
    "reason": "concurrent_jobs_limit",
    "limit": 1,
    "current": 1
  }
}
```

## 4) Reporting + admin quick-remove

- **Owner unshare**: removes the item from the shared index (sets visibility to private).
- **Admin remove**: can unshare any item immediately.
- **Report**: users can submit a short report on shared items.

When configured, admin alerts are sent via **ntfy** (private/self-hosted):

- `NTFY_NOTIFY_ADMIN=1`
- `NTFY_ADMIN_TOPIC=<topic>`

If ntfy is not configured, reports are queued for review in `/ui/admin/reports`.

## 5) Logging policy

- **No transcript content** is logged by default (`LOG_TRANSCRIPTS=0`).
- Authentication tokens, cookies, and JWTs are **redacted** in logs.
- Audit events include IDs and counts only (no content payloads).
