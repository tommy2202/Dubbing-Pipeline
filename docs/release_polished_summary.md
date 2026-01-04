## Release polished summary

### What was fixed / hardened

- **Phase 0 report**: added `docs/release_hardening.md` (entrypoints, stage map, risks, fix plan).
- **Correctness/import safety**:
  - Fixed a dead ASR “missing whisper” fallback check in `src/anime_v2/stages/transcription.py`.
  - Hardened `scripts/smoke_import_all.py` to import all major entrypoints/modules and print actionable tracebacks.
  - Made `anime_v1` import-safe by removing hard imports of optional deps at import time (lazy `whisper`/`pydub`).
- **Dependency hygiene**:
  - Added optional legacy extra (`legacy`) in `pyproject.toml` for `anime_v1` UI deps.
  - Updated Docker constraints numpy pin to a modern 1.26.x compatible range for Python 3.10 images.
  - Added `scripts/verify_env.py` for required vs optional deps + feature availability reporting.
- **Obsolete/duplicate cleanup**:
  - Removed the unused legacy diarization implementation `src/anime_v2/stages/diarize.py`.
  - Updated docs that referenced it (`docs/tier2_plan.md`, `docs/tier_next_plan.md`).
- **Resume-safe metadata**:
  - Added `src/anime_v2/jobs/manifests.py` and wrote stage manifests for at least `audio` and `transcribe` in both CLI and job queue (best-effort).
  - Added a minimal `JobContext` in `src/anime_v2/jobs/context.py` (public-settings snapshot only; no secrets).
- **Stage-based logs + ffmpeg stderr capture**:
  - Added per-job log artifacts via `src/anime_v2/utils/job_logs.py`.
  - Added concurrency-safe ffmpeg stderr capture to `Output/<job>/logs/ffmpeg/` via `ContextVar` support in `src/anime_v2/utils/ffmpeg_safe.py`.
  - Added CLI flags: `--log-level`, `--log-json` (reserved), `--debug-dump`.
- **Polish gate**:
  - Added `scripts/polish_gate.py` (one-command health check).

### What was removed

- `src/anime_v2/stages/diarize.py` (legacy, unused; superseded by `src/anime_v2/stages/diarization.py`)

### How to run the polish gate (one command)

```bash
python3 scripts/polish_gate.py
```

This runs:
- import smoke test
- env verification (required vs optional deps)
- synthetic tests (mixing, timing-fit, PG filter, style guide, QA, multitrack mux)
- scans for stub/wireframe markers and known-obsolete modules

### How to run a full job (CLI)

```bash
anime-v2 Input/Test.mp4 --mode medium --device auto
```

Optional release-debug run:

```bash
anime-v2 Input/Test.mp4 --log-level DEBUG --debug-dump
```

