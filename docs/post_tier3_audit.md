## Post Tier‑3 Audit (Lip‑sync plugin + Expressive prosody + Streaming mode)

Date: 2026‑01‑02

### Summary

Tier‑3 features are integrated as **optional, offline-first enhancements**:
- Tier‑3A: Lip-sync as a plugin (`wav2lip`) with safe fallbacks
- Tier‑3B: Expressive prosody controls (source-audio/text-only) that remain pacing-safe
- Tier‑3C: Streaming mode (chunked) producing per-chunk MP4s + manifest, with optional final stitch

All are **OFF by default** and do not change legacy pipeline behavior unless explicitly enabled.

---

### Phase A — Conflicts removed / rerouted (single source of truth)

#### Lip-sync
- Canonical implementation is now under `src/anime_v2/plugins/lipsync/`.
- The older v1 Wav2Lip runner was rerouted: `src/anime_v1/stages/lipsync.py` delegates to the v2 plugin.
- v2 pipeline wiring lives only in:
  - `src/anime_v2/cli.py` (CLI run)
  - `src/anime_v2/jobs/queue.py` (job runner)

#### Expressive prosody
- Canonical feature extraction: `src/anime_v2/expressive/prosody.py`
- Canonical policy + ffmpeg application: `src/anime_v2/expressive/policy.py`
- `src/anime_v2/stages/tts.py` keeps `--emotion-mode` for backward compatibility but routes “auto” behavior through the same categorization logic (no parallel frameworks).

#### Streaming
- Canonical streaming implementation:
  - `src/anime_v2/streaming/chunker.py`
  - `src/anime_v2/streaming/runner.py`
- Legacy `src/anime_v2/realtime.py` is now a wrapper that calls the streaming runner (no duplicated chunk pipeline).

---

### Phase B — Config + CLI coherence

- **Defaults preserve legacy behavior**:
  - `--lipsync` defaults to `off`
  - `--expressive` defaults to `off`
  - `--stream` defaults to `off` and `--realtime` defaults to `off`
- Settings are centralized in `config/public_config.py`:
  - `LIPSYNC`, `STRICT_PLUGINS`, `WAV2LIP_*`
  - `EXPRESSIVE*`
  - `STREAM*` (defaults match CLI)
- CLI help output matches the implemented behavior (`anime-v2 run --help`).

---

### Phase C — Import + runtime safety

- **No heavy work at import time**:
  - Wav2Lip plugin only resolves paths / runs subprocess inside `run()`
  - Expressive source-audio analysis only runs per segment when enabled
  - Streaming runner orchestrates work inside `run_streaming(...)`
- **Subprocess safety**:
  - All ffmpeg invocations use list args (no `shell=True`)
  - Wav2Lip inference uses list args and runs in a configured repo working directory
- **Temp/output consistency**:
  - Lip-sync temp files confined to `Output/<job>/tmp/lipsync/`
  - Expressive artifacts written to `Output/<job>/expressive/plans/`
  - Streaming artifacts written to `Output/<job>/chunks/` and `Output/<job>/stream/`

---

### Phase D — Verification scripts (no real anime required)

These scripts are present and intended to exit non-zero if broken:
- `scripts/smoke_import_all.py`
- `scripts/verify_lipsync_plugin.py` (uses a fake Wav2Lip repo; validates command+mux)
- `scripts/verify_expressive.py` (generates audio; works without optional deps)
- `scripts/verify_streaming_mode.py` (generates a tiny video; runs streaming in dry-run mode)

---

### Phase E — README sweep (Tier‑3)

README has:
- Lip-sync setup + example command + fallback behavior
- Expressive modes + debug artifacts + pacing interaction notes
- Streaming mode commands + artifacts + API endpoints + troubleshooting

---

### Quick test commands (3 max)

```bash
python3 -m scripts.smoke_import_all
python3 -m scripts.verify_lipsync_plugin
python3 -m scripts.verify_streaming_mode
```

---

### Limitations / future ideas

- **Streaming mode** currently re-encodes per-chunk video segments to baseline H.264 for concat compatibility; this is slower but robust. Future: use keyframe-aligned segment copy when inputs allow.
- **Expressive** pitch proxy uses librosa when available; future: add a lightweight pitch estimator to avoid optional dep.
- **Lip-sync** currently supports Wav2Lip; future: add additional plugins (e.g., SadTalker) under the same interface.

