# P0 security checklist

Run before inviting other users or opening remote access.

## One-command gate
- `python scripts/p0_gate.py`

## Manual sanity (spot-check)
- Login/logout works and CSRF blocks cross-site POSTs.
- Uploads reject traversal, bad chunk order, and oversize files.
- User A cannot access User B jobs/uploads/files (admin can).
- Audit log includes request_id + actor_id for sensitive actions.
- Shutdown is clean (no `CancelledError` noise in logs).

