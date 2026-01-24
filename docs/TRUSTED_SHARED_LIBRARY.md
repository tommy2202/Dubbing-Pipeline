# Trusted Shared Library

The shared library is intended for **trusted, internal sharing** of completed jobs.
It is **not** a public gallery.

## Usage rules
- **Default is private**: jobs are private unless explicitly shared.
- **Share intentionally**: only share outputs you are comfortable with other authenticated users seeing.
- **Avoid sensitive content**: keep personal or restricted content private unless explicitly approved.

## Private vs Shared
- **Private**: owner + admins only.
- **Shared**: visible to other authenticated users in the library.

You can toggle visibility per job in the UI or via:
```
POST /api/jobs/{id}/visibility
```

## Moderation & reporting
- **Owner unshare**: remove your own shared item (sets it to private).
- **Admin remove**: immediately unshare and remove from the shared index.
- **Report**: users can report shared items with a short reason.
  - Alerts go to admin via ntfy if configured.
  - If ntfy is not configured, reports are queued for admin review.

Admin reports UI:
- `/ui/admin/reports`

## Access model
Shared items are still protected:
- Users must be authenticated.
- Access is restricted to shared items (or owner/admin).
- Remote access should be **Tailscale-first**; tunnel mode must be protected by Cloudflare Access allowlist (Policy A).

## Privacy & logging
- Logs redact tokens and cookies.
- Transcript text is not logged by default.
- Audit logs are coarse and do not store content.
