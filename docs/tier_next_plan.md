## Tier‑Next Plan (pre-implementation scan + integration design)

Scope: **planning only**. This document records repo-wide conflict scan results and proposes canonical insertion points, removal/reroute plan, metadata schemas under `Output/<job>/`, dependency/fallback strategy, and logging approach.

---

### 1) Current architecture summary (Tier‑Next insertion points)

#### v2 CLI pipeline

Entry: `src/anime_v2/cli.py` (`run`)

High-level order:
- Extract audio: `anime_v2.stages.audio_extractor.extract`
- (Optional) separation/mixing: `anime_v2.audio.separation`, `anime_v2.audio.mix`, `anime_v2.stages.mixing`
- Diarization: `anime_v2.stages.diarization.diarize` (uses VAD gating via `anime_v2.utils.vad`)
- Transcription: `anime_v2.stages.transcription.transcribe` (writes `*.srt` and `*.json` with `segments_detail`)
- Translation: `anime_v2.stages.translation.translate_segments`
  - already supports glossary + style YAML, including honorific policy + profanity masking
- TTS: `anime_v2.stages.tts.run`
  - Tier‑2 review locks, Tier‑3 expressive controls, Tier‑1 pacing/duration control
- Mix/export: `anime_v2.audio.mix` (enhanced) or `anime_v2.stages.mixing.mix` (legacy)
- Optional lipsync plugin: `anime_v2.plugins.lipsync.*`

#### v2 job-queue pipeline

Entry: `src/anime_v2/jobs/queue.py`

Same stages as CLI with checkpointing and web-triggered resynth/review integration. Best insertion point for Tier‑Next features that must apply to both CLI and web.

#### Web/API

Entry: `src/anime_v2/web/routes_jobs.py`

Already has endpoints for:
- job submission + progress streaming (SSE / websocket)
- transcript editing + resynth triggers
- Tier‑2 review loop endpoints
- Tier‑3 streaming manifest/chunk endpoints

---

### 2) Repo-wide conflict scan (existing/basic frameworks overlapping Tier‑Next)

#### A/B) Singing/music detection & OP/ED detection

Existing overlapping pieces:
- VAD: `src/anime_v2/utils/vad.py` (webrtcvad optional; energy gate fallback)
- Diarization filters: `src/anime_v2/stages/diarization.py` intersects diarization with VAD speech segments.
- Demucs separation features: `--separate-vocals` and Tier‑1A separation/mix modules (useful as signal for “music/vocals present” but not a detector).

Potential conflicts to unify later:
- Multiple “speech vs non-speech” heuristics exist (VAD + demucs-based bed separation). We should create **one canonical “audio classification timeline”** module and have both VAD gating and “keep original during singing/music” consult it.

#### C) PG Mode

Existing overlapping pieces:
- Style rules already include profanity masking + honorific policy in translation:
  - `src/anime_v2/stages/translation.py` `_apply_style()` uses `style.profanity` and a `profanity_words` list.

Conflict:
- PG Mode is a **session-only switch**, while current profanity behavior is driven by style YAML (persistent).
  - Plan: PG Mode should layer on top (override) without replacing existing style framework.

#### D) Quality checks & scoring

Existing overlapping pieces:
- ASR metadata includes `segments_detail` and logprobs (in `transcription.py` metadata JSON)
- Tier‑1 pacing produces per-segment debug actions JSON when enabled (`TIMING_DEBUG`)
- Mixing supports loudness targets (`LUFS`) and limiter, but no explicit “clip/peak detector”

Conflict:
- Multiple places already compute per-segment debug info (timing debug, review state). We need a single canonical **QA report** writer with stable schema.

#### E) Glossary & style guide per project

Existing overlapping pieces:
- Translation already supports:
  - `--glossary` (TSV) via `_read_glossary(...)` in `translation.py`
  - `--style` (YAML) via `_read_style(...)` in `translation.py`
  - honorific/profanity policies within style application.
- Web has “projects” infrastructure (`JobStore._projects()` and templates), and job submission includes `output_subdir` / projects UI.

Conflict:
- There is already a “global per-run glossary/style path” model. Adding per-project rules must not create a competing framework.
  - Plan: make project rules resolve to existing `glossary_path`/`style_path` inputs so translation remains the single source of truth.

#### F) Scene-aware speaker smoothing

Existing overlapping pieces:
- `src/anime_v2/stages/diarization.py` already merges short gaps of same speaker after VAD intersection.
- There is also an older diarization framework: `src/anime_v2/stages/diarize.py` that persists `voices/registry.json` etc.

Conflict:
- **Two diarization implementations** exist (`diarization.py` and `diarize.py`). Today, the v2 pipeline uses `diarization.py`.
  - Plan: treat `diarization.py` as canonical, and either delete `diarize.py` or explicitly mark/route it as legacy-only to avoid future Tier‑Next work integrating into the wrong one.

#### G) Dub Director mode

Existing overlapping pieces:
- Tier‑3 expressive module is canonical now (`anime_v2/expressive/*`)
- Tier‑2 review loop allows manual edits + lock per segment

Conflict:
- Avoid reintroducing a second “expressive policy” layer. Dub Director mode should be a **policy wrapper** around the expressive module + review loop artifacts.

#### H) Multi-track outputs (orig/dubbed/background/dialogue-only)

Existing overlapping pieces:
- Enhanced mixing outputs:
  - `Output/<job>/audio/final_mix.wav`
  - `Output/<job>/stems/background.wav`, `dialogue.wav` (when separation enabled)
- Export/mux code currently produces single-audio-track MKV/MP4/HLS:
  - `src/anime_v2/stages/export.py` and `src/anime_v2/stages/mkv_export.py`

Conflict:
- Multiple “audio artifact” locations exist (legacy vs enhanced mixing). We should define canonical track naming and ensure exporters map multiple audio tracks consistently.

---

### 3) Removal vs reroute plan (before implementing Tier‑Next features)

These are **conflict-resolution tasks** that must be done before new Tier‑Next code lands:

- **Diarization duplication**
  - **Canonical**: `src/anime_v2/stages/diarization.py`
  - **Action**: either delete `src/anime_v2/stages/diarize.py` or reroute callers to `diarization.py` only; add a doc comment if it must remain for compatibility.

- **Music/singing timeline**
  - **Canonical to add**: `src/anime_v2/audio/timeline.py` (planned)
  - **Action**: reroute any future VAD/music/singing decisions through this module (instead of ad-hoc checks in diarization/mixing).

- **Profanity/PG**
  - **Canonical**: keep `translation.py` style system.
  - **Action**: PG Mode should not add a new profanity framework; it should override style policy resolution at runtime.

---

### 4) Data model plan (new metadata under `Output/<job>/`)

#### A/B) Singing/music + OP/ED detection

File: `Output/<job>/audio_timeline.json`

Schema (v1):
```json
{
  "version": 1,
  "source_audio": "Output/<job>/audio.wav",
  "segments": [
    {"start": 12.34, "end": 45.67, "kind": "music", "confidence": 0.82, "tags": ["singing"]},
    {"start": 0.0, "end": 90.0, "kind": "opening", "confidence": 0.70, "tags": ["op"]}
  ]
}
```

Consumption rules:
- For any segment marked `music`/`opening`/`ending`, the pipeline should **keep original audio** (no dialogue replacement) for those times.

#### C) PG Mode (session-only)

No persistent file by default. If we need traceability, add a **non-authoritative** runtime snapshot:
- `Output/<job>/runtime/session_flags.json`:
```json
{"pg_mode": true, "set_via": "web|cli", "ts": "..."}
```

#### D) Quality checks & scoring

File: `Output/<job>/qa/report.json`

Schema (v1):
```json
{
  "version": 1,
  "job_id": "...",
  "checks": {
    "alignment": {"score": 0.91, "details": {...}},
    "pacing": {"score": 0.88, "details": {...}},
    "clipping": {"score": 0.99, "peaks_dbfs": -1.2},
    "asr_confidence": {"score": 0.74, "avg_logprob": -0.52},
    "speaker_flips": {"score": 0.95, "count": 3}
  },
  "segments": [
    {"segment_id": 12, "score": 0.77, "flags": ["fast", "low_confidence"]}
  ]
}
```

#### E) Glossary/style per project

Persistent under `Output/<job>/project/` snapshot:
- `Output/<job>/project/rules.json` (resolved glossary/style policy used for the run)

And stored centrally under JobStore projects later (not in Output).

#### F) Speaker smoothing / scene detection

File: `Output/<job>/diarization_smoothing.json`

Schema (v1):
```json
{
  "version": 1,
  "method": "scene_aware_merge",
  "params": {"max_flip_gap_s": 0.4, "scene_cut_guard_s": 0.2},
  "changes": [{"at": 123.4, "from": "SPEAKER_02", "to": "SPEAKER_01", "reason": "flip_smooth"}]
}
```

#### H) Multi-track outputs

File: `Output/<job>/audio/tracks.json`

Schema (v1):
```json
{
  "version": 1,
  "tracks": {
    "orig": "Output/<job>/audio.wav",
    "dubbed": "Output/<job>/<stem>.tts.wav",
    "final_mix": "Output/<job>/audio/final_mix.wav",
    "background": "Output/<job>/stems/background.wav",
    "dialogue_isolated": "Output/<job>/stems/dialogue.wav"
  }
}
```

---

### 5) Dependency plan (optional, offline-first)

- **Music/singing detection**
  - Default: energy/VAD heuristics + optional demucs signal if installed
  - Optional: lightweight classifier model (local file) if provided
  - No internet required.

- **Scene detection**
  - Default: timestamp-based heuristics (gap patterns + subtitle boundaries)
  - Optional: PySceneDetect (only if installed).

- **Quality scoring**
  - Default: ffprobe/ffmpeg measurements + existing ASR metadata (logprobs) + pacing reports
  - Optional: librosa for pitch-related checks; otherwise skip pitch checks.

---

### 6) Logging plan (structured + per-job context)

Use existing structlog setup (`src/anime_v2/utils/log.py`) and:
- Add a per-job bound logger context: `job_id`, `video_stem`
- For per-segment actions (music keep, smoothing, QA flags), log events like:
  - `tier_next.music_keep` `{segment_id, start_s, end_s, confidence, reason}`
  - `tier_next.qa_flag` `{segment_id, kind, value, threshold}`
  - `tier_next.speaker_smooth` `{at_s, from, to, reason}`

Also persist these decisions into the JSON artifacts above for deterministic replay.

---

### 7) Concrete implementation checklist (future prompts)

When prompted to implement the Tier‑Next features, the expected “touch points” are:
- **Audio timeline / music detection**: new `src/anime_v2/audio/timeline.py` and integration into `anime_v2.stages.tts` alignment/mixing overlay logic.
- **PG mode**: CLI+web session flag (ephemeral), overrides translation style policy resolution (`translation.py`) and/or subtitle rewriting.
- **QA scoring**: new `src/anime_v2/qa/` module, called near end of `jobs/queue.py` and `cli.py` to write `Output/<job>/qa/report.json`.
- **Project glossary/style**: integrate with existing `translation.py` loaders; store resolved snapshot under `Output/<job>/project/`.
- **Speaker smoothing**: extend `anime_v2.stages.diarization.py` output normalization; write smoothing report; ensure voice memory mapping uses smoothed labels.
- **Multi-track export**: extend `stages/export.py` / `mkv_export.py` to map multiple audio tracks, using `audio/tracks.json` as the single source of truth.

---

STOP: This is the pre-implementation scan + plan only.

