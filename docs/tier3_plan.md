## Tier‑3 Plan (T3‑A/T3‑B/T3‑C)

Scope: **planning only** (no major code yet). This document maps the current repo architecture and proposes Tier‑3 integration points, conflict resolution, dependency/fallback strategy, and an implementation checklist.

---

### 1) Current architecture (what exists today)

#### v2 pipeline (CLI path)

Primary entrypoint: `src/anime_v2/cli.py` (`run`)

Stage order (high level):
- **Extract audio**: `anime_v2.stages.audio_extractor.extract`
- **(Optional Tier‑1A) Separate + mix mode enhanced**: `anime_v2.audio.separation.separate_dialogue` then `anime_v2.audio.mix.mix_dubbed_audio`
- **Diarization**: `anime_v2.stages.diarization.diarize` (+ VAD gating via `anime_v2.utils.vad`)
- **Transcription**: `anime_v2.stages.transcription.transcribe`
- **Translation**: `anime_v2.stages.translation.translate_segments` (with optional timing fit)
- **TTS**: `anime_v2.stages.tts.run`
  - Prosody controls already exist:
    - `_emotion_controls(...)` (off/auto/tags)
    - `_apply_prosody_ffmpeg(...)` (rate/pitch/energy via ffmpeg filters)
- **Mixing/mux/export**:
  - Legacy/baseline: `anime_v2.stages.mixing.mix`
  - Enhanced mode: `anime_v2.stages.export.export_*` using `final_mix.wav`

#### v2 pipeline (job queue path)

Orchestrator: `src/anime_v2/jobs/queue.py`

Same stage order as CLI, but executed under per‑job work directories and checkpointing. Tier‑2B review locks are injected into TTS via `review_state_path`.

#### Tier‑2A voice memory

Canonical store: `src/anime_v2/voice_memory/`
- `embeddings.py`: embedding providers + cosine matching + deterministic fingerprint fallback
- `store.py`: persistent store under `data/voice_memory/`

#### Tier‑2B review loop

Canonical state: `src/anime_v2/review/`
- `state.json` at `Output/<job>/review/state.json`
- Locked segments are reused by `anime_v2.stages.tts.run(review_state_path=...)`

#### “Realtime” support (currently pseudo‑streaming)

Module: `src/anime_v2/realtime.py`
- CLI flags already exist: `--realtime --chunk-seconds --chunk-overlap --stitch`
- Implementation is a **chunked offline pipeline** producing per‑chunk artifacts under `Output/<stem>/realtime/`
- Optional stitch is concat‑based (ffmpeg concat demuxer) and subtitle stitching uses adjusted timestamps

---

### 2) Repo-wide Tier‑3 discovery results (existing attempts/hooks)

#### T3‑A Lip-sync (existing)

There is **v1-only** lip-sync integration:
- `src/anime_v1/stages/lipsync.py`: shells out to a Wav2Lip repo (`infer.py`) if present under `MODELS_DIR/Wav2Lip` and checkpoint under `MODELS_DIR/wav2lip/wav2lip.pth`.
- `src/anime_v1/cli.py`: `--lipsync/--no-lipsync` flag, calls `anime_v1.stages.lipsync.run(...)`.
- `src/anime_v1/ui.py`: checkbox “Lip-sync”.

There is **no v2 lip-sync stage/plugin** yet.

#### T3‑B Prosody / emotion (existing)

Already present in v2:
- `src/anime_v2/stages/tts.py`:
  - `--emotion-mode off|auto|tags` (heuristics)
  - `--speech-rate`, `--pitch`, `--energy` (global multipliers)
  - `_apply_prosody_ffmpeg(...)` uses ffmpeg `asetrate + atempo` and `volume`
- VAD and energy gating exist:
  - `src/anime_v2/utils/vad.py` (webrtcvad optional + RMS gate fallback)

Missing: **source-audio-derived per‑segment prosody transfer** (intensity/pitch variability → expressive guidance) and a formal “prosody mode” concept.

#### T3‑C Streaming/chunking (existing)

Already present in v2:
- `src/anime_v2/realtime.py`: pseudo‑streaming (chunked) offline pipeline
- Web stack includes websocket/SSE support (job progress, logs), plus HLS export in the normal pipeline:
  - `src/anime_v2/stages/export.py` (`export_hls`)
  - `src/anime_v2/web/routes_jobs.py`: websocket `/ws/jobs/{id}` and SSE endpoints

Missing: **true low‑latency streaming mode** (incremental output to player / progressive HLS segments as chunks complete) and a modular “streaming pipeline” interface.

---

### 3) Conflicts / basic frameworks to unify or replace

#### Lip-sync conflicts
- **v1 Wav2Lip stub** (`anime_v1.stages.lipsync`) is the only implementation.
  - Plan: move Wav2Lip execution logic into a **v2 plugin** (shared utility), and have v1 delegate to it (or keep v1 as-is but mark v2 plugin as canonical).

#### Prosody/emotion conflicts
- Current prosody controls are embedded inside `anime_v2.stages.tts.py`.
  - Plan: keep as the baseline fallback, but extract the logic into a small `anime_v2/prosody/` module to avoid TTS becoming the “god module”.

#### Streaming conflicts
- `anime_v2.realtime.realtime_dub` currently mixes transcription/translation/TTS logic inline.
  - Plan: refactor into a streaming pipeline module (generator-style) while keeping the current function as the “pseudo-stream fallback” wrapper for CLI compatibility.

---

### 4) Tier‑3 design: architecture + insertion points

#### T3‑A) Lip-sync video option as a plugin (optional)

**Goal**: Optional post-processing step that takes (video, dubbed audio) and produces a lipsynced video.

Insertion point (v2):
- After final audio is ready (post TTS + mixing), before final “published output” is declared.
  - CLI path: `src/anime_v2/cli.py` near the end (after `mix(...)` / `mkv_export.run(...)` chooses the final container).
  - Job queue path: `src/anime_v2/jobs/queue.py` after the mux/export phase produces the final MKV/MP4.

Plugin interface (proposed):
- New package: `src/anime_v2/plugins/lipsync/`
  - `base.py`: `LipSyncPlugin` protocol/interface
  - `wav2lip.py`: Wav2Lip implementation (subprocess wrapper, timeouts, safe paths)
  - `registry.py`: resolve plugin from config/CLI (`off|wav2lip`)

Output behavior:
- Default remains unchanged (no lipsync).
- When enabled, produce an additional artifact:
  - `Output/<job>/video/lipsync.mp4` (or `.mkv`) and optionally set it as the final “primary” output if configured.

#### T3‑B) Prosody / emotion transfer with offline heuristics fallback

**Goal**: Per‑segment expressive guidance derived from:
- (Primary) TTS engine capabilities (if supported)
- (Fallback) offline heuristics: punctuation + source audio features (RMS, speech rate proxy, pitch proxy)

Insertion point:
- Inside per‑segment synthesis in `src/anime_v2/stages/tts.py`:
  - Right before each clip is synthesized, compute segment “prosody hints”.
  - If the TTS engine supports a parameter (e.g., `speed` already supported by Coqui XTTS), apply it.
  - Otherwise apply `_apply_prosody_ffmpeg` after synthesis (already done).

Proposed modules:
- `src/anime_v2/prosody/features.py`
  - Extract lightweight features from source segment wav: RMS, ZCR, simple pitch estimate (autocorrelation-based; no heavy deps).
- `src/anime_v2/prosody/hints.py`
  - Convert features + text cues into `(rate_mul, pitch_mul, energy_mul)` and optional categorical tags.
- `src/anime_v2/prosody/__init__.py`

Data sources for features:
- If diarization segments exist: use `diarization.json` segment wavs (`wav_path`).
- Else: slice from extracted audio with ffmpeg to a temp wav (bounded duration).

Debugging:
- Optional per-segment report appended to the existing segment debug JSON (Tier‑1C), or write `Output/<job>/segments/<idx>.prosody.json` when enabled.

#### T3‑C) Real-time / streaming dubbing mode

**Goal**: Two modes:
- **Real streaming pipeline** (when dependencies and runtime allow): produces incremental outputs suitable for a player (e.g., rolling HLS segments).
- **Pseudo-stream fallback**: current `anime_v2.realtime.realtime_dub` behavior (offline chunked run, optional stitch).

Insertion points:
- CLI already routes `--realtime` to `src/anime_v2/realtime.py` early in `src/anime_v2/cli.py`.
- Web server already has websocket/SSE infrastructure and HLS export in the non-realtime pipeline.

Proposed architecture:
- `src/anime_v2/streaming/pipeline.py`
  - Generator that yields `ChunkResult` events (ASR done / MT done / TTS done / mux segment ready).
- `src/anime_v2/streaming/hls_live.py`
  - Best-effort “append segments as they complete” HLS writer (optional; ffmpeg-driven).
- Keep `src/anime_v2/realtime.py` as the pseudo-stream fallback wrapper, refactored to call the streaming pipeline with a “file output sink”.

Web integration (later Tier‑3 work):
- Extend existing websocket to push chunk events.
- Optionally host a simple player page that plays HLS manifest as it grows.

---

### 5) Dependency plan + fallbacks

#### Lip-sync (Wav2Lip)
- **Optional GPU**; can run CPU but slow.
- Detection:
  - repo exists under `MODELS_DIR/Wav2Lip`
  - checkpoint exists under `MODELS_DIR/wav2lip/wav2lip.pth` (or configurable)
- Fallback: if unavailable → warn and skip (no lipsync).

#### Prosody / emotion transfer
- Primary: whatever the active TTS engine supports (today: Coqui XTTS supports `speed`; no explicit style embedding support in current wrapper).
- Fallback: offline heuristics + `_apply_prosody_ffmpeg` (already implemented).
- No internet required.

#### Streaming
- Primary: chunk pipeline with incremental outputs (likely ffmpeg-driven HLS).
- Fallback: existing pseudo-stream (`anime_v2.realtime.realtime_dub`) that writes chunk artifacts and optionally stitches.

---

### 6) Security + performance notes

- **Subprocess safety**: Wav2Lip must be executed with list-args only; no `shell=True`; capture stderr; enforce timeouts; sanitize/resolve paths under configured `MODELS_DIR` and `Output/<job>/`.
- **Egress**: Tier‑3 features should remain offline-first; do not fetch models at runtime.
- **Resource control**:
  - Wav2Lip: add explicit timeout + optional “max fps”/downscale to bound runtime.
  - Streaming: bounded chunk queue and per-chunk temp directory under `Output/<job>/realtime/<chunk>/`.
- **Caching**:
  - Lip-sync can be cached by hash(video + audio + plugin params) to avoid re-running.
  - Prosody feature extraction can reuse diarization segment wavs when available.

---

### 7) Implementation checklist (next step, after this plan)

#### T3‑A Lip-sync plugin
- Add `src/anime_v2/plugins/lipsync/base.py`
- Add `src/anime_v2/plugins/lipsync/wav2lip.py` (port + harden v1 logic)
- Add config + CLI:
  - `config/public_config.py`: `lipsync: str` (`off|wav2lip`), `wav2lip_repo_dir`, `wav2lip_ckpt_path`, `lipsync_timeout_s`
  - `src/anime_v2/cli.py`: `--lipsync off|wav2lip`
  - `src/anime_v2/jobs/queue.py`: wire settings into post-mux step
- Add verification: `scripts/verify_lipsync_plugin.py` (dry-run validation of detection and command construction; no real face video required)

#### T3‑B Prosody transfer
- Add `src/anime_v2/prosody/features.py` and `hints.py`
- Add config + CLI:
  - `prosody_mode: off|heuristic|source`
  - `prosody_debug: bool`
- Wire into `src/anime_v2/stages/tts.py` per-segment synthesis

#### T3‑C Streaming mode
- Add `src/anime_v2/streaming/pipeline.py` with a `yield`ing event stream
- Refactor `src/anime_v2/realtime.py` to use the pipeline (keep existing CLI behavior)
- Optional web: extend websocket events and add a minimal UI/player page later

