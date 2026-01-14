## Voice cloning + persistence plan (design only)

This document designs two features without introducing duplicate implementations:

- **A) Full flow**: separation → diarization (on dialogue stem) → ref extraction → two-pass clone → mix over background stem.
- **B) Persistent character voice memory** across episodes/seasons, grouped by show/series, opt-in + privacy-safe.

It references the current codebase as of this branch and proposes **exact modules to modify**, expected artifacts/manifests, fallbacks, and UI flow. No implementation is performed in this document.

---

## 0) Current relevant building blocks (already in repo)

- **Demucs separation (canonical)**: `src/dubbing_pipeline/audio/separation.py`
  - `separate_dialogue(input_wav, out_dir, ...) -> SeparationResult(dialogue_wav, background_wav, meta_path, cached)`
- **Enhanced mix (canonical)**:
  - Professional mixdown: `src/dubbing_pipeline/audio/mix.py` (`mix_dubbed_audio(background_wav, tts_dialogue_wav, out_wav, ...)`)
  - Export/mux stage: `src/dubbing_pipeline/stages/mixing.py` + orchestration in `src/dubbing_pipeline/jobs/queue.py`
- **Diarization stage (canonical)**: `src/dubbing_pipeline/stages/diarization.py` (`diarize(audio_path: str, device, cfg) -> utts`)
- **Speaker reference extraction (canonical)**: `src/dubbing_pipeline/voice_memory/ref_extraction.py`
  - Writes job-local refs under `Output/<job>/analysis/voice_refs/<speaker>.wav` (and global voice store refs)
- **Two-pass orchestration (current, already added)**: `src/dubbing_pipeline/jobs/queue.py`
  - Pass 1 forces no-clone TTS, then queues pass 2.
  - Pass 2 forces rerun of TTS + mix and uses job-local refs.
- **Voice memory (Tier-2A, opt-in, current)**: `src/dubbing_pipeline/voice_memory/store.py`
  - Current layout under `VOICE_MEMORY_DIR`: `characters.json`, `embeddings/<character_id>/ref_*.wav`, `episodes/<episode_key>.json`
  - Has merge tooling: `src/dubbing_pipeline/voice_memory/tools.py`, CLI: `src/dubbing_pipeline/voice_memory/cli.py`
- **Job UI “characters” mapping**:
  - API: `src/dubbing_pipeline/web/routes_jobs.py` (`GET/PUT /api/jobs/{id}/characters`)
  - UI: `src/dubbing_pipeline/web/templates/job_detail.html` (Overrides tab)
- **Privacy + encryption primitives**:
  - Privacy runtime gating exists in `jobs/queue.py` (privacy/minimal flags) and UI gating for ref audio already exists.
  - Encryption config already supports class `"voice_memory"` via `ENCRYPT_AT_REST_CLASSES` in `config/public_config.py`.

---

## 1) A) Full separation → diarization(dialogue stem) → refs → two-pass clone → mix(background stem)

### 1.1 Target stage graph (single canonical pipeline)

**Pass 1 (no clone; build refs)**

1. **Audio extract**: `audio.wav` (current)
2. **Separation** (optional): `stems/dialogue.wav` + `stems/background.wav`
3. **Diarization** (on *dialogue stem* when available)
4. **Voice memory mapping** (optional) + **ref extraction** (job-local refs)
5. **ASR + translation** (ideally on dialogue stem as well; see 1.3)
6. **TTS pass 1** (explicitly no clone; preset/basic fallback)
7. **Mix**:
   - Enhanced mix: background stem + pass-1 TTS track (acceptable preview)
   - Legacy mix: existing behavior
8. **Queue pass 2 automatically** (do not run expensive exports twice)

**Pass 2 (TTS + mix only; use refs)**

1. Reuse checkpoints: separation, diarization, translation
2. **TTS pass 2** (clone mode with per-speaker refs)
3. **Mix pass 2** over background stem
4. Post-steps (mobile outputs, lipsync) should run **only after pass 2** (or only once at the end).

### 1.2 Exact modules/files to modify (A)

#### Choosing diarization input (dialogue stem vs original)

- **Primary place**: `src/dubbing_pipeline/jobs/queue.py`
  - Create canonical variables early in the pipeline:
    - `wav_src` = audio extracted path (current `wav`)
    - `wav_dialogue` = `base_dir/stems/dialogue.wav` (if separation ran and succeeded)
    - `wav_background` = `base_dir/stems/background.wav` (if separation ran)
    - `wav_for_speech` = `wav_dialogue if exists else wav_src`
  - Then use `wav_for_speech` as input to:
    - diarization (`diarize_v2`)
    - per-utt segment extraction (`extract_audio_mono_16k(src=wav_for_speech, ...)`)
    - voice memory matching and ref extraction (because their candidates come from diar segments)

Notes:
- **Do not add a new diarization pipeline**. Only change the **input WAV path** used by existing `diarize_v2`.
- Ensure the diarization public/work JSON fields (`audio_path`, per-segment `wav_path`) reflect the **speech wav** used, so downstream tooling is consistent.

#### Separation stage (reuse existing demucs)

- **Primary place**: `src/dubbing_pipeline/jobs/queue.py` (already calls `separate_dialogue`)
- **Canonical separation implementation** remains in `src/dubbing_pipeline/audio/separation.py`.

Changes to plan (design intent):
- Add a **checkpoint stage** for separation (e.g. `stage="separation"`) so pass 2 can skip it.
- Persist stable stems in `Output/<job>/stems/`:
  - `Output/<job>/stems/dialogue.wav`
  - `Output/<job>/stems/background.wav`
  - `Output/<job>/stems/meta.json`
- Add a stage manifest entry `manifests/separation.json` via `write_stage_manifest(...)`:
  - inputs: fingerprint of `wav_src`
  - outputs: absolute paths to dialogue/background/meta
  - params: model/device/stems/cache key

#### Ref extraction stage (already present; make it dialogue-based automatically)

- **Already implemented** in `src/dubbing_pipeline/voice_memory/ref_extraction.py`
- **Orchestrated** in `src/dubbing_pipeline/jobs/queue.py` immediately after diarization mapping.

Design detail:
- Ensure the diarization segments used to build refs come from `wav_for_speech` (dialogue stem when available).
- Keep existing job-local output:
  - `Output/<job>/analysis/voice_refs/manifest.json`
  - `Output/<job>/analysis/voice_refs/<speaker_id>.wav`

#### Two-pass rerun (TTS + mix only)

- **Primary place**: `src/dubbing_pipeline/jobs/queue.py`
  - Pass 1: force `tts.run(..., voice_mode="preset", no_clone=True, voice_ref_dir=None)`
  - Pass 2: force `tts.run(..., voice_mode="clone", no_clone=False, voice_ref_dir=Output/<job>/analysis/voice_refs)`
  - In pass 2, skip:
    - separation (checkpoint)
    - diarization (checkpoint)
    - translation (checkpoint)
  - Force rerun of:
    - TTS stage (ignore checkpoint)
    - mix stage (ignore checkpoint), and rebuild mobile outputs once

- **Admin rerun trigger** (already exists conceptually):
  - Endpoint: `src/dubbing_pipeline/web/routes_jobs.py` `POST /api/jobs/{id}/two_pass/rerun`
  - Job runtime marker: `job.runtime.two_pass.request = "rerun_pass2"`

#### Mixing over background stem

- **Enhanced mix uses bed + TTS**: already done in `src/dubbing_pipeline/jobs/queue.py` and uses canonical `src/dubbing_pipeline/audio/mix.py`.
- Design adjustment for the “full flow”:
  - When separation is enabled and succeeded, set background input for mix to:
    - `wav_background` (Demucs `background.wav`) not the original `wav_src`
  - Ensure pass 2 reruns mix using the same `wav_background`.

### 1.3 (Recommended) Use dialogue stem for ASR/MT too

Although the requirement calls out diarization specifically, in practice **ASR and translation are more robust** on a dialogue stem.

Plan:
- In `src/dubbing_pipeline/jobs/queue.py`, pass `audio_path=wav_for_speech` into:
  - `transcribe(...)` (currently receives `wav`)
  - translation providers that use audio (`TranslationConfig.audio_path`, whisper translate path)
- Keep the original extracted audio around (unless privacy/minimal forbids it) for:
  - operator debugging
  - optional features that want full mix context

Fallback:
- If demucs is unavailable or fails, `wav_for_speech = wav_src`.

### 1.4 Pass 2 correctness + artifacts + manifests

To avoid “duplicate pipelines” and not break resume, pass 2 should reuse artifacts via checkpoint + stage manifests:

- **Separation checkpoint**: `stages.separation.done` + artifacts `{dialogue_wav, background_wav, meta}`
- **Diarization checkpoint**: already present in `jobs/queue.py` (`diarization.work.json`, `diarization.json`)
- **Translation checkpoint**: `translated.json` and `translated_srt`
- **TTS manifest**: `Output/<job>/work/<job_id>/tts_manifest.json` already exists
  - Must include per-speaker:
    - `refs_used`
    - `clone_attempted`
    - `clone_succeeded`
    - `fallback_reasons`
- **Mix artifacts**:
  - Enhanced mix uses `Output/<job>/audio/final_mix.wav`
  - Legacy mix creates container outputs

Logging:
- Each stage should append a single high-signal line to job logs:
  - `two_pass: phase=pass1|pass2 enabled=...`
  - `separation: ok cached=...`
  - `diarize: input=<dialogue|full>`
  - `voice_refs: speakers=N warnings=...`
  - `tts: speaker_report summarized`
  - `mix: background=<bed|full> mode=<enhanced|legacy>`

---

## 2) B) Persistent character voice memory across episodes/seasons (grouped by series)

### 2.1 Goals and constraints

- **Opt-in**: must be explicitly enabled (`VOICE_MEMORY=on`) and should be disabled automatically in privacy/minimal contexts.
- **Privacy-safe**:
  - store only: small reference WAVs (seconds), embeddings, and minimal metadata/mappings
  - do not store full episode audio
  - support deletion of characters and mappings
  - support encryption at rest when configured (`ENCRYPT_AT_REST_CLASSES` includes `voice_memory`)
- **No duplicate implementations**:
  - reuse existing `VoiceMemoryStore` logic and CLI merge tooling
  - extend or wrap it for series-scoped roots rather than creating a second store

### 2.2 Proposed data model + folder layout (series-scoped)

Requirement target:

`voice_store/<series_slug>/characters/<character_id>/...`

Proposed canonical root:
- Use existing `VOICE_STORE` (already used for extracted refs) as the top-level “voice storage” root, then create a **new sub-tree** for voice memory per series:

```
<VOICE_STORE>/
  <series_slug>/
    characters/
      <character_id>/
        meta.json
        refs/
          ref_001.wav
          ref_002.wav
        embeddings/
          embedding.npy           # if numpy available
          embedding.json          # always written as fallback
        delivery_profile.json     # optional, or embedded into meta.json
        aliases.json              # optional (merge/alias)
    episodes/
      <episode_key>.json          # diar_label -> character_id mapping for that episode
    backups/
      <merge_id>/...              # reuse existing merge backup format under the series root
    index.json                    # optional: summary for quick listing
```

Notes:
- `series_slug` already exists in the library model and is stored on each job (`job.series_slug`).
- `character_id` should be stable within a series. Options:
  - Keep current `SPEAKER_XX` style IDs (simple)
  - Or migrate to `CHAR_0001` style IDs (clearer semantics)
  - Either works as long as the UI shows a human display name and merges are supported.

### 2.3 Exact modules/files to modify (B)

#### Series-scoped VoiceMemoryStore

Preferred approach: **wrap** the existing store to avoid duplicating logic.

- Add helper module:
  - `src/dubbing_pipeline/voice_memory/series.py` (new)
    - `def voice_memory_root_for_series(voice_store_dir: Path, series_slug: str) -> Path`
    - returns `<VOICE_STORE>/<series_slug>/voice_memory` or directly `<VOICE_STORE>/<series_slug>/` depending on final layout choice

- Modify/extend existing store to support alternative layout without rewriting algorithms:
  - `src/dubbing_pipeline/voice_memory/store.py`
    - allow specifying subpaths `characters_dir`, `episodes_dir`, `backups_dir` under `root`
    - or keep the store as-is but instantiate it with `root=<VOICE_STORE>/<series_slug>/` and adjust internal directory names:
      - today it uses `root/embeddings` + `root/episodes` + `root/characters.json`
      - new layout wants `root/characters/<id>/...`, so a small refactor is required:
        - `characters.json` becomes `characters/index.json` or `root/index.json`
        - `embeddings/<id>` becomes `characters/<id>/embeddings`
        - refs are `characters/<id>/refs/ref_*.wav`

This refactor should be done in a backwards-compatible way:
- If old layout exists, continue to read it (migration-on-read or explicit migration command).

#### Job pipeline: mapping speakers to persistent character IDs

- `src/dubbing_pipeline/jobs/queue.py`
  - In the diarization mapping section (currently does voice-memory match):
    - instantiate the series-scoped store when enabled:
      - `series_slug = job.series_slug` (fallback to slug derived from series_title)
      - `store = VoiceMemoryStore(root=voice_store/<series_slug>/...)`
    - for each diar label, compute representative wav (from dialogue stem segments), then:
      - if UI has explicit mapping: `job.runtime.voice_map` mapping `SPEAKER_XX -> <character_id>`
      - else call `match_or_create_from_wav(...)` to pick/create a character id
    - persist `episodes/<episode_key>.json`:
      - include series_slug, season/episode numbers, job_id, and mapping confidence

Fallback rules:
- If embeddings provider is unavailable:
  - still allow creating/enrolling refs (no embedding)
  - treat matches as unknown, requiring manual assignment

Privacy rules:
- If `privacy_mode` or `minimal_artifacts`:
  - auto-disable voice memory enrollment and do not write to series store

#### UI/API for listing/assigning/locking characters

- **Web API**: `src/dubbing_pipeline/web/routes_jobs.py`
  - Add endpoints (admin/editor/operator depending on scope):
    - `GET /api/series/{series_slug}/voice_memory/characters` (list)
    - `POST /api/series/{series_slug}/voice_memory/characters` (create)
    - `PATCH /api/series/{series_slug}/voice_memory/characters/{character_id}` (rename, set voice_mode, preset, delivery_profile)
    - `POST /api/series/{series_slug}/voice_memory/characters/{character_id}/enroll_ref` (enroll current job’s extracted ref)
    - `DELETE /api/series/{series_slug}/voice_memory/characters/{character_id}` (delete + scrub mappings)
  - Ensure these endpoints:
    - respect privacy/minimal flags (block or require explicit admin override)
    - respect encryption-at-rest settings (do not serve raw encrypted data)
    - log audit events

- **UI**: `src/dubbing_pipeline/web/templates/job_detail.html`
  - Extend the existing **Overrides** tab (no new pages) with:
    - “Assign speakers to characters” table
    - “Lock character voice” controls
    - “Delete character” (admin-only)

---

## 3) Mapping SPEAKER_XX → persistent Character IDs (algorithm)

### 3.1 Inputs available today

- Diarization output includes per-utterance segments with:
  - `speaker_id` (currently mapped)
  - `wav_path` (segment wav extracted)
- Job metadata includes:
  - `series_title`, `series_slug`, `season_number`, `episode_number`
- `VoiceMemoryStore.match_or_create_from_wav(wav_path, ...)` already exists.

### 3.2 Proposed mapping strategy (deterministic + safe)

For each diarization cluster label (e.g. `SPEAKER_01`):

1. Pick a representative sample:
   - Prefer the best “clean” segment (from dialogue stem) or the extracted **speaker ref wav** once built.
2. If job has explicit assignment (`job.runtime.voice_map`):
   - Use the assigned `character_id` directly.
3. Else if voice memory is enabled:
   - Compute embedding (best-effort).
   - Match against existing characters within `series_slug`.
   - If similarity ≥ threshold: select that character.
   - Else create a new character and enroll the ref.
4. Persist `episodes/<episode_key>.json` mapping:
   - `diar_label` → `{character_id, similarity, provider, confidence}`

### 3.3 Handling merges and aliases

Reuse existing merge tooling concepts (already present under `VOICE_MEMORY_DIR`):
- Merge old character into new character
- Keep alias tombstones
- Update episode mappings to point to canonical character

Series-scoped plan:
- Move merge backup + alias metadata under `voice_store/<series_slug>/backups/` and `characters/<id>/...`.

---

## 4) UI flow (no new pages) for assignment + locking

All UI lives in **Job Detail → Overrides**.

### 4.1 “Assign speakers to characters”

UI section: “Voice memory (series)”:

- Left column: detected speakers for this job (from `analysis/voice_refs/manifest.json`):
  - `SPEAKER_01`, `SPEAKER_02`, ...
  - show ref duration, warnings, and clone/fallback status
- Right column: dropdown of existing series characters:
  - display name + character_id
  - “Create new character”

Actions:
- Save mapping writes to:
  - `job.runtime.voice_map` (existing API `/api/jobs/{id}/characters`)
  - and (optionally) `episodes/<episode_key>.json` in series voice memory store

Fallback:
- If voice memory disabled or blocked by privacy:
  - show mapping as “job-local only” and do not persist.

### 4.2 “Lock character voice”

Per character actions:
- **Lock mode**:
  - Set `preferred_voice_mode` to `clone` or `preset`
  - If preset: choose preset voice id
- **Enroll ref**:
  - “Use this job’s ref as the character ref”
  - copies `Output/<job>/analysis/voice_refs/<speaker>.wav` into:
    - `voice_store/<series_slug>/characters/<character_id>/refs/ref_XXX.wav`
  - updates embedding (best-effort)
- **Admin delete**:
  - deletes character folder and removes references from episode mappings
  - records audit log

### 4.3 Rerun pass 2 after changes

UI already has the pattern for “Rerun pass 2”:
- After mapping or enrolling refs, the operator/admin can click:
  - **Rerun pass 2** (server sets runtime marker + queues job)

---

## 5) Fallbacks + logging (must not break working code)

### 5.1 Separation fallbacks
- If demucs missing/fails:
  - log: `separation unavailable; using original audio`
  - `wav_for_speech = wav_src`, `wav_background = wav_src`

### 5.2 Voice memory fallbacks
- If embeddings can’t be computed:
  - store refs only (no embedding) and set mapping confidence low
  - require manual assignment for stability

### 5.3 TTS fallbacks (per speaker, per segment)
- If XTTS clone fails:
  - fall back to XTTS preset → basic TTS → espeak → silence
  - record per-speaker status in `tts_manifest.json`:
    - `clone_attempted`, `clone_succeeded`, `fallback_reasons`, `refs_used`, `providers`

### 5.4 Privacy + deletion
- If `privacy_mode` or `minimal_artifacts`:
  - do not persist voice memory across episodes
  - do not serve voice refs audio in UI/API
- Deletion must:
  - remove character refs + embeddings
  - scrub episode mappings
  - leave an audit event (and optionally a tombstone record)

---

## 6) Summary: “Exact modules to modify” checklist

1) **Choosing diarization input (dialogue stem vs original)**
- `src/dubbing_pipeline/jobs/queue.py` (use `wav_for_speech`)

2) **Adding / strengthening ref extraction stage**
- `src/dubbing_pipeline/jobs/queue.py` (ensure refs built from dialogue-derived diar segments)
- `src/dubbing_pipeline/voice_memory/ref_extraction.py` (already writes job-local refs; keep canonical)

3) **Two-pass rerun (TTS + mix only)**
- `src/dubbing_pipeline/jobs/queue.py` (phase control + checkpoint usage + forcing TTS/mix rerun)
- `src/dubbing_pipeline/stages/tts.py` (prefer per-speaker refs; emit speaker_report)
- `src/dubbing_pipeline/web/routes_jobs.py` (admin rerun trigger)
- `src/dubbing_pipeline/web/templates/job_detail.html` (button already lives here)

4) **Persistent voice memory across series**
- `src/dubbing_pipeline/voice_memory/store.py` (refactor for series-scoped layout or add compatibility layer)
- `src/dubbing_pipeline/voice_memory/tools.py` + `src/dubbing_pipeline/voice_memory/cli.py` (reuse; relocate to series root)
- `src/dubbing_pipeline/jobs/queue.py` (series-scoped store + mapping persistence)
- `src/dubbing_pipeline/web/routes_jobs.py` + `src/dubbing_pipeline/web/templates/job_detail.html` (UI/API for assignment + locking + deletion)

---

## 7) Next implementation steps (out of scope for this doc)

- Implement `wav_for_speech` routing and checkpointed separation stage.
- Add series-scoped voice memory root + migration strategy.
- Add voice memory CRUD endpoints and extend job detail UI to use them.
- Add end-to-end verifier scripts for:
  - “diarization on dialogue stem”
  - “series-scoped voice memory mapping persists across two synthetic jobs”

