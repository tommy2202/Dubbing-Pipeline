# Privacy & Data Handling

This document describes what the system stores by default, retention behavior, and how to remove data.
Defaults are **privacy-first** and designed for private deployments.

## Retention defaults
Retention is enabled and runs periodically to remove old artifacts and logs:

- `RETENTION_ENABLED=1`
- `RETENTION_UPLOAD_TTL_HOURS=24`
- `RETENTION_JOB_ARTIFACT_DAYS=14`
- `RETENTION_LOG_DAYS=14`
- `RETENTION_INTERVAL_SEC=3600`

Legacy knobs (still supported):
- `RETENTION_DAYS_INPUT` (default 7)
- `RETENTION_DAYS_LOGS` (default 14)

Cache policy defaults:
- `CACHE_POLICY=full` (keep artifacts)
- `RETENTION_DAYS=0` (time-based pruning disabled for cache policy)

## What is stored
By default the system stores:
- **Job metadata** in `jobs.db` (owner, status, timestamps, runtime flags)
- **Auth data** in `auth.db` (users, keys, refresh tokens)
- **Outputs** under `Output/<stem>/...` and `Output/jobs/<job_id>/...`
- **Library index** in the `job_library` table (series, season, episode, visibility)
- **Logs** under `LOG_DIR` (default: `logs/`)
- **Audit logs** in `LOG_DIR/audit-YYYYMMDD.log` and `LOG_DIR/audit.jsonl`
- **Manifests** under `Output/.../manifest.json` for playback links and metadata

Transcripts are stored as SRT/JSON outputs by default. You can disable transcript storage:
- Per job: `no_store_transcript=true`
- Global privacy mode: `PRIVACY_MODE=on`

## Logging policy (default)
- **No transcript text is logged** (`LOG_TRANSCRIPTS=0`)
- Tokens/cookies/JWTs are **redacted** in logs
- Audit logs are **coarse** (event type, actor, resource, timestamp, request id, outcome)

To enable transcript logging for debugging (not recommended on shared systems):

```bash
export LOG_TRANSCRIPTS=1
```

## How to delete data
1) **Delete a job** (removes artifacts + metadata):
   - UI: open job → Delete (admin)
   - API: `DELETE /api/jobs/{id}` (admin) or library delete for owners

2) **Unshare** (makes private, keeps artifacts):
   - UI: job visibility or library “Remove from shared”
   - API: `POST /api/library/{series:season:episode}/unshare`

3) **Retention cleanup**:
   - Runs automatically based on retention config.
   - You can reduce retention days or disable retention as needed.

## Optional privacy features
- **Encryption-at-rest**: `ENCRYPT_AT_REST=1` (for supported artifacts)
- **Minimal artifacts**: `PRIVACY_MODE=on`, `MINIMAL_ARTIFACTS=1`
- **No transcript storage**: `NO_STORE_TRANSCRIPT=1`

These options reduce stored intermediates but may affect review workflows.
## Privacy + data lifecycle

This doc summarizes what voice-related artifacts are stored and how to remove them.

---

## What is stored (voice-related)

- **Per-job speaker refs** (job-local):
  - `Output/<stem>/analysis/voice_refs/`
  - Used for cloning and voice mapping.
- **Series voice store** (persistent refs):
  - `VOICE_STORE` (default: `data/voices`)
  - Layout: `<series_slug>/characters/<character_slug>/ref.wav`
- **Voice memory** (optional, cross-episode identity):
  - `VOICE_MEMORY_DIR` (default: `data/voice_memory`)

---

## How to delete voice memory (wipe)

1) Stop the server (or ensure no jobs are running).
2) Delete the voice memory directory:

```bash
rm -rf data/voice_memory
```

If you override the path, delete `VOICE_MEMORY_DIR` instead of `data/voice_memory`.

---

## How to reset series voices

To remove all persistent series refs:

```bash
rm -rf data/voices
```

To remove a single series:

```bash
rm -rf data/voices/<series_slug>
```

If you override the path, delete `VOICE_STORE` instead of `data/voices`.

---

## Removing per-job speaker mappings

Speaker → character mappings are stored in the jobs DB (`DUBBING_STATE_DIR`, default `Output/_state/jobs.db`).

To fully wipe mappings:
- stop the server
- delete the jobs DB (this removes job history)

If you only want to change a mapping, use the **Voices** tab on a job page and resave.

---

## Privacy mode (optional)

Enable privacy/minimal storage:
- `PRIVACY_MODE=on`
- `MINIMAL_ARTIFACTS=1`

This reduces stored intermediates and can prevent voice refs from being written.
