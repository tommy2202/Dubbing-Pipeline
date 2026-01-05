## Post Tier‑1 audit (post-change interference scan + fixes)

This report covers a paranoid, repo-wide audit after Tier‑1A (dialogue isolation + remixing) and Tier‑1B/C (timing-fit translation + segment pacing).

### Scope + proof approach

- **Repo-wide searches** were used to find duplicate/legacy implementations and risky patterns (ffmpeg, atempo/padding, Input/Output hardcodes, subprocess usage).
- **Fixes were applied in code**, not just documented.
- **Defaults remain backwards-compatible**: Tier‑1 features are still opt-in; legacy behavior stays default.

---

## Phase A — Interference scan + refactors (conflicts removed / routed)

### A1) Duplicate/old implementations that could conflict

- **Duplicate `atempo` chain logic** existed in both:
  - `src/anime_v2/stages/tts.py` (prosody filters)
  - `src/anime_v2/timing/pacing.py` (segment pacing)
  - **Fix**: canonicalized via `anime_v2.timing.pacing.atempo_chain()` and removed the duplicated implementation from `tts.py`.

- **Realtime chunk helpers used direct ffmpeg subprocess calls**
  - `src/anime_v2/realtime.py` had local `_concat_wavs_ffmpeg()` / `_pad_or_trim_to()` + direct ffmpeg calls.
  - **Fix**: routed through canonical helpers:
    - `anime_v2.utils.ffmpeg_safe.run_ffmpeg` for concat/trim
    - `anime_v2.timing.pacing.pad_or_trim_wav` for duration forcing

- **Legacy mixing / muxing**
  - `src/anime_v2/stages/mixing.py` remains as the **legacy** mixing path (kept intentionally for backward compatibility).
  - **Fix**: its internal ffmpeg/ffprobe calls were rerouted to the canonical wrappers (see A4).

### A2) Hardcoded config → centralized settings

Hardcoded v2 input upload paths were found in:
- `src/anime_v2/web/routes_jobs.py`
- `src/anime_v2/ops/retention.py`

**Fix**:
- Added new public settings (non-sensitive):
  - `INPUT_DIR`
  - `INPUT_UPLOADS_DIR`
- Web upload + API “filename” resolution now uses these settings with safe fallbacks (still under `APP_ROOT`).
- Retention uses configured uploads dir when set.

Legacy v1 hardcoded paths/ports were found in:
- `src/anime_v1/cli.py` (`/data/out`)
- `src/anime_v1/ui.py` (`/data/out`, `0.0.0.0:7860`)
- `src/anime_v1/stages/mkv_export.py` (`/data/out`)

**Fix**:
- Added public settings preserving old defaults:
  - `V1_OUTPUT_DIR` (default `/data/out`)
  - `V1_HOST` (default `0.0.0.0`)
  - `V1_PORT` (default `7860`)
- v1 code now reads these from config (defaults unchanged).

### A3) Direct env reads scattered across code

- No new scattered `os.environ`/`os.getenv` reads were introduced in `src/` beyond existing justified cases.
- The only notable env gate remains `STRICT_SECRETS` inside `config/settings.py` (explicitly process-level).

### A4) Risky subprocess usage → safer canonical wrappers

FFmpeg/ffprobe direct calls were consolidated to provide:
- list-args invocation (no `shell=True`)
- stderr capture (actionable errors)
- timeouts (best-effort)

**Updated to use canonical wrappers**:
- `src/anime_v2/stages/audio_extractor.py` → uses `extract_audio_mono_16k`
- `src/anime_v2/stages/export.py` → uses `run_ffmpeg`
- `src/anime_v2/stages/mkv_export.py` → uses `run_ffmpeg`
- `src/anime_v2/stages/mixing.py` → uses `ffprobe_duration_seconds` + `run_ffmpeg`
- `src/anime_v2/stages/tts.py` → ffmpeg calls use `run_ffmpeg` (prosody + pcm16k normalize)
- `src/anime_v2/realtime.py` → ffmpeg calls use `run_ffmpeg`

Non-ffmpeg subprocess usage (e.g. `demucs`, `espeak-ng`, optional `aws`) remains list-args and guarded.

---

## Phase B — Consistency & quality

### B1) Circular imports / import side effects

- No heavy work (model loads, ffmpeg execution) was moved into import-time code.
- `scripts/smoke_import_all.py` was extended to import Tier‑1 modules:
  - `anime_v2.audio.*`, `anime_v2.timing.*`, `anime_v2.realtime`, `anime_v2.jobs.queue`, plus server/web/cli.

### B2) CLI coherence

- Tier‑1 flags exist and are wired:
  - Tier‑1A: `--separation`, `--separation-model`, `--separation-device`, `--mix`, `--lufs-target`, `--ducking`, `--ducking-strength`, `--limiter`
  - Tier‑1B/C: `--timing-fit`, `--pacing`, `--wps`, `--tolerance`, pacing bounds
- Added alias flags to reduce confusion:
  - `--min-stretch` → alias of `--pacing-min-stretch`
  - `--pace-max-stretch` → alias of `--pacing-max-stretch`

### B3) Output layout coherence

Output directories are created with `mkdir(parents=True, exist_ok=True)` and `pathlib`:
- Tier‑1A: `Output/<job>/stems/*`, `Output/<job>/audio/final_mix.wav`
- Tier‑1B/C debug: `Output/<job>/segments/<idx>.json` (when enabled)

### B4) Logging / actionable errors

- FFmpeg failures now include stderr tails via `anime_v2.utils.ffmpeg_safe`.
- Missing optional deps (e.g. Demucs) keep a warning + fallback behavior.

---

## Phase C — Verification scripts (no real anime files required)

These scripts run without requiring a real input video:
- `scripts/verify_audio_pipeline.py`: ffmpeg/ffprobe checks + synthetic tone mix
- `scripts/verify_timing_fit.py`: timing-fit heuristics + basic pacing sanity
- `scripts/smoke_segment_pacing.py`: synthetic WAVs to validate stretch/pad/trim path
- `scripts/smoke_import_all.py`: imports CLI/server/web/tier‑1 modules to catch circular imports
- `scripts/verify_runtime.py`: safe config report + tool availability + writable dirs

---

## Phase D — Repo hygiene

### Tracked runtime artifacts removed

These were incorrectly tracked and could cause interference / noise:
- `logs/*`
- `Output/*`
- `**/__pycache__/*` / `*.pyc`

**Fix**:
- Added ignore rules in `.gitignore`
- Removed the tracked artifacts from git so fresh runs don’t dirty the repo.

---

## How to test quickly (3 commands)

```bash
make check
python3 scripts/smoke_import_all.py
python3 scripts/verify_audio_pipeline.py && python3 scripts/verify_timing_fit.py && python3 scripts/smoke_segment_pacing.py
```

---

## Files changed (high signal)

- **Config**: `config/public_config.py`, `.gitignore`
- **FFmpeg hardening / routing**:
  - `src/anime_v2/stages/audio_extractor.py`
  - `src/anime_v2/stages/export.py`
  - `src/anime_v2/stages/mkv_export.py`
  - `src/anime_v2/stages/mixing.py`
  - `src/anime_v2/stages/tts.py`
  - `src/anime_v2/realtime.py`
- **Paths + uploads**: `src/anime_v2/utils/paths.py`, `src/anime_v2/web/routes_jobs.py`, `src/anime_v2/ops/retention.py`
- **CLI**: `src/anime_v2/cli.py` (pacing alias flags)
- **Import smoke**: `scripts/smoke_import_all.py`
- **Legacy v1 config routing**: `src/anime_v1/cli.py`, `src/anime_v1/ui.py`, `src/anime_v1/stages/mkv_export.py`, `src/anime_v1/stages/lipsync.py`

