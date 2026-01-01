### Production hardening + performance upgrade audit

This document is a repo-wide audit + fix plan focused on **reliability, performance, maintainability, observability, and security** while preserving current outputs/default behavior.

---

## High-risk bugs (crashes / data loss / security)

- **Hardcoded `ffmpeg` binary in CLI diarization extraction**
  - **path**: `src/anime_v2/cli.py`
  - **symbol**: `cli()` (diarization segment extraction loop)
  - **issue**: uses `"ffmpeg"` literal instead of configured `ffmpeg_bin`; breaks in containers/custom paths and bypasses centralized config.
  - **planned fix**: route through a single ffmpeg runner (`anime_v2.utils.ffmpeg_safe`) and always use `get_settings().ffmpeg_bin`.

- **Hardcoded `ffmpeg`/`ffprobe` in mux/export paths**
  - **paths**: `src/anime_v2/stages/mkv_export.py`, `src/anime_v2/stages/export.py`, `src/anime_v1/stages/mkv_export.py`
  - **symbols**: `_ffprobe_duration_s()`, `mux()`, `export_hls()`
  - **issue**: hardcoded tool names cause runtime failures and inconsistent behavior vs config layer.
  - **planned fix**: use configured binaries and unify execution via a hardened ffmpeg wrapper.

- **Concat list injection / quoting hazard (realtime stitch)**
  - **path**: `src/anime_v2/realtime.py`
  - **symbol**: `_concat_wavs_ffmpeg()`
  - **issue**: concat demuxer list uses single quotes; filenames containing quotes/newlines can break parsing.
  - **planned fix**: escape concat paths safely or use a safer concat strategy; keep functionality identical.

---

## Medium issues (correctness drift / poor defaults / brittle behavior)

- **Subprocess usage is inconsistent (stderr dropped, timeouts missing)**
  - **paths**: multiple (`src/anime_v2/stages/*`, `src/anime_v2/utils/hashio.py`, `src/anime_v1/stages/*`)
  - **symbols**: many `subprocess.run(..., check=True)` calls
  - **issue**: failures often lose stderr context; timeouts are not consistently enforced; retry policy varies.
  - **planned fix**: implement a single robust runner in `anime_v2.utils.ffmpeg_safe`:
    - timeouts, retries, stderr capture (tail), friendly `FFmpegError` messages
    - then migrate high-value callsites (mux/mix/export/diarization chunking) to use it.

- **File writes sometimes non-atomic (best-effort copies)**
  - **paths**: `src/anime_v2/jobs/queue.py`, `src/anime_v2/realtime.py`, various stage code
  - **symbols**: `write_bytes`, `write_text` (direct)
  - **issue**: power loss / crash can leave partially-written artifacts.
  - **planned fix**: extend `anime_v2.utils.io` with atomic text/bytes helpers and use them in critical artifact writes (manifests, srt/vtt, job artifacts).

- **Whisper metadata is optional; word timestamps depend on implementation**
  - **paths**: `src/anime_v2/stages/transcription.py`, `src/anime_v2/cli.py`
  - **symbols**: `transcribe(... word_timestamps=...)`
  - **issue**: interface is guarded (TypeError fallback), but documentation and runtime checks should be clearer.
  - **planned fix**: add a runtime verifier script (`scripts/verify_runtime.py`) and document behavior and toggles in README/USAGE.

---

## Low issues (readability / consistency / small maintenance)

- **Mixed ffmpeg helpers across v1/v2**
  - **paths**: `src/anime_v1/*`, `src/anime_v2/*`
  - **issue**: duplicated subprocess patterns.
  - **planned fix**: prefer reusing v2 hardened helpers where safe; avoid big refactors that risk behavior drift.

---

## Performance bottlenecks / opportunities

- **Redundant ffmpeg invocations for segment extraction**
  - **paths**: `src/anime_v2/cli.py`, `src/anime_v2/stages/diarization.py`
  - **issue**: per-segment `ffmpeg` calls can be slow; no batching.
  - **planned fix**: keep behavior, but enforce timeouts + reuse `extract_audio_mono_16k` helper and avoid unnecessary re-encode flags.

- **Batch mode safety vs throughput**
  - **path**: `src/anime_v2/cli.py`
  - **issue**: multi-worker batch runs isolate processes (good), but could be more observable (per-item logs/exit codes) and bounded.
  - **planned fix**: keep the isolation design; improve worker spec + error reporting, and add `--dry-run` to preflight.

---

## Security concerns (baseline)

- **Subprocess safety**
  - **status**: good baseline (list argv, no `shell=True` found in `src/`)
  - **remaining**: improve error reporting without leaking secrets; keep using configured binaries; validate ffmpeg flags centrally (already exists).

- **Secret safety**
  - **status**: config split exists; logging redacts token-like patterns (`src/anime_v2/utils/log.py`).
  - **remaining**: ensure new diagnostic scripts are allowlisted for env reads and never print secret values.

---

## Quick wins vs bigger refactors

- **Quick wins (do now, low risk)**
  - Replace remaining hardcoded `ffmpeg`/`ffprobe` literals with `get_settings()` binaries.
  - Harden `ffmpeg_safe` runner with stderr tail + timeouts + retries.
  - Add `--dry-run` to CLI (preflight: ffmpeg present, input readable, output writable).
  - Add `scripts/verify_runtime.py` (config + tools + filesystem checks + safe config report).

- **Bigger refactors (defer / incremental)**
  - Full provider abstraction across ASR/MT/TTS (keep lightweight and backwards compatible).
  - Cross-stage cache normalization and manifest schema stabilization.

---

## Planned execution order (matches requested process)

1) **Config hardening**: ensure *all* soft-coded values route through `config/settings.py` (finish remaining ffmpeg literals).
2) **Robust ffmpeg/subprocess wrapper**: one utility with timeout/retry/stderr capture + friendly errors, then migrate callsites.
3) **Path + file I/O improvements**: atomic write helpers and safer temp dirs.
4) **Logging upgrades**: add `--verbose/--debug` and standard stage-timing helpers (keep structlog).
5) **Stage interfaces**: incremental provider pattern improvements without breaking current APIs.
6) **Batch improvements**: bounded workers, better resume checks, and clearer summaries.
7) **Caching**: extend where missing (translation) but keep existing behavior.
8) **Tests + CI**: keep current CI, add runtime verification script, ensure `make check` still passes.

