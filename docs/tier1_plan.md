### Tier-1 dubbing quality integration plan (T1-A / T1-B / T1-C)

This plan is **integration-first**: add Tier‑1 quality features by extending existing v2 pipeline modules (`anime_v2/stages/*`, `anime_v2/jobs/queue.py`, `anime_v2/cli.py`, web job endpoints) rather than introducing parallel pipelines.

**Scope**: planning only. No major code changes in this step.

---

## 1) Current architecture summary (v2)

### Entry points
- **CLI**: `anime-v2` → `src/anime_v2/cli.py:cli`
- **Server**: `src/anime_v2/server.py` (FastAPI app + lifecycle + scheduler + `JobQueue`)
- **Web/API**: `src/anime_v2/web/routes_jobs.py` (job submission + batch + transcript editing + resynthesis)
- **Legacy root app**: `main.py` (wraps `anime_v1` CLI for a simple upload form; not the primary v2 pipeline)

### Filesystem layout
- **Inputs**: user files stored under configured uploads dir (`config/public_config.py`: `uploads_dir`)
- **Outputs (stable)**: `Output/<video_stem>/...` (base output dir configurable)
- **Work dirs (ephemeral per job)**: `Output/<stem>/work/<job_id>/...` (v2 job queue)
- **Cache**: `Output/cache` (v2 cache store)

### Stage flow (v2 job execution)
Orchestrated by `src/anime_v2/jobs/queue.py`:
1) **Audio extraction**: `src/anime_v2/stages/audio_extractor.py:extract/run` → `audio.wav`
2) **(Optional) Diarization + speaker IDs**: `src/anime_v2/stages/diarization.py:diarize` + per‑speaker segment WAV extraction
3) **ASR**: `src/anime_v2/stages/transcription.py:transcribe` → `<stem>.srt` + `<stem>.json` (`segments_detail`, optional `words`)
4) **Segment assembly**:
   - segments are built as dicts containing at least `start`, `end`, `speaker`, `text`, optional `logprob`
   - timing can come from diarization utterances + ASR overlap assignment (preferred), else ASR timing
5) **MT**: `src/anime_v2/stages/translation.py:translate_segments` → `translated.json` with `segments[]`
6) **TTS**: `src/anime_v2/stages/tts.py:run` (per-line clips + aligned stitched track) + per‑clip retiming
7) **Mix/mux/export**: `src/anime_v2/stages/mixing.py:mix` (ducking + loudnorm + optional demucs bed) → MKV/MP4/HLS

---

## 2) Existing/partial implementations that will interact with Tier‑1

### Dialogue isolation / separation (T1‑A relevant)
- **v2 demucs integration (already present)**:
  - `src/anime_v2/stages/mixing.py:_run_demucs_if_enabled()` runs demucs (`mixing` optional dep) and searches for `no_vocals.wav`
  - `src/anime_v2/stages/mixing.py:mix()` can use `no_vocals.wav` as the background bed when `--separate-vocals` + `ENABLE_DEMUCS=1`
- **v1 separation stage (already present)**:
  - `src/anime_v1/stages/separation.py` runs `demucs.separate` and persists a background track, used by `src/anime_v1/stages/mkv_export.py`

**Risk**: Tier‑1 dialogue replacement must not fight the existing `separate_vocals` behavior. We should **reuse** demucs outputs and make the new behavior an extension (e.g., “dialogue replace mode”), not a second separation pipeline.

### Mixing/remix (T1‑A relevant)
- **Ducking + loudness control exists**:
  - `src/anime_v2/stages/mixing.py:_build_filtergraph()` uses `sidechaincompress` for ducking and `alimiter`
  - `src/anime_v2/stages/mixing.py` uses **2‑pass loudnorm** when profile requires it
- **MKV mux path exists**:
  - `src/anime_v2/stages/mkv_export.py:mux()` (copy video + AAC audio + optional SRT)

### Timing / alignment / stretching (T1‑B and T1‑C relevant)
- **Alignment utility**:
  - `src/anime_v2/stages/align.py:realign_srt()` uses VAD + optional aeneas and includes a **reading-speed heuristic** (`wpm_min/max`) to adjust segment durations.
- **TTS duration matching (already partial T1‑C)**:
  - `src/anime_v2/stages/tts.py` retimes each clip using `src/anime_v2/stages/align.py:retime_tts()` (librosa if available; else pad/trim).
  - v1 has `atempo`-based stretching: `src/anime_v1/stages/tts.py:_time_stretch_with_ffmpeg()`.
- **Optional word timestamps**:
  - `src/anime_v2/stages/transcription.py` can store `words[]` when whisper implementation supports it and `WHISPER_WORD_TIMESTAMPS=1` or `--align-mode word`.

### Translation rewrite / constraints (T1‑B relevant)
- **No explicit “fit-to-time rewrite” yet**, but translation already has:
  - glossary enforcement and fallback MT engines: `src/anime_v2/stages/translation.py`
  - style controls (honorifics/profanity): `src/anime_v2/stages/translation.py:_read_style/_apply_style`

---

## 3) Current segment data model (what we must preserve)

### Segment shape (v2)
Segments are plain dicts passed across stages:
- **Core keys**: `start: float`, `end: float`, `speaker: str`, `text: str`
- **Optional keys**: `logprob`, `avg_logprob`, `src_text`, `engine`, `glossary_ok`, `fallback_used`, `aligned_by`, etc.

### Timestamp sources
- **Preferred**: diarization utterances (`start/end`) with ASR text assigned by overlap
  - `src/anime_v2/jobs/queue.py` builds diarization‑timed `segments_for_mt`
- **Fallback**: ASR segments directly from Whisper metadata (`segments_detail`) or parsed SRT
  - `src/anime_v2/stages/transcription.py` writes `<stem>.json` with `segments_detail`

### Implication for Tier‑1
Tier‑1 must treat `start/end` as the authoritative **time budget** for translation fit and pacing, and must preserve existing JSON/SRT artifacts unless explicitly generating additional artifacts.

---

## 4) Current audio build process (where Tier‑1 plugs in)

### Original audio extraction
- `src/anime_v2/stages/audio_extractor.py:extract/run` produces mono 16k `audio.wav`.

### TTS generation + stitching
- `src/anime_v2/stages/tts.py:run`
  - per segment: synthesize to clip
  - normalize to 16k mono PCM
  - retime clip to match `(end-start)` (partial T1‑C)
  - `render_aligned_track()` stitches clips by padding/overlaying into `<stem>.tts.wav`

### Remix/mux
- `src/anime_v2/stages/mixing.py:mix`
  - selects background source: original video audio OR demucs bed (`no_vocals.wav`)
  - mixes with sidechain ducking + loudness processing
  - exports MKV/MP4/HLS

---

## 5) Where to insert Tier‑1 features (exact integration points)

### T1‑A) Dialogue isolation + remixing (keep BGM/SFX, replace dialogue)

**Goal**: preserve non-dialogue bed as much as possible; remove/attenuate original dialogue; lay TTS as dialogue track; keep existing output container behavior.

**Primary insertion**: `src/anime_v2/stages/mixing.py:mix`
- Extend background selection from:
  - current: **original audio** OR **demucs no_vocals bed**
  - Tier‑1: **dialogue-replaced mix**:
    - bed = `no_vocals.wav` (or best available “instrumental/no_dialogue” stem)
    - dialogue = TTS track only (no ducking required, or much lighter)
- Extend `_run_demucs_if_enabled()` to optionally surface both stems:
  - `vocals.wav` (proxy for dialogue)
  - `no_vocals.wav` (proxy for BGM/SFX)

**Fallbacks (offline-first)**
- If demucs not installed: keep current baseline ducking mix (already exists).
- If demucs installed but stem missing: keep baseline ducking mix.
- Optional “VAD-based dialogue gate” (no new heavy deps): attenuate original audio during detected speech segments as a last-resort approximation (future incremental improvement).

**Minimal surface area**:
- Add a single config/flag to switch “mix strategy”:
  - e.g., `MIX_DIALOGUE_MODE=duck|replace` (default stays `duck` to preserve current behavior)

### T1‑B) Timing-aware translation (rewrite to fit target time)

**Goal**: translation output text should be constrained to the segment’s time budget, so downstream TTS pacing is easier and quality is higher.

**Primary insertion**: `src/anime_v2/stages/translation.py:translate_segments`
- After base translation is produced, run a **timing fit pass** that:
  - computes duration budget = `end-start`
  - estimates readable/speakable text length budget via CPS/WPM heuristics (language-aware)
  - if overflow: shorten/rewrite deterministically (offline-first)
  - if underflow: optionally expand slightly (usually not required; prefer natural pauses)

**Secondary insertion**: `src/anime_v2/jobs/queue.py` where `TranslationConfig` is built
- Wire through per-job constraints (e.g., CPS target) via config/runtime.

**Fallbacks**
- Default to “no rewrite” if constraints are disabled (preserve current behavior).
- Keep rewrite rule-based by default; optionally enable transformer paraphraser only if already installed (translation extras already include transformers).

**Important**: keep glossary/style guarantees—rewrite must not violate glossary constraints:
- enforce glossary terms post‑rewrite (or rewrite only outside glossary spans).

### T1‑C) Segment pacing controls (TTS duration matching)

**Goal**: better duration matching across a wider range of durations without artifacts, and explicit pacing strategy controls.

**Primary insertion**: `src/anime_v2/stages/tts.py:run`
- Enhance per-segment pacing strategy selection (without changing defaults):
  - current: synthesize → normalize → `retime_tts()` → stitch
  - Tier‑1: choose among:
    - **pad/trim only** (for small deltas)
    - **time-stretch** (bounded by `max_stretch`)
    - **re-synthesize with adjusted rate** (when far off; engine-dependent)
    - **hybrid**: mild resynth + mild stretch

**Supporting module**: add a small pacing policy helper
- e.g., `src/anime_v2/stages/pacing.py` (pure functions: compute ratios, pick action, clamp)

**Fallbacks**
- If librosa isn’t installed: keep existing pad/trim fallback in `retime_tts`.
- If TTS engine doesn’t support rate control: fall back to time-stretch/pad/trim.

---

## 6) Dependency plan (optional vs required)

### Required (already required by repo)
- `ffmpeg` / `ffprobe`
- existing Python deps for v2 pipeline (Click/FastAPI/etc.)

### Optional (already in repo optional extras)
- **Demucs** (existing): `pyproject.toml` optional group `mixing = ["demucs>=4.0.1"]`
  - used for T1‑A best quality “bed extraction”
- **Alignment tooling** (existing): optional `align = [aeneas, librosa, pyloudnorm]`
  - used for T1‑C time-stretch quality and T1‑B time budget heuristics (read-speed)
- **Transformers** (existing): optional `translation = [transformers, sentencepiece, ...]`
  - can optionally support better rewrite/paraphrase, but **rule-based rewrite remains default**

### New deps
- **None required for Tier‑1 baseline** (plan is to implement rule-based + reuse existing optional deps).

---

## 7) Minimal new modules/files to add (planned)

No large new packages; small focused modules under `src/anime_v2/`:

- `src/anime_v2/stages/dialogue_isolation.py`
  - wrappers around demucs outputs and fallback “dialogue gate”
  - returns paths to `bed.wav` and/or `dialogue.wav` when available

- `src/anime_v2/stages/timing_fit.py`
  - pure functions:
    - `estimate_budget(duration_s, lang) -> max_chars/max_words`
    - `rewrite_to_fit(text, budget, lang, preserve_terms=...) -> text`
  - integrates into `translation.translate_segments`

- `src/anime_v2/stages/pacing.py`
  - pure functions:
    - `choose_pacing_action(target_dur, actual_dur, max_ratio) -> action`
  - integrates into `tts.run`

No new entrypoints required; everything is wired via existing CLI/server paths.

---

## 8) Risks / conflicts and mitigation

- **Existing `separate_vocals` / demucs behavior**
  - **risk**: Tier‑1 dialogue replacement could conflict with current “demucs bed + ducking” mode.
  - **mitigation**: treat Tier‑1 as a new **mix strategy** that reuses the same demucs outputs; keep existing default path unchanged.

- **Segment timing authority**
  - **risk**: changing `start/end` to satisfy reading speed could cascade into translation and TTS.
  - **mitigation**: T1‑B should not mutate timings; it should fit text to existing timing. Timing adjustments remain opt-in and are a separate concern.

- **Glossary constraints**
  - **risk**: rewrite could violate glossary requirements and create regressions.
  - **mitigation**: rewrite must preserve required terms or re-run glossary enforcement after rewrite.

- **Pacing artifacts**
  - **risk**: aggressive stretch can sound unnatural.
  - **mitigation**: bounded strategy selection; prefer rewrite (T1‑B) first, then mild pacing (T1‑C).

- **v1 vs v2 divergence**
  - **risk**: implementing Tier‑1 only in v2 might leave v1 behavior behind.
  - **mitigation**: scope Tier‑1 to v2 pipeline first (it’s the active server path); optionally backport later by calling shared helpers.

---

## 9) Implementation checklist (exact tasks, in-order)

### Repo wiring / surfaces
- [ ] Add new **config fields** (public) for Tier‑1 controls (defaults preserve current behavior), e.g.:
  - dialogue mode: `MIX_DIALOGUE_MODE=duck|replace` (default `duck`)
  - translation fit: `MT_FIT_MODE=off|heuristic|model` (default `off`)
  - pacing strategy: `PACING_MODE=auto|stretch|pad|resynth` (default `auto`)
  - pacing limits: `PACING_MAX_STRETCH=0.15` (reuse existing `MAX_STRETCH`)
- [ ] Add CLI flags in `src/anime_v2/cli.py` that map to those config values (optional overrides).
- [ ] Add job runtime plumbing for API jobs:
  - accept/validate in `src/anime_v2/web/routes_jobs.py`
  - persist in `Job.runtime`
  - read in `src/anime_v2/jobs/queue.py` and pass into stages.

### T1‑A (dialogue isolation + remix)
- [ ] Implement `stages/dialogue_isolation.py`:
  - returns `bed_wav` and a “confidence”/metadata of how it was derived
  - uses demucs if available; otherwise returns `None`
- [ ] Extend `stages/mixing.py:mix`:
  - add a new mode “replace”: use bed as bg, mix TTS as dialogue (no ducking, or very light)
  - keep existing “duck” path as default
- [ ] Add artifacts:
  - optional save `bed.wav` for inspection (under work dir)

### T1‑B (timing-aware translation)
- [ ] Implement `stages/timing_fit.py` (pure functions, no heavy deps required)
- [ ] Integrate into `stages/translation.py:translate_segments`:
  - after translation + style + glossary, apply fit pass when enabled
  - preserve glossary terms
  - annotate output with metadata: `fit_applied`, `fit_ratio`, `cps_target` (non-breaking additions)

### T1‑C (segment pacing controls)
- [ ] Implement `stages/pacing.py` (choose action based on duration ratio)
- [ ] Integrate into `stages/tts.py:run`:
  - compute per-line target duration and apply chosen pacing
  - keep current retime behavior as default; add policy gates so defaults remain identical

### Validation (no regressions)
- [ ] Update `docs/feature_audit.md` / README to reflect Tier‑1 flags once implemented
- [ ] Add/extend tests (later step): ensure default path produces identical artifacts for existing test suite

