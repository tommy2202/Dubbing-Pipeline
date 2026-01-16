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
