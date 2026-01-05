### Feature Audit (F1–F9)

This document audits the repository’s dubbing pipeline against 9 requested features, with concrete evidence and invocation paths.

### Step 0 — Current architecture (entrypoints + stages)

- **Entrypoints**
  - **CLI (v2)**: `src/anime_v2/cli.py:110-260` (Click command `cli`)
  - **API/UI (v2)**: `src/anime_v2/server.py:166-228` (FastAPI app + routers), plus UI templates under `src/anime_v2/web/`
  - **Legacy root server**: `main.py` (FastAPI “Anime Dubbing Server” wrapper around the v2 CLI)
- **High-level stage flow (v2)**
  - extract audio → diarize (optional) → transcribe (Whisper) → translate (optional) → TTS → mix/mux → outputs under `Output/<stem>/...`
  - Evidence: `src/anime_v2/cli.py:238-260` (output layout + stage files), `src/anime_v2/jobs/queue.py` (server-side orchestrator).
- **Config system**
  - Centralized settings layer: `config/public_config.py`, `config/secret_config.py`, merged at `config/settings.py` with a safe report API.

---

### Table

| Feature | Status | Evidence (paths + symbols) | How to run it | Notes / limitations |
|---|---|---|---|---|
| F1 Voice cloning / speaker preservation | **Present** | `src/anime_v2/stages/tts.py:131-206` (speaker wav overrides + per-speaker strategy), `src/anime_v2/stages/tts.py:244-267` (rep wav from diarization), `src/anime_v2/stages/tts.py:351-401` (XTTS clone + preset fallback), `src/anime_v2/web/routes_jobs.py` (voice map + upload speaker wav to job) | CLI: `anime-v2 ...` (uses diarization + speaker wavs). API: `/api/jobs/{id}/characters` + `/api/jobs/{id}/transcript/synthesize` | Uses XTTS clone when reference wav exists; falls back to preset voices / basic TTS / espeak. |
| F2 Emotion transfer / expressive control | **Present** | `src/anime_v2/stages/tts.py` (`_apply_prosody_ffmpeg`, `emotion_mode`), `src/anime_v2/cli.py` (`--emotion-mode`, `--speech-rate`, `--pitch`, `--energy`) | CLI: `anime-v2 ... --emotion-mode auto` or `--emotion-mode tags` (e.g. `[happy] ...`) | Best-effort offline heuristics + ffmpeg filters; never required. |
| F3 Real-time / streaming dubbing | **Partial** | `src/anime_v2/realtime.py` (`realtime_dub`), `src/anime_v2/cli.py` (`--realtime`, `--chunk-seconds`, `--chunk-overlap`) | CLI: `anime-v2 ... --realtime --chunk-seconds 20 --chunk-overlap 2` | Implements pseudo-streaming (chunked) with per-chunk artifacts + optional stitched outputs; not true live streaming. |
| F4 Web UI / API support | **Present** | `src/anime_v2/server.py:166-228` (FastAPI app + routers), `src/anime_v2/web/app.py` (web app), `src/anime_v2/web/routes_jobs.py:241+` (job APIs), `src/anime_v2/web/routes_ui.py` (UI routes) | `anime-v2-web` or run server module, then use `/api/*` and UI pages | Auth + RBAC + CSRF exist; API supports jobs, batch, transcript editing, resynthesis, logs, metrics, etc. |
| F5 Multi-language support | **Present** | `src/anime_v2/cli.py:124-133` (`--src-lang`, `--tgt-lang`), `src/anime_v2/stages/translation.py` (translation config supports multiple engines), `config/public_config.py` (multilingual TTS defaults) | CLI: `anime-v2 ... --src-lang auto --tgt-lang es` (example) | Translation engines are pluggable (whisper/marian/nllb) and fallback logic exists. |
| F6 Timing & alignment precision | **Partial** | `src/anime_v2/stages/align.py:47-110` (`retime_tts` w/ cap + pad/trim), `src/anime_v2/stages/align.py:163+` (`realign_srt` via VAD/aeneas/heuristic), `src/anime_v2/cli.py:171-183` (`--aligner`, `--max-stretch`) | CLI: `anime-v2 ... --aligner auto --max-stretch 0.15` | Segment-level alignment + time-stretch exists; **word-level timestamps** are not guaranteed/exposed end-to-end yet. |
| F7 Subtitle generation (SRT/VTT) | **Present** | `src/anime_v2/utils/subtitles.py` (`write_srt`, `write_vtt`), `src/anime_v2/cli.py` (`--subs`, `--subs-format`) | CLI: `anime-v2 ... --subs both --subs-format both` | SRT is default; VTT is optional. |
| F8 Batch processing | **Present** | API batch endpoint: `src/anime_v2/web/routes_jobs.py:462-566` (`create_jobs_batch`), CLI batch: `src/anime_v2/cli.py` (`--batch`, `--jobs`, `--resume`, `--fail-fast`) + worker `src/anime_v2/batch_worker.py` | CLI: `anime-v2 --batch 'Input/*.mp4' --jobs 2 --resume` | CLI batch uses isolated worker processes for safety. |
| F9 Model selection & fine-tuning hooks | **Partial** | `src/anime_v2/runtime/model_manager.py` (cache + allocator), `src/anime_v2/cli.py` (`--asr-model`, `--mt-provider`, `--tts-provider`), `src/anime_v2/stages/tts.py` (`tts_provider`, `voice_mode`), `scripts/train_voice.py` (training hook placeholder) | CLI: `anime-v2 ... --asr-model large-v3 --mt-provider nllb --tts-provider basic` | Provider abstraction is lightweight (selection knobs + hooks), not a full plugin package with external entrypoints. |

---

### Per-feature notes (2–5 bullets each)

#### F1 Voice cloning / speaker preservation — Present
- Uses **per-speaker reference wav** (from diarization segments or explicit overrides) and an XTTS engine wrapper.
- Falls back to **preset voice IDs** (voice bank map / similarity match) and then to **basic TTS** / **espeak**.
- Evidence: `src/anime_v2/stages/tts.py:150-205` (voice maps + strategies).

#### F2 Emotion transfer — Present
- Adds best-effort prosody controls (rate/pitch/energy) and offline heuristics (`auto`) or simple tags (`tags`).
- Implemented as an optional post-processing layer over synthesized clips (never required).

#### F3 Realtime/streaming dubbing — Partial
- Adds a **chunked “pseudo-streaming”** mode that produces per-chunk artifacts and an optional stitched track + subtitles.
- Does not implement true low-latency live audio/video streaming with incremental mux.

#### F4 Web UI/API — Present
- FastAPI server exposes job lifecycle APIs, batch submission, transcript editing, log streaming, and a browser UI.

#### F5 Multi-language — Present
- CLI accepts `src_lang`/`tgt_lang`; translation stage supports multiple engines with fallbacks.

#### F6 Timing & alignment — Partial
- Time-stretch is implemented (`retime_tts`) with caps + pad/trim.
- Optional SRT realignment exists (VAD anchoring; aeneas optional).
- Missing: a first-class “word-level timestamp” pipeline output (when Whisper can provide it).

#### F7 Subtitles — Present
- SRT remains the default; VTT export is available and controlled via CLI.

#### F8 Batch — Present
- Batch exists via API and CLI (folder/glob), with `--jobs` concurrency implemented using isolated worker processes.

#### F9 Providers/tuning hooks — Partial
- Model manager supports caching and device selection; config supports prewarm.
- Missing: provider abstraction + documented fine-tuning hooks.

