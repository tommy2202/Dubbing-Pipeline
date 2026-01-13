## Features (core + optional)

This page is a “source of truth” list of **what the repo does today**, how to enable each feature, and where outputs show up.

Key principle: **optional features are opt-in** or degrade gracefully when dependencies are missing.

---

## Core pipeline (always available)

- **Input**: local video files (MP4/MKV/MOV/WebM).
- **Outputs (canonical)**: written under `Output/<stem>/` (see `docs/OVERVIEW.md`).
- **Library grouping (mirror)**: best-effort mirror under `Output/Library/<series>/season-XX/episode-YY/job-<job_id>/`.
  - A `manifest.json` is written (best-effort) containing playback URLs/paths and metadata.
  - If links cannot be created (Windows symlink restrictions), the Library folder still contains a manifest + pointer files.
- **Core stages**:
  - audio extraction
  - ASR (speech-to-text)
  - TTS (text-to-speech)
  - mix + mux to outputs

### Outputs
- `Output/<stem>/<stem>.dub.mkv`
- `Output/<stem>/<stem>.dub.mp4`
- `Output/<stem>/job.log`
- `Output/<stem>/.checkpoint.json` (best-effort resume metadata)
- `Output/<stem>/manifest.json` (best-effort library manifest fallback)
- `Output/Library/.../manifest.json` (preferred grouped library manifest)

### Required metadata for web submissions
New job submissions via the web/API require:
- **Series name** (`series_title`)
- **Season** (`season_text` / `season_number`)
- **Episode** (`episode_text` / `episode_number`)

These are normalized and stored as:
- `series_title` (trimmed)
- `series_slug` (normalized)
- `season_number` (int)
- `episode_number` (int)

---

## Modes: high / medium / low

- **What it does**: presets that trade quality vs speed.
- **How to enable**:
  - CLI: `--mode high|medium|low` (default `medium`)
  - Web: “Mode” dropdown in Upload Wizard
- **Fallbacks**: when optional deps are missing, the job still runs but may be “degraded” (the job metadata tracks degraded reasons).

---

## Languages: ASR + translation + subtitles

### ASR (transcription)
- **What it does**: produces a transcript for the source audio.
- **How to enable**: on by default (CLI `run`).
- **Controls**:
  - `--asr-model <name>` (e.g. `large-v3`, `medium`)
  - `--src-lang auto|<code>`
- **Outputs** (typical):
  - `Output/<stem>/<stem>.srt` (when subtitles are enabled)
  - JSON segment metadata under `Output/<stem>/...` (varies by run)

### Translation (optional)
- **What it does**: translates the transcript to the target language.
- **How to enable**: default is enabled (target language defaults to `en`).
- **Controls**:
  - `--tgt-lang <code>` (default `en`)
  - `--no-translate` (force off)
  - `--mt-provider auto|whisper|marian|nllb|none`
- **Outputs**:
  - `Output/<stem>/<stem>.translated.srt` (when enabled)
  - `Output/<stem>/translated.json` (when enabled)

### Subtitle formats
- **What it does**: writes SRT and/or VTT files to disk; muxing into the final container is separate.
- **Controls**:
  - `--subs off|src|tgt|both` (default `both`)
  - `--subs-format srt|vtt|both` (default `srt`)
  - `--no-subs` (do not mux subtitles into output container)

### External subtitle/transcript import (web feature)
- **What it does**: you can submit a job with external SRT/JSON so the pipeline can skip ASR and/or translation when possible.
- **How to enable**: Upload Wizard “imports” fields (SRT/JSON); stored under `Input/imports/<job_id>/...`.
- **Where it shows**:
  - job runtime metadata records `skipped_stages`
  - generated SRT/JSON artifacts appear under `Output/<stem>/`

---

## Speaker diarization (optional)

- **What it does**: detects “who spoke when”, used to stabilize speaker identity and enable per-speaker voices.
- **Controls**:
  - `--diarizer auto|pyannote|speechbrain|heuristic|off`
- **Fallbacks**:
  - when optional diarization dependencies/models are missing, `auto` falls back to a lighter heuristic path or can be turned off.
- **Outputs**:
  - `Output/<stem>/diarization.json` (when enabled)

---

## Voices: TTS, voice modes, and voice memory

### TTS provider selection (optional)
- **Controls**:
  - `--tts-provider auto|xtts|basic|espeak`
- **Fallbacks**:
  - if advanced TTS isn’t available, the pipeline degrades to simpler voices (depending on installed deps).

### Voice modes (speaker strategy)
- **Controls**:
  - `--voice-mode clone|preset|single` (default `clone`)
  - `--voice-ref-dir <path>` (reference WAVs by speaker id)
  - `--voice-store <path>` (persistent storage for voice refs, if used)

### Character voice memory (optional; cross-episode consistency)
- **What it does**: keeps a persistent “character → voice identity” map across jobs to reduce drift.
- **Controls**:
  - `--voice-memory off|on` (default `off`)
  - `--voice-memory-dir <path>` (optional override)
- **CLI tools**:
  - `anime-v2 voice list`
  - `anime-v2 voice audition --text "..." [--character <id>]`
  - `anime-v2 voice merge FROM_ID TO_ID [--move-refs] [--keep-alias]`
  - `anime-v2 voice undo-merge <merge_id>`

### Per-character tuning (optional)
- **What it does**: per-character voice/prosody preferences for the pipeline.
- **CLI** (`anime-v2 character ...`):
  - `set-voice-mode <character_id> clone|preset|single`
  - `set-rate <character_id> <rate_mul>`
  - `set-style <character_id> default|tight|normal|dramatic`
  - `set-expressive <character_id> <strength>`

---

## Timing fit + pacing (optional)

- **What it does**: rewrites or adjusts translations to fit timing constraints better.
- **Controls**:
  - `--timing-fit/--no-timing-fit` (default off)
  - `--pacing/--no-pacing` (default off)
  - `--wps`, `--tolerance`, `--pacing-min-stretch`, `--pacing-max-stretch`
  - `--rewrite-provider heuristic|local_llm`
  - `--rewrite-endpoint http://127.0.0.1:<port>/...` (local-only)
- **Fallbacks**:
  - default is deterministic heuristics
  - local LLM mode is optional and requires explicit configuration

---

## Mixing: separation + LUFS + ducking + limiter (optional)

- **Controls**:
  - `--mix legacy|enhanced` (default `legacy`)
  - `--separation off|demucs` (default `off`)
  - `--lufs-target`, `--ducking/--no-ducking`, `--ducking-strength`, `--limiter/--no-limiter`
- **Fallbacks**:
  - if Demucs isn’t installed/configured, separation is skipped unless strict plugins are requested.
- **Outputs** (when enabled):
  - stems and intermediate wavs under `Output/<stem>/stems/` and `Output/<stem>/audio/`

---

## Music/singing preservation + OP/ED detection (optional)

- **What it does**: detects music/singing regions so they can be preserved/handled differently.
- **Controls**:
  - `--music-detect off|on` (default off)
  - `--music-mode auto|heuristic|classifier`
  - `--op-ed-detect off|on` (default off), `--op-ed-seconds <N>`
- **Overrides (CLI + web)**: see “Overrides” below.

---

## Expressive prosody + “Dub Director” (optional)

- **Controls**:
  - `--emotion-mode off|auto|tags`
  - `--expressive off|auto|source-audio|text-only`
  - `--expressive-strength <0..1>`
  - `--director off|on`, `--director-strength <0..1>`
- **Outputs**:
  - when debug is enabled: `Output/<stem>/expressive/...`

---

## Lip-sync (optional plugin)

- **Controls**:
  - `--lipsync off|wav2lip` (default off)
  - `--strict-plugins` to fail if the plugin isn’t available
- **CLI tool**:
  - `anime-v2 lipsync preview <video>`
- **Fallbacks**:
  - if deps aren’t installed, web/API may return `503 WebRTC deps not installed` / lip-sync is skipped unless strict.

---

## Multi-track outputs (optional)

- **What it does**: produces additional track artifacts and can mux a multi-audio MKV.
- **Controls**:
  - `--multitrack off|on` (default off)
  - `--container mkv|mp4` (default `mkv`)
- **Outputs**:
  - `Output/<stem>/audio/tracks/*` (track wavs; plus `.m4a` sidecars when container is mp4)

---

## QA scoring + reports (optional)

- **What it does**: runs offline checks and produces a score and issue list.
- **Controls**:
  - CLI run: `--qa off|on` (default off)
  - CLI tools: `anime-v2 qa run <job>` and `anime-v2 qa show <job>`
- **Outputs**:
  - `Output/<stem>/qa/summary.json`
  - `Output/<stem>/qa/*` (issue details)
- **Web UI**:
  - “Quality” tab shows score + top issues and deep-links to the editor (“Fix”).

---

## Review/edit loop (web + CLI tools)

- **What it does**: lets you edit per-segment text, regenerate audio, preview, and lock/unlock segments.
- **Web UI**:
  - Job page “Review / Edit” tab
  - Quick helpers: shorten 10%, formal, reduce slang, apply PG style (best-effort; uses the configured rewrite provider)
- **CLI** (`anime-v2 review ...`):
  - `init <input_video>`
  - `list <job>`
  - `show <job> <segment_id>`
  - `edit <job> <segment_id> --text "..." `
  - `regen <job> <segment_id>`
  - `play <job> <segment_id>`
  - `lock|unlock <job> <segment_id>`
  - `render <job>`

---

## Overrides (music regions + speaker overrides)

- **What it does**: lets you override music/singing regions and force speaker/character IDs per segment.
- **Web UI**: “Overrides” tab.
- **CLI**:
  - `anime-v2 overrides music add|edit|remove|list ...`
  - `anime-v2 overrides speaker set|unset ...`
  - `anime-v2 overrides apply <job>`

---

## Web UI + mobile features

- **Job submission**:
  - chunked/resumable uploads (`/api/uploads/*`)
  - server-local file picker (restricted) (`/api/files`)
  - bounded job queue + progress + cancel/kill
- **Playback**:
  - master outputs (MKV/MP4) with HTTP Range support
  - mobile-friendly artifacts:
    - `Output/<stem>/mobile/mobile.mp4` (**enabled by default**, controlled by `MOBILE_OUTPUTS`)
    - `Output/<stem>/mobile/hls/` (**optional**, controlled by `MOBILE_HLS`)
  - “Open in VLC” links on job pages
- **Library management**:
  - list/search/filter jobs, tags, archive/unarchive, delete (role-gated)
- **Model management (admin)**:
  - view disk usage + loaded models; optional prewarm (default off)

Details: `docs/WEB_MOBILE.md`

---

## Security, privacy, and ops (server)

These features are designed to keep remote/mobile usage safe.

- **Auth**: username/password, access+refresh tokens, cookie sessions + CSRF, strict CORS, rate limits
- **RBAC**: viewer/operator/editor/admin roles + scoped API keys
- **Remote access modes**: opt-in allowlists; proxy-safe forwarded header trust only in Cloudflare mode
- **Audit logging**: append-only JSONL security events with payload scrubbing
- **Encryption at rest (optional)**: AES-GCM for selected artifact classes (fails safe if key missing)
- **Privacy mode (optional)**: minimize stored intermediates; redaction in notifications/logging; retention automation
- **Retention**:
  - per-job retention policy: `--cache-policy full|balanced|minimal`
  - best-effort global purges (old uploads/logs) via ops script

Details: `docs/SETUP.md` and `docs/security.md`

---

## Notifications (optional; private)

- **What it does**: sends job completion/failure notifications to a private self-hosted `ntfy` server.
- **Default**: off.
- **How to enable**: configure `NTFY_*` env vars and secrets.

Details: `docs/notifications.md`

