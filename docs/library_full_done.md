## Library grouping: implementation summary (done)

This document summarizes the completed “grouped library browsing” work: metadata, storage, manifests, APIs, UI, and policy controls.

---

## What changed

- **Jobs now carry library metadata**:
  - `series_title`, `series_slug`, `season_number`, `episode_number`, `visibility`
  - stored on the existing `Job` record (`src/anime_v2/jobs/models.py`)
- **Job submission requires metadata** (web/API):
  - `POST /api/jobs` requires `series_title`, `season_text`, `episode_text` (or numeric equivalents).
  - Parsing uses the canonical normalizer (`src/anime_v2/library/normalize.py`).
- **Canonical library index in jobs.db**:
  - A single indexed SQL table `job_library` inside the existing `jobs.db` enables efficient grouped queries.
- **Canonical manifest writer + layout strategy**:
  - `src/anime_v2/library/paths.py` is the single source of truth for output roots and `Output/Library/...` paths.
  - `src/anime_v2/library/manifest.py` is the single writer for the library-facing `manifest.json`.
- **Grouped library APIs + UI**:
  - `/api/library/*` provides series/seasons/episodes with strict object-level auth and numeric sorting.
  - `/ui/library*` pages render ordering provided by the API (no client grouping logic).
- **Robust queue policy + admin controls**:
  - Per-user caps, high-mode admin gating, GPU downgrade, and audit logs.
  - Admin endpoints for queue view, reprioritize, cancel, visibility updates.

---

## How to use (web)

1) Start server: `anime-v2-web`
2) Login at `/ui/login`
3) Submit a job at `/ui/upload`
   - Required fields: **Series name**, **Season**, **Episode**
4) Browse grouped library at `/ui/library`

Object-level auth:
- Users see **their private jobs** + **other users’ public jobs**.
- Admin sees everything.

---

## How to use (CLI)

CLI still writes canonical artifacts under `Output/<stem>/`. Library metadata is supplied via env vars:

- `ANIME_V2_SERIES_TITLE`
- `ANIME_V2_SEASON_NUMBER` (text accepted, parsed to int)
- `ANIME_V2_EPISODE_NUMBER` (text accepted, parsed to int)
- optional: `ANIME_V2_OWNER_USER_ID`, `ANIME_V2_VISIBILITY`

---

## Where outputs live

Canonical output directory (unchanged, backwards-compatible):
- `Output/<stem>/...`

Grouped library mirror (best-effort, preferred for browsing):
- `Output/Library/<series_slug>/season-XX/episode-YY/job-<job_id>/`
  - `manifest.json`
  - `master.mkv` (linked when possible)
  - `mobile.mp4` / `hls/` (linked when possible)
  - `logs/`, `qa/` (dirs created)

---

## Fallback behaviors

- **If Library/ path cannot be created**: manifest is written into the canonical Output job dir instead (`Output/<stem>/manifest.json`) and a warning is logged.
- **If linking isn’t possible** (e.g. Windows symlink restrictions): Library dir still exists with manifest + pointer files.
- **If manifest write fails**: the job still completes; the error is logged (best-effort behavior).
- **If GPU isn’t available**: `device=cuda` is downgraded to CPU; `mode=high` may be downgraded.

---

## Verification / gates

Run the full suite:

```bash
python3 scripts/library_full_gate.py
python3 scripts/library_full_gate.py --include-ui
```

