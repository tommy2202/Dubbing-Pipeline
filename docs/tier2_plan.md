## Tier‑2 plan (T2‑A + T2‑B) — scan results + integration design

This document is **planning only** (no Tier‑2 implementation in this step).

### Tier‑2 goals

- **T2‑A**: Persistent speaker identity stability across episodes (“Character Voice Memory”)
- **T2‑B**: Better review loop (edit a line → regenerate → preview → lock segment)

---

## 1) Current architecture summary (with exact touchpoints)

### 1.1 Diarization + speaker mapping (v2)

There are **two** diarization-related systems in v2:

- **Primary path (actively used by CLI + job queue)**: `src/anime_v2/stages/diarization.py`
  - Entry: `diarize(audio_path: str, device: str, cfg: DiarizeConfig) -> list[dict]`
  - Output: utterances like `[{start,end,speaker,conf}]` (speaker labels like `SPEAKER_01`)
  - Engines: `pyannote` (optional), `speechbrain`-style clustering over VAD segments (optional), `heuristic` fallback (always).

- **Secondary/legacy-ish path (present but not used by CLI/job queue)**: `src/anime_v2/stages/diarize.py` (removed during release hardening)
  - Entry: `run(audio_path: Path, out_dir: Path, ...) -> (segments, speaker_embeddings)`
  - Persists: `voices/registry.json` + `voices/embeddings/*.npy`
  - Uses **resemblyzer** embeddings when available; otherwise writes placeholder embeddings.

#### Where stable IDs are assigned today (v2)

Stable character IDs are currently assigned in orchestration code (not inside the diarization stage):

- **CLI**: `src/anime_v2/cli.py`
  - Step “2) diarization + persistent character IDs”
  - Flow:
    - `utts = diarize_v2(...)` (from `stages/diarization.py`)
    - per-utterance wav extraction into `Output/<stem>/segments/*.wav`
    - compute per-speaker **ECAPA embeddings** using `anime_v2.utils.embeds.ecapa_embedding`
    - map diarization label → persistent `character_id` using `CharacterStore.match_or_create(...)`
    - persist diarization json `Output/<stem>/diarization.json` containing `segments` and `speaker_embeddings` (best-effort)

- **Job pipeline**: `src/anime_v2/jobs/queue.py`
  - Step “b) diarize.identify”
  - Same pattern as CLI:
    - diarization work json includes `wav_path` (workdir); public json strips it
    - persistent character IDs come from `CharacterStore.default()`

#### Persistent store used for cross-episode identity (already exists)

- **`CharacterStore`**: `src/anime_v2/stages/character_store.py`
  - Purpose: “Persistent store for per-character voiceprints across scenes/episodes.”
  - Storage: encrypted file at `data/characters.json`
  - Keying:
    - `match_or_create(embedding, show_id: str, thresholds: {"sim": float}) -> character_id`
    - has a per-character `shows: {show_id: count}` for show-local preference.
  - Stores:
    - `embedding` (running average)
    - `speaker_wavs` (paths to reference wavs)

#### Speaker → voice selection hooks (already exist)

- **TTS stage**: `src/anime_v2/stages/tts.py`
  - Reads diarization JSON for:
    - representative `speaker_rep_wav` per `speaker_id`
    - `speaker_embeddings` map (optional)
  - Uses `CharacterStore.speaker_wavs` as a **cross-episode fallback** to locate a reference wav when diarization wavs aren’t present.
  - Supports per-job overrides via `voice_map.json` produced from:
    - **Web endpoint** `/api/jobs/{id}/characters` (`src/anime_v2/web/routes_jobs.py`)

### 1.2 Voice store / embedding store / preset selection (v2)

There are multiple voice-related persistence layers today:

- **Character Voice Memory (encrypted, per-repo)**:
  - `data/characters.json` via `CharacterStore` (stores embeddings + speaker wav paths)

- **Diarization speaker registry (plaintext, per-repo)**:
  - `voices/registry.json` + `voices/embeddings/*.npy` via `src/anime_v2/stages/diarize.py` (removed)
  - NOTE: this is not currently the primary path for v2 diarization used by CLI/job queue.

- **Preset voice DB (for “closest preset” fallback)**:
  - Implemented in `src/anime_v2/stages/tts_engine.py`:
    - `build_voice_db(...)`, `load_voice_db(...)`, `choose_similar_voice(...)`
  - Uses `voices/presets/**` and `voices/presets.json` + `voices/embeddings/<preset>.npy`

- **Voice reference store directory (wav copies, per speaker_id)**:
  - `settings.voice_store_dir` (default `data/voices`)
  - `src/anime_v2/stages/tts.py` persists `ref.wav` under `voice_store_dir/<speaker_id>/ref.wav`

### 1.3 Transcript editing + resynthesis (v2)

There is already an end-to-end “edit → approve → resynthesize approved only” path:

- **Web UI**:
  - Template: `src/anime_v2/web/templates/partials/transcript_editor.html`
  - Supports:
    - editing target text per segment
    - `Approved` checkbox
    - `Needs re-translate` flag
    - “Synthesize from Approved” action

- **Backend transcript store (per job, under Output/<stem>/)**:
  - Implemented in `src/anime_v2/web/routes_jobs.py`
  - Files:
    - `Output/<stem>/transcript_store.json` (latest state)
    - `Output/<stem>/transcript_versions.jsonl` (append-only history)
  - Endpoints:
    - `GET /api/jobs/{id}/transcript`
    - `PUT /api/jobs/{id}/transcript`
    - `POST /api/jobs/{id}/transcript/synthesize` (sets `job.runtime["resynth"] = {"type":"approved", ...}` and queues job)

- **Pipeline wiring (resynthesis)**:
  - `src/anime_v2/jobs/queue.py`
  - When `runtime.resynth.type == "approved"`:
    - `_apply_transcript_to_translated_json(approved_only=True)`:
      - creates an edited translation JSON for TTS
      - non-approved segments become **silence** (keeps timing)
      - writes an edited SRT for mux

This is the natural foundation for **T2‑B** (but it currently lacks per-line regenerate/preview and a “locked” concept).

### 1.4 Job storage + state machine

- Jobs are stored in sqlite-backed dicts:
  - `src/anime_v2/jobs/store.py` (`jobs.db` with tables `jobs`, `idempotency`, `presets`, `projects`)
- State machine:
  - `src/anime_v2/jobs/models.py` defines `JobState`
- Pipeline stages + checkpointing:
  - `src/anime_v2/jobs/checkpoint.py` writes `.checkpoint.json` in output dir

---

## 2) Single source of truth to extend (avoid duplicates)

### For T2‑A (Character Voice Memory)

**Primary source of truth to extend**:

- `src/anime_v2/stages/character_store.py` (`CharacterStore`)
  - Already designed for cross-episode identity.
  - Already has show-aware matching.
  - Already stores reference wav paths and embeddings.

**Orchestration touchpoints to update (do not duplicate speaker matching elsewhere)**:

- `src/anime_v2/jobs/queue.py` (diarization mapping → stable character IDs)
- `src/anime_v2/cli.py` (same mapping for CLI)
- `src/anime_v2/stages/tts.py` (speaker_wav selection and persistence)
- `src/anime_v2/web/routes_jobs.py` (job character mapping endpoint + upload)

### For T2‑B (Review loop / segment lock)

**Primary source of truth to extend**:

- `Output/<stem>/transcript_store.json` schema + logic in `src/anime_v2/web/routes_jobs.py`
- Resynth integration in `src/anime_v2/jobs/queue.py` (`_apply_transcript_to_translated_json`)

**UI touchpoint**:

- `src/anime_v2/web/templates/partials/transcript_editor.html`

---

## 3) Conflict list (existing basic frameworks / overlaps)

### Conflicts for T2‑A

- **Two competing “speaker memory” stores**
  - `CharacterStore` (encrypted, show-aware, used by CLI/job queue)
  - `voices/registry.json` + `voices/embeddings` (plaintext, produced by `stages/diarize.py`, removed)
  - Tier‑2 should pick **one** canonical memory store. Recommendation: **keep `CharacterStore` as canonical** and treat `voices/registry.json` as either:
    - deprecated legacy artifact, or
    - a derived cache strictly for preset selection (if needed), but not a separate identity source.

- **Two embedding stacks**
  - ECAPA embeddings (`anime_v2.utils.embeds.ecapa_embedding`) used in production speaker mapping now.
  - Resemblyzer embeddings (`tts_engine.py` presets) used elsewhere.
  - Tier‑2 should avoid introducing a third embedding type; if we add “episode memory”, store the embedding type + dimension in the schema.

### Conflicts for T2‑B

- The transcript editing system already supports `approved` and `flags`, but:
  - There is **no “locked” state** (to prevent future retranslation/resynth overwrite).
  - Resynth is **batch-only** (“approved only”), not “regenerate one line and preview”.
  - “Needs re-translate” exists but doesn’t yet trigger a targeted MT rerun.

---

## 4) Tier‑2 task checklist (concrete, file-scoped)

### T2‑A) Persistent speaker identity stability (“Character Voice Memory”)

**A.1 Normalize “series identity”**
- **Goal**: avoid `show_id = video.stem` fragmentation across episodes.
- Add a single concept used everywhere:
  - `series_id` (or `voice_memory_id`) separate from per-file stem.
- Touchpoints:
  - `config/public_config.py`: add `SERIES_ID` default empty + behavior docs
  - `src/anime_v2/cli.py`: if user passes `--show-id`, treat it as `series_id`
  - `src/anime_v2/jobs/queue.py`: use `settings.show_id` as `series_id` consistently
  - `src/anime_v2/web/routes_jobs.py`: allow project/preset to carry `series_id` in job runtime

**A.2 Make `CharacterStore` the canonical memory store**
- Extend schema to store:
  - multiple embeddings (rolling average + optional per-episode snapshots)
  - metadata: embedding model name, embedding dim, last_seen timestamps
  - canonical reference wav(s) (normalized copies in `voice_store_dir`)
- Touchpoints:
  - `src/anime_v2/stages/character_store.py`
  - `src/anime_v2/stages/tts.py` (ensure ref wav copies use stable canonical naming)

**A.3 Deterministic speaker ID stability per episode**
- Today mapping is done from diarization labels → rep wav → ECAPA embedding → `match_or_create`.
- Add:
  - stable “cluster signature” per diar label (optional) to reduce flip-flop
  - guardrails: if confidence low, don’t update the global embedding aggressively
- Touchpoints:
  - `src/anime_v2/jobs/queue.py` and `src/anime_v2/cli.py` speaker mapping loops
  - `src/anime_v2/utils/embeds.py` (optional: return quality metrics)

**A.4 UI/API for “voice memory” visibility**
- Add read-only endpoints to list known characters and their series coverage (no raw secrets).
- Touchpoints:
  - `src/anime_v2/web/routes_jobs.py` (or a new dedicated routes file under `anime_v2/api/`)
  - `src/anime_v2/web/templates/settings.html` or a new “Characters” page

### T2‑B) Better review loop (edit → regenerate → preview → lock)

**B.1 Extend transcript store schema to include locking**
- Add per-segment fields:
  - `locked: bool`
  - `locked_at`, `locked_by` (optional)
  - `audio_status` / `last_synth` metadata
- Touchpoints:
  - `src/anime_v2/web/routes_jobs.py` (`_load_transcript_store`, `PUT /transcript`)
  - `src/anime_v2/web/templates/partials/transcript_editor.html` (add Lock toggle)

**B.2 Targeted regenerate (single line)**
- Add endpoint:
  - `POST /api/jobs/{id}/transcript/segments/{idx}/synthesize`
    - synthesizes only that segment (and optional neighbors for context)
    - writes a preview wav: `Output/<stem>/segments_preview/<idx>.wav`
- Touchpoints:
  - `src/anime_v2/web/routes_jobs.py` (new endpoint)
  - `src/anime_v2/stages/tts.py` (add helper to synthesize one segment deterministically)
  - `src/anime_v2/timing/pacing.py` (reuse pacing)

**B.3 Preview playback**
- Serve preview segment audio via `/api/jobs/{id}/files` or a new `/preview` endpoint.
- UI: add a “Preview” button per segment that plays the preview wav.
- Touchpoints:
  - `src/anime_v2/web/routes_jobs.py` (file serving)
  - `src/anime_v2/web/templates/partials/transcript_editor.html` (audio element per line)

**B.4 Lock semantics in pipeline**
- When a segment is locked:
  - MT stage should not overwrite it (unless explicitly forced)
  - TTS resynth should always use the locked text as source of truth
- Touchpoints:
  - `src/anime_v2/jobs/queue.py`:
    - extend `_apply_transcript_to_translated_json` to incorporate `locked` and `needs_retranslate`
  - `src/anime_v2/cli.py` (optional: support lock file usage in CLI mode)

---

## 5) Data model proposal (JSON schema)

### 5.1 Speaker identity memory (`data/characters.json` plaintext payload schema)

This extends the existing `CharacterStore` structure (the file remains encrypted at rest).

```json
{
  "version": 2,
  "next_n": 12,
  "embedding": {
    "type": "ecapa",
    "dim": 192,
    "model": "speechbrain/spkrec-ecapa-voxceleb",
    "updated_at": "2026-01-01T00:00:00Z"
  },
  "characters": {
    "SPEAKER_01": {
      "name": "optional human label",
      "embedding_avg": [0.0],
      "embedding_count": 17,
      "series": {
        "one_piece_s01": { "seen": 12, "last_seen_at": "..." }
      },
      "speaker_wavs": [
        "data/voices/SPEAKER_01/ref.wav"
      ],
      "notes": "",
      "created_at": "...",
      "updated_at": "..."
    }
  }
}
```

**Notes**
- Keep `SPEAKER_XX` IDs stable (existing behavior).
- Store a single **canonical reference wav** path per character (plus optional extras).
- Track `series_id` rather than `video.stem` to stabilize across episodes.

### 5.2 Segment review state (`Output/<stem>/transcript_store.json`)

Extend existing per-job transcript store:

```json
{
  "version": 3,
  "updated_at": "2026-01-01T00:00:00Z",
  "segments": {
    "1": {
      "tgt_text": "Edited target text",
      "approved": true,
      "locked": false,
      "flags": ["needs_retranslate"],
      "last_preview": {
        "wav": "Output/<stem>/segments_preview/0001.wav",
        "duration_s": 1.23,
        "created_at": "..."
      }
    }
  }
}
```

**Semantics**
- `approved`: included in “approved-only” resynth batch.
- `locked`: text should not be overwritten by MT refresh; TTS always prefers locked text.
- `flags`: workflow signals like `needs_retranslate`.
- `last_preview`: points to the most recent per-segment preview artifact.

---

## 6) Summary: what Tier‑2 should *not* duplicate

- Do **not** create a new “voice memory” database: extend `CharacterStore`.
- Do **not** add a third embedding stack: standardize around ECAPA (current v2 mapping) and record embedding metadata in the store.
- Do **not** build a second transcript store: extend the existing `transcript_store.json` + `transcript_versions.jsonl` and the resynth hook in `jobs/queue.py`.

