## Post Tier‑Next Audit (A–H)

### What was implemented (Feature H: Multi‑track outputs)

- **Track artifacts (deterministic, per job)**: `src/anime_v2/audio/tracks.py`
  - Writes to `Output/<job>/audio/tracks/`:
    - `original_full.wav`
    - `dubbed_full.wav`
    - `background_only.wav` (uses separation bed when available; otherwise derived from original)
    - `dialogue_only.wav`
  - When `--container mp4`, also writes sidecar `*.m4a` tracks in the same folder.
- **Container muxing**
  - **MKV multi‑audio mux**: `src/anime_v2/stages/export.py` (`export_mkv_multitrack`)
    - Video stream is copied (`-c:v copy`)
    - Audio tracks encoded to AAC, with `title` + `language` metadata
  - **MP4 fallback**: `export_m4a` sidecar tracks for players that don’t reliably support multi‑audio MP4.
- **Pipeline wiring**
  - CLI runs: `src/anime_v2/cli.py` (`--multitrack`, `--container`)
  - Job queue runs: `src/anime_v2/jobs/queue.py` (uses settings `multitrack` + `container`)
- **Web UI/API file listing**
  - `src/anime_v2/web/routes_jobs.py` now includes `Output/<job>/audio/tracks/*.{wav,m4a}` in `/api/jobs/{id}/files`.

### How to test quickly (≤ 5 commands)

```bash
python3 scripts/verify_multitrack_mux.py
python3 scripts/smoke_import_all.py
python3 scripts/verify_music_detect.py
python3 scripts/verify_pg_filter.py
python3 scripts/verify_qa.py
```

### Outputs (where to find things)

- **Multi‑track artifacts**: `Output/<job>/audio/tracks/`
- **Multi‑audio MKV (when enabled)**: `Output/<job>/dub.mkv` (CLI) or `Output/<job>/<stem>.dub.mkv` (queue naming)
- **MP4 + sidecar tracks (when enabled with `--container mp4`)**:
  - `Output/<job>/dub.mp4`
  - `Output/<job>/audio/tracks/*.m4a`

### Repo‑wide interference sweep (Tier‑Next features)

Searched for duplicate/obsolete code paths across:

- **Music detection/preservation**: `src/anime_v2/audio/music_detect.py` is the single source of truth; invoked by `cli.py`, `jobs/queue.py`, and `streaming/runner.py`.
- **PG filter**: `src/anime_v2/text/pg_filter.py` is the single source of truth; invoked by `cli.py`, `jobs/queue.py`, and `streaming/runner.py`.
- **QA scoring**: `src/anime_v2/qa/scoring.py` is the single source of truth; invoked by `cli.py`, `jobs/queue.py`, `streaming/runner.py`, and `qa` CLI.
- **Style guide**: `src/anime_v2/text/style_guide.py` is the single source of truth; invoked by `cli.py`, `jobs/queue.py`, and `streaming/runner.py`.
- **Speaker smoothing**: `src/anime_v2/diarization/smoothing.py` is the single source of truth; invoked by `cli.py` and `jobs/queue.py`.
- **Director mode**: `src/anime_v2/expressive/director.py` is the source of truth for director planning; invoked by `stages/tts.py`.
- **Mux/multitrack**: `src/anime_v2/stages/export.py` is the single source of truth for MKV/MP4 exports and multitrack muxing.

No conflicting duplicate “framework” implementations were found for these features; the new multi‑track implementation was wired through the existing export/mix orchestration rather than creating parallel mux flows.

### Hardening notes / safety checks

- **Video re‑encode**: MKV multitrack mux uses `-c:v copy` (no re‑encode).
- **Determinism**: track filenames and MKV audio track order/metadata are fixed.
- **Optional deps**: multitrack uses ffmpeg/ffprobe only; no new heavy deps.
- **Import safety**: new modules do not perform heavy work at import time.

### Known limitations

- **MP4 multi‑audio**: intentionally not relied upon by default; sidecar `*.m4a` files are generated instead when `--container mp4`.
- **Background‑only accuracy when separation is off**: `background_only.wav` is derived from the original track when no background stem exists (it is not “true BGM/SFX only” without separation).

