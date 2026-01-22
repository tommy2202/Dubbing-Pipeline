## Dubbing Pipeline (offline-first dubbing)

This repository is an **offline-first dubbing pipeline** for turning a source video into a dubbed output with a practical “edit → regen → review” loop.

It ships with:
- **CLI** for local processing: `dubbing-pipeline`
- **FastAPI server + web UI** for mobile-friendly job submission, progress monitoring, QA, editing, and playback: `dubbing-web`

Outputs are written under:
- `Output/<stem>/...` (canonical)
- plus a stable per-job pointer for web jobs at `Output/jobs/<job_id>/target.txt`

### What’s New (current feature-complete stack)
- **Mobile/web job submission**: resumable chunked uploads + server-file picker fallback, queue/progress/cancel, job pages with tabs.
- **Mobile playback**: auto-selected mobile-friendly MP4 + optional HLS + “Open in VLC” links.
- **QA + review loop**: QA scoring, actionable “Fix” deep-links, segment edit/regen/preview/lock, overrides for music regions + speakers.
- **Remote access (opt-in)**: Tailscale primary, Cloudflare Tunnel + Access optional, proxy-safe behavior.
- **Security & privacy**: cookie sessions + CSRF, strict CORS, rate limits, RBAC + scoped API keys, audit logging, optional encryption-at-rest, privacy mode + retention.
- **Ops**: model cache status + optional prewarm, library management (tags/archive/delete), verification gates.

Before inviting others, run:

```bash
python3 scripts/v0_gate.py
```

### Documentation index (start here)
- **Quickstart (Tailscale recommended)**: `docs/GOLDEN_PATH_TAILSCALE.md`
- Clean setup guide (brand-new computer): `docs/CLEAN_SETUP_GUIDE.txt`
- Overview & architecture: `docs/OVERVIEW.md`
- Full feature list (defaults + optional + outputs): `docs/FEATURES.md`
- Setup (local + Docker + GPU + security): `docs/SETUP.md`
- CLI reference + recipes: `docs/CLI.md`
- Web/mobile guide (LAN + remote): `docs/WEB_MOBILE.md`
- Troubleshooting: `docs/TROUBLESHOOTING.md`

Additional focused docs:
- Remote access (advanced): `docs/advanced/README.md`
- Private notifications (ntfy): `docs/notifications.md`
- Security details: `docs/security.md`

---

## Quickstart (Tailscale recommended)

If you want to safely access the web UI from your phone on mobile data **without** exposing your machine publicly:

- Follow: `docs/GOLDEN_PATH_TAILSCALE.md`
- Run:

```bash
./scripts/run_prod.sh
```

Windows:

```powershell
.\scripts\run_prod.ps1
```

This starts the server in **Tailscale mode** (IP allowlist enforced) and prints the exact **tailnet URL** to open.

---

## Quickstart (local)

### Prereqs

- Python **3.10+**
- `ffmpeg` + `ffprobe`

### Install

```bash
python3 -m pip install -e .
```

---

## Developer quickstart

```bash
python3 -m pip install -e ".[dev]"
pytest -q
python3 scripts/polish_gate.py  # v0 gate
```

Notes:
- Some tests are skipped when optional dependencies (e.g., whisper) are missing.

### Runtime directories (important)

- `Input/` and `Output/` are **runtime folders**. They are intentionally kept **empty in git** (only `.gitkeep`) and must **not** be committed.
- If you need a tiny MP4 for local testing, generate one offline and copy it into `Input/`:

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=size=320x180:rate=10" \
  -f lavfi -i "sine=frequency=440:sample_rate=44100" \
  -t 2.0 \
  -c:v libx264 -pix_fmt yuv420p \
  -c:a aac \
  samples/Test.mp4

cp samples/Test.mp4 Input/Test.mp4
```

---

## Web UI authentication (mobile-safe)

- **Login page**: `/login` (alias) or `/ui/login` (canonical).
- **Browser sessions**:
  - `POST /api/auth/login` supports `session=true` to set a signed `session` cookie (recommended for UI).
  - CSRF is enforced for state-changing requests via the `csrf` cookie + `X-CSRF-Token` header.
- **API clients**:
  - use `Authorization: Bearer <access_token>` from the login response JSON.
  - Optional scoped API keys can be managed by admins under `/keys/*` (can be disabled via `ENABLE_API_KEYS=0`).
- **Legacy (unsafe) token-in-URL**:
  - Disabled by default.
  - Can be enabled for LAN-only quick use with:

```bash
export ALLOW_LEGACY_TOKEN_LOGIN=1
```

This is **unsafe on public networks** (tokens can leak via history/screenshots/referrers).

Optional extras (recommended on a real machine with enough disk/RAM):

```bash
python3 -m pip install -e ".[translation,diarization,tts,dev]"
```

### Verify wiring (recommended)

```bash
python3 scripts/verify_config_wiring.py
python3 scripts/verify_env.py
python3 scripts/verify_features.py
python3 scripts/smoke_import_all.py
python3 scripts/smoke_run.py
python3 scripts/verify_runtime.py
python3 scripts/polish_gate.py
python3 scripts/security_smoke.py
python3 scripts/mobile_gate.py
python3 scripts/security_mobile_gate.py
```

Notes:
- `scripts/verify_env.py` reports **required vs optional** dependencies and which features are enabled.
- `scripts/polish_gate.py` is the “release polish” one-command check (imports + env + synthetic feature tests + stub/dupe scan).
- `scripts/mobile_gate.py` is the **end-to-end mobile/remote readiness** suite (synthetic media, no real content required).
- `scripts/security_mobile_gate.py` runs a **combined security + mobile** verification set (auth incl QR, upload safety, job lifecycle, playback variants, optional ntfy, and secret-leak scan).

Mobile/web guide: `docs/WEB_MOBILE.md` (legacy mobile doc: `docs/mobile_update.md`).

### Run a single dub job (CLI)

```bash
dubbing-pipeline Input/Test.mp4 --mode medium --device auto
```

Logging flags:

```bash
dubbing-pipeline Input/Test.mp4 --log-level DEBUG --debug-dump
```

Override ASR model directly:

```bash
dubbing-pipeline Input/Test.mp4 --asr-model large-v3 --device auto
```

### Batch folder/glob (CLI)

```bash
dubbing-pipeline --batch "Input/*.mp4" --jobs 2 --resume
```

### Multi-language examples (CLI)

```bash
dubbing-pipeline Input/Test.mp4 --src-lang auto --tgt-lang es
dubbing-pipeline Input/Test.mp4 --no-translate --src-lang ja
```

Force MT provider:

```bash
dubbing-pipeline Input/Test.mp4 --mt-provider nllb --src-lang ja --tgt-lang en
dubbing-pipeline Input/Test.mp4 --mt-provider none
```

### Subtitles (SRT/VTT)

```bash
dubbing-pipeline Input/Test.mp4 --subs both --subs-format both
```

### Voice controls

```bash
# force single narrator voice
dubbing-pipeline Input/Test.mp4 --voice-mode single

# prefer preset selection (skip cloning)
dubbing-pipeline Input/Test.mp4 --voice-mode preset

# hint cloning references from a folder: <dir>/<speaker_id>.wav
dubbing-pipeline Input/Test.mp4 --voice-mode clone --voice-ref-dir data/voices
```

### Expressive controls

```bash
dubbing-pipeline Input/Test.mp4 --emotion-mode auto
dubbing-pipeline Input/Test.mp4 --emotion-mode tags
```

### Optional Dialogue Isolation (Demucs)

Defaults preserve current behavior (**no separation**). To isolate dialogue and keep BGM/SFX:

```bash
# enable enhanced mixing + demucs stems (requires: pip install -e ".[mixing]")
dubbing-pipeline Input/Test.mp4 --mix enhanced --separation demucs
```

Artifacts:

- `Output/<stem>/stems/dialogue.wav`
- `Output/<stem>/stems/background.wav`
- `Output/<stem>/audio/final_mix.wav`

### Enhanced Mixing (LUFS + Ducking)

```bash
dubbing-pipeline Input/Test.mp4 --mix enhanced --lufs-target -16 --ducking --ducking-strength 1.0 --limiter
```

### Multi-track Outputs (Tier‑Next H, optional)

Opt-in multi-audio output for containers that support it (MKV preferred). When enabled, the pipeline writes deterministic track artifacts and (when `--container mkv`) muxes them into a single MKV without re-encoding video.

CLI:

```bash
# Multi-audio MKV (recommended): Original + Dubbed + Background + Dialogue
dubbing-pipeline Input/Test.mp4 --multitrack on --container mkv

# MP4 fallback: normal MP4 (dubbed) + sidecar .m4a tracks in Output/<job>/audio/tracks/
dubbing-pipeline Input/Test.mp4 --multitrack on --container mp4
```

Artifacts:

- `Output/<job>/audio/tracks/original_full.wav`
- `Output/<job>/audio/tracks/dubbed_full.wav`
- `Output/<job>/audio/tracks/background_only.wav` (derived from original when separation is off)
- `Output/<job>/audio/tracks/dialogue_only.wav`
- When `--container mp4`: also writes sidecar AAC tracks (`*.m4a`) in the same folder.

MKV track metadata/order:

- Track 1: **Original (JP)** (`language=jpn`)
- Track 2: **Dubbed (EN)** (`language=eng`, default)
- Track 3: **Background Only** (`language=und`)
- Track 4: **Dialogue Only** (`language=eng`)

Playback tip:

- Use VLC to select audio tracks: `Audio -> Audio Track`

### Per-job logs and manifests

When running `dubbing-pipeline`, the pipeline also writes per-job artifacts:

- `Output/<job>/logs/pipeline.log` (JSONL)
- `Output/<job>/logs/pipeline.txt` (human-readable breadcrumbs)
- `Output/<job>/logs/stages/<stage>.log` (JSONL, best-effort)
- `Output/<job>/logs/ffmpeg/*.stderr.log` (captured stderr per ffmpeg invocation)
- `Output/<job>/logs/summary.json` (timings summary, best-effort)
- `Output/<job>/manifests/<stage>.json` (resume-safe stage metadata, best-effort)

### Artifact retention / cache policy

By default the pipeline keeps everything. You can opt into cleanup of heavy intermediates under `Output/<job>/`.

- `--cache-policy full|balanced|minimal` (default: `full`)
- `--retention-days N` (optional age gate; `0` disables)
- `--retention-dry-run` (plan + report, no deletes)

Report: `Output/<job>/analysis/retention_report.json`

---

## Optional Features (A–M)

These are opt-in upgrades that extend the canonical pipeline without introducing parallel systems.

- **A Mode contract tests**: `scripts/verify_modes_contract.py`
- **B Artifact retention + cache policy**: `--cache-policy`, `Output/<job>/analysis/retention_report.json`
- **C Per-project profiles**: `--project <name>` with `projects/<name>/{profile,qa,mix,style_guide,delivery}.yaml`
- **D UI/CLI overrides**: `dubbing-pipeline overrides ...` (music regions + speaker overrides)
- **E Subtitle formatting + variants**: `Output/<job>/subs/{src,tgt_literal,tgt_pg,tgt_fit}.{srt,vtt}`
- **F Voice-memory character merge tools**: `dubbing-pipeline voice merge/undo-merge`
- **G Voice audition tool**: `dubbing-pipeline voice audition --text ...`
- **H QA rewrite-heavy/pacing-heavy/outlier checks + fix links**: `Output/<job>/qa/*` + UI deep-links
- **I Streaming context bridging**: overlap de-dup + context hints across chunks (default HIGH/MED)
- **J Lip-sync improvements**: offline preview + scene-limited lip-sync (optional)
- **K Per-character delivery profiles**: rate/pause/expressive/voice-mode defaults per character
- **L Cross-episode drift reports**: `Output/<job>/analysis/drift_report.md` + `data/reports/<project>/season_report.md`
- **M Optional offline LLM rewrite provider hook**: local-only endpoint/model, falls back to heuristic

### Mode + project + cache policy (example)

```bash
dubbing-pipeline Input/Test.mp4 --mode high --project example --cache-policy balanced --retention-days 0
```

### Overrides (CLI)

```bash
# Music regions
dubbing-pipeline overrides music list <job>
dubbing-pipeline overrides music add <job> --start 12.3 --end 25.0 --kind music --confidence 1.0 --reason "manual"
dubbing-pipeline overrides apply <job>

# Per-segment speaker override
dubbing-pipeline overrides speaker set <job> 12 SPEAKER_01
dubbing-pipeline overrides speaker unset <job> 12
```

### Subtitle variants (outputs)

After a run, check:
- `Output/<job>/subs/src.srt` (+ `.vtt`)
- `Output/<job>/subs/tgt_literal.srt`
- `Output/<job>/subs/tgt_pg.srt` (only when PG enabled)
- `Output/<job>/subs/tgt_fit.srt` (only when timing-fit enabled)

### Voice memory tools (merge + audition)

```bash
dubbing-pipeline voice list
dubbing-pipeline voice merge SPEAKER_99 SPEAKER_01
dubbing-pipeline voice undo-merge <merge_id>
dubbing-pipeline voice audition --text "Testing line" --top 5 --character SPEAKER_01 --lang en
```

### Character delivery profiles (Feature K)

```bash
dubbing-pipeline character set-rate SPEAKER_01 1.05
dubbing-pipeline character set-style SPEAKER_01 dramatic
dubbing-pipeline character set-expressive SPEAKER_01 0.7
dubbing-pipeline character set-voice-mode SPEAKER_01 clone
```

### Drift reports (Feature L)

After a run, check:
- `Output/<job>/analysis/drift_report.md`
- `data/reports/<project>/season_report.md`

### Optional offline LLM rewrite provider (Feature M)

This is used only during timing-fit and is **OFF by default**. See `docs/offline_llm_rewrite.md`.

```bash
dubbing-pipeline Input/Test.mp4 \
  --timing-fit \
  --rewrite-provider local_llm \
  --rewrite-endpoint http://127.0.0.1:8080/completion \
  --rewrite-strict
```

### Legacy (optional)

`dub-legacy` is kept for compatibility, but most users should use the primary pipeline.

If you need legacy CLI/UI dependencies:

```bash
python3 -m pip install -e ".[legacy]"
```

### Singing/Music Preservation (Tier‑Next A/B, optional)

Opt-in detector that attempts to find **music/singing-heavy regions** (opening/ending themes, inserted songs) and **leaves the original audio unchanged** during those time ranges.

```bash
dubbing-pipeline Input/Test.mp4 --music-detect on --music-threshold 0.70
```

OP/ED specialization (best-effort):

```bash
dubbing-pipeline Input/Test.mp4 --music-detect on --op-ed-detect on --op-ed-seconds 90
```

Artifacts:
- `Output/<job>/analysis/music_regions.json`
- `Output/<job>/analysis/op_ed.json` (when enabled)

Notes:
- When Demucs separation is enabled (`--mix enhanced --separation demucs`), music preservation switches the bed to the **original audio** during detected music regions so vocals aren’t lost.
- Detection is offline-first and uses lightweight heuristics by default. If optional deps are missing, it logs the chosen strategy and continues.

### PG Mode (Tier‑Next C, session-only; optional)

Opt-in **deterministic** text sanitization pass applied **after translation/style** and **before timing-fit, TTS, and target subtitles**. Default is **OFF** each run / each server restart.

CLI:

```bash
dubbing-pipeline Input/Test.mp4 --pg pg13
dubbing-pipeline Input/Test.mp4 --pg pg
dubbing-pipeline Input/Test.mp4 --pg pg13 --pg-policy /path/to/pg_policy.json
```

Web UI:
- In the Upload Wizard, enable **PG Mode** and choose a level.
- The selection is stored **per job** (not as a global server default).

Artifacts / audit:
- `Output/<job>/analysis/pg_filter_report.json` (does **not** include raw matched words; only hashed tokens + rule IDs)

### Quality Checks (Tier‑Next D, optional)

Offline scoring and checks that produce **actionable** per-segment issues (drift/overlap, speaking rate, clipping, low confidence, speaker flips, music overlap).

Enable during a run (does not change media outputs, only writes reports):

```bash
dubbing-pipeline Input/Test.mp4 --qa on
```

Run QA on an existing job directory:

```bash
dubbing-pipeline qa run Output/<job> --top 20
dubbing-pipeline qa run Output/<job> --fail-only
```

Artifacts:
- `Output/<job>/qa/segment_scores.jsonl`
- `Output/<job>/qa/summary.json`
- `Output/<job>/qa/top_issues.md`

Web UI:
- Job page includes a **Quality** tab when QA reports exist (or after enabling QA on submit).

### Projects / Style Guides (Tier‑Next E, optional)

Deterministic per-project text rules applied in this order:

**translate → style guide → PG filter → timing-fit → TTS/subtitles**

Folder layout:
- `projects/<project_name>/style_guide.yaml` (or `.json`)

CLI:

```bash
# load projects/example/style_guide.yaml
dubbing-pipeline Input/Test.mp4 --project example

# explicit path override
dubbing-pipeline Input/Test.mp4 --style-guide projects/example/style_guide.yaml
```

Audit artifact:
- `Output/<job>/analysis/style_guide_applied.jsonl` (per-segment applied rule IDs + conflict detection)

Conflict detection:
- If rules cause the text to repeat a previous state (loop/toggle), the engine stops early for that segment and logs a `style_guide_conflict`.

### Speaker smoothing (Tier‑Next F, optional)

Optional scene-aware post-processing to reduce rapid diarization speaker flips **within a scene** (helps voice selection stability and voice memory mapping).

CLI:

```bash
dubbing-pipeline Input/Test.mp4 --speaker-smoothing on --scene-detect audio --smoothing-min-turn 0.6 --smoothing-surround-gap 0.4
```

Artifact:
- `Output/<job>/analysis/speaker_smoothing.json`

Notes:
- Offline-only, heuristic by default (audio RMS/spectral changes + silence boundaries).
- If disabled, diarization behavior is unchanged.

### Dub Director mode (Tier‑Next G, optional)

Optional “direction” layer that makes **conservative** per-segment expressive adjustments (rate/pitch/energy/pauses) based on punctuation + scene intensity proxy. Tier‑1 pacing still enforces segment duration when enabled.

CLI:

```bash
dubbing-pipeline Input/Test.mp4 --director on --director-strength 0.5
```

Artifact:
- `Output/<job>/expressive/director_plans.jsonl`

### Timing Fit & Pacing (Tier‑1 B/C)

Defaults preserve current behavior. To enable timing-aware translation fitting + segment pacing:

```bash
dubbing-pipeline Input/Test.mp4 --timing-fit --pacing --wps 2.7 --tolerance 0.10 --pacing-min-stretch 0.88 --pacing-max-stretch 1.18
```

Notes:

- `--min-stretch` is an alias for `--pacing-min-stretch`
- `--pace-max-stretch` is an alias for `--pacing-max-stretch`
- `--max-stretch` (without the `pacing-` prefix) is the **legacy retime** bound used by the aligner path.

Debug artifacts (per segment):

```bash
dubbing-pipeline Input/Test.mp4 --timing-fit --pacing --timing-debug
```

This writes:

- `Output/<stem>/segments/0000.json` (start/end, pre-fit/fitted text, pacing actions)

### Character Voice Memory (Tier‑2 A)

Opt-in feature to keep **stable character identities across episodes** by persisting per-character reference WAVs + embeddings under `data/voice_memory/`.

Enable:

```bash
dubbing-pipeline Input/Test.mp4 --voice-memory on --voice-match-threshold 0.75 --voice-auto-enroll
```

Management:

```bash
dubbing-pipeline --list-characters
dubbing-pipeline --rename-character SPEAKER_01 "Zoro"
dubbing-pipeline --set-character-voice-mode SPEAKER_01 clone
dubbing-pipeline --set-character-preset SPEAKER_01 alice
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
dubbing-pipeline review init Input/Test.mp4
```

Inspect and edit:

```bash
dubbing-pipeline review list <job>
dubbing-pipeline review show <job> 12
dubbing-pipeline review edit <job> 12 --text "Edited line…"
```

Regenerate + preview + lock:

```bash
dubbing-pipeline review regen <job> 12
dubbing-pipeline review play <job> 12
dubbing-pipeline review lock <job> 12
```

Render final review output (rebuild episode audio from current per-segment WAVs and best-effort remux):

```bash
dubbing-pipeline review render <job>
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
dubbing-pipeline Input/Test.mp4 --lipsync wav2lip --wav2lip-checkpoint /models/wav2lip/wav2lip.pth
```

Preview (offline, best-effort face visibility scan):

```bash
dubbing-pipeline lipsync preview Input/Test.mp4
```

Scene-limited lip-sync (only apply where face visibility is good; falls back to pass-through elsewhere):

```bash
dubbing-pipeline Input/Test.mp4 --lipsync wav2lip --lipsync-scene-limited on
```

Output:
- `Output/<job>/final_lipsynced.mp4` (when successful)
- `Output/<job>/analysis/lipsync_preview.json` (preview report)
- `Output/<job>/analysis/lipsync_ranges.jsonl` (per-range status log when scene-limited)

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
dubbing-pipeline Input/Test.mp4 --timing-fit --pacing --expressive source-audio --expressive-strength 0.5 --expressive-debug
```

### Pseudo-streaming (chunk mode)

```bash
dubbing-pipeline Input/Test.mp4 --realtime --chunk-seconds 20 --chunk-overlap 2 --stitch
```

### Streaming Mode (Tier‑3 C, optional)

Chunked dubbing mode that produces playable per-chunk MP4s under `Output/<job>/stream/` plus a manifest. **Off by default**.

Enable (segments output):

```bash
dubbing-pipeline Input/Test.mp4 --stream on --chunk-seconds 10 --overlap-seconds 1 --stream-output segments
```

Enable (stitched final MP4):

```bash
dubbing-pipeline Input/Test.mp4 --stream on --chunk-seconds 10 --overlap-seconds 1 --stream-output final
```

Artifacts:
- `Output/<job>/chunks/` (mono16k chunk wavs)
- `Output/<job>/stream/manifest.json`
- `Output/<job>/stream/chunk_001.mp4`, `chunk_002.mp4`, ...
- `Output/<job>/stream/stream.final.mp4` (when `--stream-output final`)

API (when using `dubbing-web`):
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
dubbing-pipeline Input/Test.mp4 --dry-run
```

### Logging verbosity

```bash
dubbing-pipeline Input/Test.mp4 --verbose
dubbing-pipeline Input/Test.mp4 --debug
```

Artifacts land in:

- `Output/<stem>/audio.wav`
- `Output/<stem>/diarization.json` (optional)
- `Output/<stem>/<stem>.srt` (+ optional `<stem>.vtt`)
- `Output/<stem>/translated.json` + `<stem>.translated.srt` (+ optional `.vtt`)
- `Output/<stem>/<stem>.tts.wav`
- `Output/<stem>/<stem>.dub.mkv`
- `Output/<stem>/<stem>.dub.mp4`
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
  --entrypoint dubbing-pipeline \
  -v "$(pwd)":/app \
  -v "$(pwd)/Input":/app/Input \
  -v "$(pwd)/Output":/app/Output \
  dubbing-pipeline /app/Input/Test.mp4 --mode low --device cpu
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

- `JWT_SECRET` (set in `.env.secrets`)
- `SESSION_SECRET` (set in `.env.secrets`)
- `CSRF_SECRET` (set in `.env.secrets`)

Optional (legacy / special cases):
- `API_TOKEN` (only for legacy token-in-URL login, which is **disabled by default**)

Safety:

- `.env` and `.env.secrets` are gitignored.
- Use `STRICT_SECRETS=1` in production to enforce required secrets.

---

## API usage (FastAPI)

The canonical server entrypoint is `dubbing-web`.

For browser/mobile use:
- Open `http://<host>:8000/ui/login` (or `/login` alias) and sign in with username/password.

For full details (job submission, uploads, monitoring, playback, QA/editing):
- `docs/WEB_MOBILE.md`

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
- **FFmpeg robustness**: improved `dubbing_pipeline.utils.ffmpeg_safe` with retries/timeout support and better error messages.
- **File I/O safety**: added atomic write/copy helpers and used them for critical artifacts (e.g., realtime manifests, job SRT propagation).
- **CLI operability**: added `--dry-run`, `--verbose`, `--debug` for safer preflight and better diagnostics without changing defaults.
- **Runtime verification**: added `scripts/verify_runtime.py` for config + tool + filesystem checks (safe config report only).


- **Retention**: `RETENTION_DAYS_INPUT` (default `7`) best-effort purges old raw uploads from `Input/uploads/`. `RETENTION_DAYS_LOGS` (default `14`) purges old files from `logs/` and old per-job `Output/**/job.log`.
  - Run manually: `python -m dubbing_pipeline.ops.retention`

- **Backups**: creates a metadata-only zip (no large media) under `backups/`:
  - Includes: `data/**`, `Output/*.db`, `Output/**/{*.json,*.srt,job.log}`
  - Run manually: `python -m dubbing_pipeline.ops.backup`
  - Output: `backups/backup-YYYYmmdd-HHMM.zip` and `backups/backup-YYYYmmdd-HHMM.manifest.json` (with per-file SHA256).
  - **Restore**: unzip into the repo/app root (so paths like `data/...` and `Output/...` land in place), then verify file hashes against the manifest.