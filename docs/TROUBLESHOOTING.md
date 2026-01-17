## Troubleshooting

This repo is designed to degrade gracefully when optional components are missing, but some issues are common on first run.

---

## “ffmpeg not found” / “ffprobe not found”

Symptoms:
- jobs fail early
- logs mention missing `ffmpeg` or `ffprobe`

Fix:
- Install ffmpeg and ensure it’s on `PATH`.

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

If you use a non-standard path:
- set `FFMPEG_BIN` and `FFPROBE_BIN` in `.env`.

---

## Startup self-check failed (ffmpeg, disk, or writable paths)

Symptoms:
- Server exits at startup
- Logs mention `startup_self_check_failed`

Fix:
- Ensure `ffmpeg` and `ffprobe` are installed and on `PATH`
- Ensure `Output/` and `Input/` are writable
- Increase free disk space or set `MIN_FREE_GB=0` to disable the disk check

---

## GPU not detected / running on CPU unexpectedly

Symptoms:
- `--device auto` chooses CPU
- performance is much slower than expected

Checks:
- `nvidia-smi` works on the host (Linux)
- PyTorch sees CUDA

For local Python:
```bash
python3 -c "import torch; print(torch.cuda.is_available())"
```

For Docker:
- ensure NVIDIA Container Toolkit is installed
- run with GPU enabled (`--gpus all`)

---

## Models failing to download

Symptoms:
- errors referencing Hugging Face / model downloads
- timeouts during first run

Common causes:
- `OFFLINE_MODE=1` or `ALLOW_EGRESS=0` blocks downloads
- missing Hugging Face token for gated models (`HUGGINGFACE_TOKEN`)
- network/DNS issues

Fix options:
- Temporarily enable egress for cache warmup:
  - `OFFLINE_MODE=0`
  - `ALLOW_EGRESS=1`
- For gated models, set `HUGGINGFACE_TOKEN=<token>` in `.env.secrets`
- Then disable egress again if you want an offline posture.

---

## Diarization not available

Symptoms:
- diarization falls back to heuristic or is skipped
- `--diarizer pyannote` fails

Cause:
- optional diarization dependencies not installed or models not available offline.

Fix:
- Install extras:

```bash
python3 -m pip install -e ".[diarization]"
```

Or explicitly disable:
```bash
dubbing-pipeline Input/Test.mp4 --diarizer off
```

---

## Why cloning fell back

Symptoms:
- voices sound generic or not cloned
- job “Voices” tab shows `fallback` or a fallback reason

Where to check:
- `Output/<stem>/analysis/tts_manifest.json` (see `speaker_report`)
- Job page → **Voices** tab (clone status + fallback reason)

Common causes:
- XTTS not available (`xtts_unavailable`)
- no usable speaker ref (`no_ref`)
- pass 1 of two-pass (cloning intentionally disabled)
- voice mode set to `preset` or `single`

Fix:
- ensure `VOICE_MODE=clone`
- verify speaker refs exist under `Output/<stem>/analysis/voice_refs/`
- if using two-pass, rerun **pass 2** after mapping voices

---

## Not enough clean speech

Symptoms:
- voice ref manifest warns `no_good_candidates` or `insufficient_audio`
- cloning fails or uses fallbacks

Where to check:
- `Output/<stem>/analysis/voice_refs/manifest.json`

Fix:
- provide cleaner input audio
- adjust `VOICE_REF_*` thresholds
- upload a manual ref override in the job **Voices** tab

---

## Diarization on wrong stem

Symptoms:
- diarization uses the full mix instead of dialogue-only audio

Where to check:
- Job log line: `diarize input=<dialogue_stem|original> path=...`
- CLI `diarization.json` fields: `diarization_input` and `diarization_input_rel`

Fix:
- enable separation: `MIX=enhanced` and `SEPARATION=demucs`
- disable separation to force original audio

---

## Reset series voices

If character refs drift or are wrong, reset the series voice store:

```bash
rm -rf data/voices/<series_slug>
```

See `docs/PRIVACY.md` for full voice data deletion.

---

## Web login works but POST actions fail (CSRF)

Symptoms:
- UI loads, but job submit/edit/cancel returns 403/401
- errors mention CSRF token

Explanation:
- Browser/cookie sessions require CSRF for state-changing actions.

Fix:
- Use the UI pages (they set the `csrf` cookie).
- If calling the API from a script using cookies, include `X-CSRF-Token: <csrf>` where `<csrf>` matches the `csrf` cookie.
- Alternatively, use Bearer tokens or API keys (no CSRF).

---

## “weak_secrets_detected”

This warning appears when the server detects placeholder secrets (dev defaults).

For local testing it’s OK, but for any remote use:
- set real secrets in `.env.secrets`
- enable strict checking:

```bash
export STRICT_SECRETS=1
```

---

## Upload rejected (400): unsupported container / too large / invalid checksum

Causes:
- file extension/MIME not in the allowlist
- `ffprobe` can’t read the file or container isn’t allowed
- chunk checksum mismatch (`X-Chunk-Sha256`)
- file exceeds `MAX_UPLOAD_MB` or duration exceeds `MAX_VIDEO_MIN`

Fix:
- try MP4/MKV
- check your limits in `.env`
- re-upload (mobile networks can corrupt uploads; chunked upload is resumable)

---

## Playback issues on iPhone/iPad (MKV)

Symptoms:
- MKV won’t play in Safari

Fix:
- Use the job page’s **Mobile MP4** (`Output/<stem>/mobile/mobile.mp4`)
- Or enable **HLS** (optional)
- Or use the “Open in VLC” links

---

## Remote access not working (403)

Symptoms:
- server is reachable, but requests are denied

Fix checklist:
- Tailscale mode:
  - ensure you’re using the **Tailscale IP**
  - ensure `REMOTE_ACCESS_MODE=tailscale`
- Cloudflare mode:
  - ensure traffic is coming from a trusted proxy
  - ensure `REMOTE_ACCESS_MODE=cloudflare` and `TRUST_PROXY_HEADERS=1`

Run the verifier:

```bash
python3 scripts/verify_remote_mode.py
```

---

## WebRTC preview returns 503

Cause:
- WebRTC deps aren’t installed (`aiortc`, `av`).

Fix:
```bash
python3 -m pip install -e ".[webrtc]"
```

---

## Rate limits (429)

The server rate-limits sensitive endpoints (login, uploads, WebRTC offer, etc.).

If you’re testing:
- slow down retries
- avoid parallel logins/uploads from the same IP

---

## “First run” gotchas

- **Large first-run downloads**: TTS/ASR models can be large; be patient or prewarm caches.
- **Disk space**: ensure `MIN_FREE_GB` is satisfied (default 10GB).
- **CPU-only is slower**: start with `--mode low` or a smaller ASR model.
- **Remote mode requires hardening**: set real secrets, set `COOKIE_SECURE=1` when HTTPS, restrict CORS.

---

## Useful verification commands

```bash
python3 scripts/verify_env.py
python3 scripts/verify_auth_flow.py
python3 scripts/verify_job_submission.py
python3 scripts/verify_playback_variants.py
python3 scripts/security_mobile_gate.py
```

