## Dubbing-Pipeline (Anime dubbing pipeline, v2-first)

This repository is an **offline-first dubbing pipeline**:

- **CLI** for running dubbing jobs locally: `anime-v2`
- **FastAPI server + web UI** for job submission, monitoring, transcript editing, and playback: `anime-v2-web`
- **Outputs** go to `Output/<video_stem>/...` (intermediate artifacts + final `dub.mkv` / `dub.mp4`)

### Architecture (high level)

```text
            +--------------------+
Input video |   CLI / API job    |
   (.mp4)   |  submission layer  |
            +---------+----------+
                      |
                      v
            +--------------------+
            |  Extract audio     |  -> Output/<stem>/audio.wav
            +--------------------+
                      |
                      v
            +--------------------+
            |   (Optional)       |
            |  diarization       |  -> diarization.json + speaker refs
            +--------------------+
                      |
                      v
            +--------------------+
            |   ASR (Whisper)    |  -> <stem>.srt + <stem>.json (segments_detail)
            +--------------------+
                      |
                      v
            +--------------------+
            | (Optional) MT      |  -> translated.json + <stem>.translated.srt
            +--------------------+
                      |
                      v
            +--------------------+
            |   TTS (XTTS/etc)   |  -> <stem>.tts.wav + clips + manifest
            +--------------------+
                      |
                      v
            +--------------------+
            |   Mix / mux        |  -> dub.mkv / dub.mp4 (+ optional subs)
            +--------------------+
```

---

## Feature list (F1–F9)

- **F1 Voice cloning / speaker preservation (optional)**: XTTS cloning when reference audio is available; fallbacks to presets/basic/espeak.
- **F2 Emotion transfer / expressive controls (optional)**: best-effort `emotion_mode` + (rate/pitch/energy) post-processing.
- **F3 Realtime / streaming dubbing (optional)**: **pseudo-streaming** chunk mode (`--realtime`) producing chunk artifacts + optional stitched outputs.
- **F4 Web UI / API support**: FastAPI server with job APIs, auth/RBAC/CSRF, UI pages, transcript editing, resynthesis.
- **F5 Multi-language**: `--src-lang` + `--tgt-lang`, with multiple MT engines + fallbacks.
- **F6 Timing & alignment precision (optional)**: segment timing + retime/pad/trim; optional aeneas alignment; optional Whisper word timestamps when supported.
- **F7 Subtitle generation**: SRT default; VTT optional (`--subs-format vtt|both`).
- **F8 Batch processing**: API batch endpoint + CLI `--batch` (folder/glob) with `--jobs` concurrency.
- **F9 Model selection & fine-tuning hooks (optional)**: model cache/allocator; `--asr-model`, `--mt-provider`, `--tts-provider`; placeholder training hook (`scripts/train_voice.py`).

Canonical audit: `docs/feature_audit.md`.

---

## Quickstart (local)

### Prereqs

- Python **3.10+**
- `ffmpeg` + `ffprobe`

### Install

```bash
python3 -m pip install -e .
```

Optional extras (recommended on a real machine with enough disk/RAM):

```bash
python3 -m pip install -e ".[translation,diarization,tts,dev]"
```

### Verify wiring (recommended)

```bash
python3 scripts/verify_config_wiring.py
python3 scripts/verify_features.py
python3 scripts/smoke_import_all.py
python3 scripts/smoke_run.py
python3 scripts/verify_runtime.py
```

### Run a single dub job (CLI)

```bash
anime-v2 Input/Test.mp4 --mode medium --device auto
```

Override ASR model directly:

```bash
anime-v2 Input/Test.mp4 --asr-model large-v3 --device auto
```

### Batch folder/glob (CLI)

```bash
anime-v2 --batch "Input/*.mp4" --jobs 2 --resume
```

### Multi-language examples (CLI)

```bash
anime-v2 Input/Test.mp4 --src-lang auto --tgt-lang es
anime-v2 Input/Test.mp4 --no-translate --src-lang ja
```

Force MT provider:

```bash
anime-v2 Input/Test.mp4 --mt-provider nllb --src-lang ja --tgt-lang en
anime-v2 Input/Test.mp4 --mt-provider none
```

### Subtitles (SRT/VTT)

```bash
anime-v2 Input/Test.mp4 --subs both --subs-format both
```

### Voice controls

```bash
# force single narrator voice
anime-v2 Input/Test.mp4 --voice-mode single

# prefer preset selection (skip cloning)
anime-v2 Input/Test.mp4 --voice-mode preset

# hint cloning references from a folder: <dir>/<speaker_id>.wav
anime-v2 Input/Test.mp4 --voice-mode clone --voice-ref-dir data/voices
```

### Expressive controls

```bash
anime-v2 Input/Test.mp4 --emotion-mode auto
anime-v2 Input/Test.mp4 --emotion-mode tags
```

### Optional Dialogue Isolation (Demucs)

Defaults preserve current behavior (**no separation**). To isolate dialogue and keep BGM/SFX:

```bash
# enable enhanced mixing + demucs stems (requires: pip install -e ".[mixing]")
anime-v2 Input/Test.mp4 --mix enhanced --separation demucs
```

Artifacts:

- `Output/<stem>/stems/dialogue.wav`
- `Output/<stem>/stems/background.wav`
- `Output/<stem>/audio/final_mix.wav`

### Enhanced Mixing (LUFS + Ducking)

```bash
anime-v2 Input/Test.mp4 --mix enhanced --lufs-target -16 --ducking --ducking-strength 1.0 --limiter
```

### Timing Fit & Pacing (Tier‑1 B/C)

Defaults preserve current behavior. To enable timing-aware translation fitting + segment pacing:

```bash
anime-v2 Input/Test.mp4 --timing-fit --pacing --wps 2.7 --tolerance 0.10 --pacing-min-stretch 0.88 --pacing-max-stretch 1.18
```

Notes:

- `--min-stretch` is an alias for `--pacing-min-stretch`
- `--pace-max-stretch` is an alias for `--pacing-max-stretch`
- `--max-stretch` (without the `pacing-` prefix) is the **legacy retime** bound used by the aligner path.

Debug artifacts (per segment):

```bash
anime-v2 Input/Test.mp4 --timing-fit --pacing --timing-debug
```

This writes:

- `Output/<stem>/segments/0000.json` (start/end, pre-fit/fitted text, pacing actions)

### Character Voice Memory (Tier‑2 A)

Opt-in feature to keep **stable character identities across episodes** by persisting per-character reference WAVs + embeddings under `data/voice_memory/`.

Enable:

```bash
anime-v2 Input/Test.mp4 --voice-memory on --voice-match-threshold 0.75 --voice-auto-enroll
```

Management:

```bash
anime-v2 --list-characters
anime-v2 --rename-character SPEAKER_01 "Zoro"
anime-v2 --set-character-voice-mode SPEAKER_01 clone
anime-v2 --set-character-preset SPEAKER_01 alice
```

Reset:
- Delete `data/voice_memory/` to rebuild from scratch.

Notes:
- When `--voice-memory on` is enabled, the pipeline avoids the legacy encrypted `CharacterStore` (no key required).
- If optional embedding deps are missing, voice memory falls back to a lightweight offline fingerprint (still deterministic).

### Review Loop (Tier‑2 B)

Edit per-segment text, regenerate audio for a single segment, preview, and lock it so future full-job re-runs reuse the locked audio/text.

Initialize state (after running the pipeline at least once):

```bash
anime-v2 review init Input/Test.mp4
```

Inspect and edit:

```bash
anime-v2 review list <job>
anime-v2 review show <job> 12
anime-v2 review edit <job> 12 --text "Edited line…"
```

Regenerate + preview + lock:

```bash
anime-v2 review regen <job> 12
anime-v2 review play <job> 12
anime-v2 review lock <job> 12
```

Render final review output (rebuild episode audio from current per-segment WAVs and best-effort remux):

```bash
anime-v2 review render <job>
```

Artifacts:
- `Output/<job>/review/state.json`
- `Output/<job>/review/audio/<segment_id>_vN.wav`
- `Output/<job>/review/review_render.wav` (+ `dub.review.mkv` when mux succeeds)

Interaction with the existing transcript editor:
- The older “approve + resynthesize” flow is preserved for compatibility, but approved-only resynth now also **writes/locks** those segments into `Output/<job>/review/state.json` so the review loop is the canonical lock store.

### Lip-sync plugin (Tier‑3 A, optional)

Optional post-processing step that can produce a lip-synced video using a local Wav2Lip install. **Off by default**.

Setup (offline):
- Place the Wav2Lip repo at `third_party/wav2lip/` (or set `WAV2LIP_DIR`)
- Provide a local checkpoint file and point `WAV2LIP_CHECKPOINT` to it (e.g. `MODELS_DIR/wav2lip/wav2lip.pth`)

Run:

```bash
anime-v2 Input/Test.mp4 --lipsync wav2lip --wav2lip-checkpoint /models/wav2lip/wav2lip.pth
```

Output:
- `Output/<job>/final_lipsynced.mp4` (when successful)

Notes:
- If Wav2Lip is missing, the job continues without lipsync unless `--strict-plugins` is set.

### Expressive / Emotion Transfer (Tier‑3 B, optional)

Opt-in per-segment expressive prosody guidance. **Off by default**.

Modes:
- `--expressive off`: disabled (default)
- `--expressive text-only`: punctuation-driven heuristics
- `--expressive source-audio`: best-effort analysis of the **source audio segment** (RMS + optional pitch if `librosa` is installed)
- `--expressive auto`: currently treated as a safe default (uses text-only unless source audio is provided)

Artifacts (when `--expressive-debug`):
- `Output/<job>/expressive/plans/<segment_id>.json`

Timing interaction:
- Expressive controls are conservative and then Tier‑1 pacing still enforces segment duration when `--pacing` is enabled.

Example:

```bash
anime-v2 Input/Test.mp4 --timing-fit --pacing --expressive source-audio --expressive-strength 0.5 --expressive-debug
```

### Pseudo-streaming (chunk mode)

```bash
anime-v2 Input/Test.mp4 --realtime --chunk-seconds 20 --chunk-overlap 2 --stitch
```

### Streaming Mode (Tier‑3 C, optional)

Chunked dubbing mode that produces playable per-chunk MP4s under `Output/<job>/stream/` plus a manifest. **Off by default**.

Enable (segments output):

```bash
anime-v2 Input/Test.mp4 --stream on --chunk-seconds 10 --overlap-seconds 1 --stream-output segments
```

Enable (stitched final MP4):

```bash
anime-v2 Input/Test.mp4 --stream on --chunk-seconds 10 --overlap-seconds 1 --stream-output final
```

Artifacts:
- `Output/<job>/chunks/` (mono16k chunk wavs)
- `Output/<job>/stream/manifest.json`
- `Output/<job>/stream/chunk_001.mp4`, `chunk_002.mp4`, ...
- `Output/<job>/stream/stream.final.mp4` (when `--stream-output final`)

API (when using `anime-v2-web`):
- `GET /api/jobs/{id}/stream/manifest`
- `GET /api/jobs/{id}/stream/chunks/{n}`

Notes:
- `--realtime` remains as a backwards-compatible alias for chunked mode.
- Streaming mode is offline-first; it may fall back to silence chunks in degraded environments.

Troubleshooting:
- If you see missing chunk outputs, verify `ffmpeg`/`ffprobe` are installed and on PATH.
- Chunk MP4s are **re-encoded** (baseline H.264) for concat compatibility; this is slower but robust.
- If Whisper/MT/TTS deps are missing, streaming runs in degraded mode (silence chunks) rather than crashing.

### Preflight / dry-run

```bash
anime-v2 Input/Test.mp4 --dry-run
```

### Logging verbosity

```bash
anime-v2 Input/Test.mp4 --verbose
anime-v2 Input/Test.mp4 --debug
```

Artifacts land in:

- `Output/<stem>/audio.wav`
- `Output/<stem>/diarization.json` (optional)
- `Output/<stem>/<stem>.srt` (+ optional `<stem>.vtt`)
- `Output/<stem>/translated.json` + `<stem>.translated.srt` (+ optional `.vtt`)
- `Output/<stem>/<stem>.tts.wav`
- `Output/<stem>/dub.mkv`
- `Output/<stem>/realtime/` (when `--realtime`)

---

## Quickstart (Docker)

### Build

```bash
docker build -f docker/Dockerfile -t dubbing-pipeline .
```

### Run (CLI)

```bash
docker run --rm \
  -v "$(pwd)":/app \
  -v "$(pwd)/Input":/app/Input \
  -v "$(pwd)/Output":/app/Output \
  dubbing-pipeline anime-v2 /app/Input/Test.mp4 --mode low --device cpu
```

### Run (web server)

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
docker compose -f docker/docker-compose.yml up --build
```

---

## Configuration (public vs secrets)

This project uses a split settings model:

- **Public config** (committed): `config/public_config.py` (overridable via `.env`)
- **Secret config** (template committed; secrets live in env / `.env.secrets`): `config/secret_config.py`
- **Unified access**: `config/settings.py` (`SETTINGS` / `get_settings()`)

Setup:

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
```

Minimum secrets for web usage:

- `API_TOKEN` (set in `.env.secrets`)

Safety:

- `.env` and `.env.secrets` are gitignored.
- Use `STRICT_SECRETS=1` in production to enforce required secrets.

---

## API usage (FastAPI)

- Start server: `anime-v2-web`
- Login: `http://<host>:8000/login?token=<API_TOKEN>`
- Primary endpoints:
  - `POST /api/jobs` (submit)
  - `POST /api/jobs/batch` (batch submit)
  - `GET /api/jobs/{id}` (status)
  - `GET /api/jobs/{id}/result` (download)
  - `GET/PUT /api/jobs/{id}/transcript` (edit transcript)
  - `POST /api/jobs/{id}/transcript/synthesize` (resynthesize from edited transcript)

---

## Performance notes

- **CPU**: works, but slow for Whisper + XTTS.
- **CUDA**: recommended for high throughput; `--device auto` will use it when available.
- Model sizes:
  - `--mode low` → fastest, lower quality
  - `--mode high` → slowest, best quality

---

## Security notes

- **Do not expose the server publicly without HTTPS.**
- Secrets never live in git; use `.env.secrets` and keep it local.
- Use strong `API_TOKEN`; request logs intentionally omit query strings.

---

## Troubleshooting

### CUDA / GPU not detected

- Docker: ensure you run with `--gpus all` and have NVIDIA drivers + container toolkit installed.
- Local: try `--device cpu` to confirm the pipeline works, then revisit CUDA setup.

### Model downloads are slow / fail

- First run downloads large models (Whisper + XTTS). Ensure disk space and a stable connection.
- Use `HF_HOME`, `TORCH_HOME`, and `TTS_HOME` to relocate caches.

### Coqui TTS Terms of Service (required)

The Coqui XTTS engine requires explicit acknowledgement:

- `COQUI_TOS_AGREED=1`

Without it, the TTS layer refuses to synthesize.

### Docker `numpy==1.22.0` pin rationale

The Docker constraints pin `numpy==1.22.0` to satisfy **wheel compatibility for `TTS==0.22.0` on Python 3.10** in a reproducible way.

### No subtitles in output

- If ASR produces an empty SRT (e.g. Whisper not installed/downloaded), muxing skips subtitles to avoid ffmpeg errors on empty SRT.
- If you want to skip subs intentionally, pass `--no-subs`.

---

## Contribution / dev

```bash
make check
python3 -m ruff check --fix
python3 -m black .
python3 -m pytest -q
```

## CHANGELOG (hardening pass)

- **Config/tooling consistency**: removed remaining hardcoded `ffmpeg/ffprobe` literals; everything flows through settings + shared helpers.
- **FFmpeg robustness**: improved `anime_v2.utils.ffmpeg_safe` with retries/timeout support and better error messages.
- **File I/O safety**: added atomic write/copy helpers and used them for critical artifacts (e.g., realtime manifests, job SRT propagation).
- **CLI operability**: added `--dry-run`, `--verbose`, `--debug` for safer preflight and better diagnostics without changing defaults.
- **Runtime verification**: added `scripts/verify_runtime.py` for config + tool + filesystem checks (safe config report only).


- **Retention**: `RETENTION_DAYS_INPUT` (default `7`) best-effort purges old raw uploads from `Input/uploads/`. `RETENTION_DAYS_LOGS` (default `14`) purges old files from `logs/` and old per-job `Output/**/job.log`.
  - Run manually: `python -m anime_v2.ops.retention`

- **Backups**: creates a metadata-only zip (no large media) under `backups/`:
  - Includes: `data/**`, `Output/*.db`, `Output/**/{*.json,*.srt,job.log}`
  - Run manually: `python -m anime_v2.ops.backup`
  - Output: `backups/backup-YYYYmmdd-HHMM.zip` and `backups/backup-YYYYmmdd-HHMM.manifest.json` (with per-file SHA256).
  - **Restore**: unzip into the repo/app root (so paths like `data/...` and `Output/...` land in place), then verify file hashes against the manifest.