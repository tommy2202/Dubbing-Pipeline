# P0 Logging Policy: No Content Logging + Coarse Audit

## Summary

This service is configured to avoid logging sensitive content by default. Logs focus on
coarse operational signals (job IDs, user IDs/roles, stages, counts, durations) and
explicitly redact secrets and tokens.

## What is logged

- Request metadata: method, path (no query), status, duration, request_id, user_id.
- High-level pipeline progress: stage transitions, job IDs, counts, durations.
- Coarse audit events: actor_id, resource_id/job_id, outcome, and safe metadata
  (counts, sizes, durations, IDs).
- Remote-access denials (mode + reason only; no headers/tokens).

## What is never logged

- Cookies, Authorization headers, JWTs, API keys, or CSRF tokens.
- Upload payloads or raw request bodies.
- Transcript/subtitle text (unless explicitly enabled for debugging).
- Secret config values (only SET/UNSET markers are reported).

Redaction uses the placeholder `***REDACTED***` in JSON logs.

## Transcript storage defaults

- Transcript persistence is **opt-in**: `NO_STORE_TRANSCRIPT=1` by default.
- When disabled, transcripts remain only in the job workspace and are not emitted to logs.

## Coarse audit helper

- `audit.audit_event(type, actor_id, target_id, outcome, metadata_safe)` writes a
  scrubbed, non-content audit record.

## Debugging safely

If you must enable more verbose output:

1. Set `LOG_LEVEL=DEBUG` **only** in non-production environments.
2. (Optional) Set `LOG_TRANSCRIPTS=1` to allow transcript text in logs.
   - Do this only on secured, local machines and rotate logs aggressively.
3. Store logs in a secure location via `DUBBING_LOG_DIR`.
4. Never enable debug logging or transcript logging on public/remote deployments.
