## Post Tier‑2 Audit (Voice Memory + Review Loop)

Date: 2026‑01‑01

### Summary

Tier‑2 features are wired end‑to‑end with **one canonical review state** (`Output/<job>/review/state.json`) and an **opt‑in cross‑episode identity store** (`data/voice_memory/`). The pipeline now avoids coupling Tier‑2A to the legacy encrypted `CharacterStore` when voice memory is enabled.

### What was removed / rerouted (interference fixes)

- **Duplicate SRT cue parsing + diarization→speaker assignment**
  - Rerouted `anime_v2.cli` and `anime_v2.jobs.queue` to delegate to `anime_v2.utils.cues` (`parse_srt_to_cues`, `assign_speakers`) so the logic lives in one place.

- **Legacy cross‑episode “CharacterStore” vs Tier‑2A voice memory**
  - `anime_v2.stages.tts` now **does not load `CharacterStore` when `voice_memory` is enabled**, preventing key/crypto requirements from interfering with Tier‑2A.
  - `anime_v2.cli` and `anime_v2.jobs.queue` now load `CharacterStore` only in the legacy path and **fall back gracefully** if the encryption key is missing.

- **Review loop vs “transcript resynth approved”**
  - The legacy “approved-only resynth” flow now **materializes into the review loop**: after a resynth run, approved segments are copied into `Output/<job>/review/audio/` and marked `locked` in `Output/<job>/review/state.json`.
  - This makes `review/state.json` the canonical segment-lock store going forward.

- **Hardcoded embeddings path**
  - The CLI legacy path no longer writes embeddings to a repo-relative `voices/embeddings/`; it now writes under `Output/<job>/voices/embeddings/` when used.

### Final feature summary

- **Tier‑2A (Character Voice Memory)**
  - Opt-in via `--voice-memory on`
  - Persistent store: `data/voice_memory/` (characters + embeddings + per-episode mappings)
  - Fallbacks: if embedding deps are missing, voice memory degrades to deterministic non-ML fingerprinting.

- **Tier‑2B (Review Loop)**
  - Canonical state: `Output/<job>/review/state.json` (atomic writes)
  - Segment audio versions: `Output/<job>/review/audio/<segment_id>_vN.wav`
  - Locked segments are reused during full pipeline reruns (no re-synthesis for those segments).
  - Optional web API endpoints exist for list/edit/regen/lock/unlock + range audio preview.

### Quick test commands (3 max)

```bash
python3 -m scripts.smoke_import_all
python3 -m scripts.verify_voice_memory
python3 -m scripts.verify_review_loop
```

### Known limitations / notes

- **Segment locking uses 1-based `segment_id`** (matches SRT/translated segment numbering).
- **Review `render` mux is best-effort**: it will produce `review_render.wav` always; video remux requires the original video path to be present and muxable.
- **Legacy `CharacterStore` remains for backwards compatibility** when voice memory is disabled, but it is no longer required for Tier‑2A and failures are non-fatal.

