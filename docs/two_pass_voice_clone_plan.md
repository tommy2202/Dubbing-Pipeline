# Two-pass automatic voice cloning (design plan)

This document designs an **optional, two-pass voice cloning flow** that integrates into the existing pipeline without introducing duplicate pipelines.

Core constraints:
- **No duplicate pipelines**: reuse existing diarization, TTS, mixing, and job execution flow.
- **Optional**: controlled via config + CLI/web runtime flag.
- **Default enablement**: **enabled by default in HIGH mode only** (disabled in medium/low unless explicitly enabled).
- **Pass 2 does not redo ASR or translation**: it only reruns **TTS + mix** (and any dependent “cheap” packaging steps needed to reflect the new mixed audio).
- **Safe fallbacks**: if any part of pass 2 fails, keep pass 1 outputs and exit cleanly.
- **Detailed logging**: every decision should be explainable from job logs.

---

## 1) Current pipeline facts (where things live today)

### 1.1 Diarization outputs (files and structure)

The canonical job runner is `src/dubbing_pipeline/jobs/queue.py`. During the diarization step it writes:

- **Work diarization JSON** (includes local wav paths for downstream TTS):
  - `Output/<job_id>/work/diarization.work.json`
- **Public diarization JSON** (safe for UI; excludes temp wav paths):
  - `Output/<job_id>/diarization.json`

During diarization it also produces temporary per-utterance wav segments under:
- `Output/<job_id>/work/segments/`

Each diarization work segment contains (shape is created in `jobs/queue.py`):
- `start`, `end`
- `diar_label` (the diarizer speaker label)
- `speaker_id` (the canonical speaker identity used for downstream TTS/translation)
- `wav_path` (path to an utterance wav segment)

### 1.2 Where speaker IDs are generated (label → speaker_id)

Speaker labels originate from `src/dubbing_pipeline/stages/diarization.py`:
- The diarizer returns utterances with `speaker` like `SPEAKER_01`, `SPEAKER_02`, etc.

The job runner then converts diarizer labels into canonical `speaker_id` values in `src/dubbing_pipeline/jobs/queue.py`:
- **If voice memory is enabled**: label → stable `character_id` from `dubbing_pipeline.voice_memory.store.VoiceMemoryStore`
  - This is the preferred cross-episode identity store.
- **Otherwise**: label → diarization label (no cross-episode mapping).

Result: `diarization.work.json` segments are written with `speaker_id` already “canonicalized”.

### 1.3 Where translation assigns speaker IDs to text segments

The translation builder path in `src/dubbing_pipeline/jobs/queue.py` uses diarization utterances for timing and speaker assignment. Output is written to:
- `Output/<job_id>/translated.json` (unless privacy/no-store redirects it to workdir)

Each translated segment carries speaker attribution under:
- `speaker` (or `speaker_id` depending on producer; TTS normalizes these)

### 1.4 Where TTS selects speaker references today

The canonical TTS stage is `src/dubbing_pipeline/stages/tts.py` (`tts.run`).

Inputs relevant to voice selection:
- `translated_json`: provides the per-segment `speaker_id`/`speaker`.
- `diarization_json` (work version preferred): provides `segments[].wav_path` and optional `speaker_embeddings`.
- `voice_memory` (optional): provides `best_ref(character_id)` and per-character preferences.
- `VOICE_REF_DIR` (optional): a directory where stable reference wavs can be placed.
- `VOICE_STORE` (always present): a persistent store where the stage may write `data/voices/<speaker_id>/ref.wav`.

Current effective priority (simplified from `stages/tts.py`):
- Per-job voice map overrides (optional)
- Series character ref (when speaker → character mapping exists)
- Per-job extracted ref (`VOICE_REF_DIR`)
- `TTS_SPEAKER_WAV` (global override)
- Preset voices / basic/espeak fallback (handled later in TTS)

Important consequence for this design:
- Job-local refs in `VOICE_REF_DIR` are the canonical speaker refs and are preferred over global overrides.

---

## 2) Goal: “Two-pass automatic voice cloning”

High-level intent:
- **Pass 1** runs the normal pipeline to produce:
  - diarization (`diarization.work.json`)
  - translation (`translated.json`)
  - TTS clips + aligned dialogue wav
  - mixed dub output
- **Pass 2** uses pass-1 artifacts to build **better per-speaker reference wavs** and then reruns:
  - **TTS** (only; reusing the same `translated.json` and `diarization.work.json`)
  - **mix** (using the same background/dialogue stems and mix settings)
  - (Any cheap downstream packaging that must reflect updated audio, e.g., mux/export, should be treated as a dependent step; no ASR/translation rerun.)

What “better reference” means (design definition):
- Construct a per-speaker reference wav that is:
  - long enough (target seconds budget)
  - speech-only (trim silence/music where possible)
  - consistent sample rate / channel layout required by XTTS cloning
  - stable path (survives workdir pruning)
  - traceable (metadata explains what source segments were used)

---

## 3) Proposed integration points (no duplicate pipeline)

### 3.1 Insert reference extraction after diarization mapping (best location)

Insert a **reference extraction helper** in `src/dubbing_pipeline/jobs/queue.py` immediately after:
- diarization utterances have been produced, and
- label → `speaker_id` mapping has been resolved, and
- per-utterance wav segments are available under `Output/<job_id>/work/segments/`.

Why here:
- We already have `speaker_id` canonicalized.
- We already have per-speaker grouped wav segments (`by_label`) and extracted speaker refs.
- We can build a “ref bundle” once, then pass it into TTS for both pass 1 and pass 2.

This helper should **not** be a new end-to-end pipeline. It is a small deterministic step inside the existing job runner.

### 3.2 Where the extracted references should live (stable, job-local)

Write extracted refs under the job output directory so they persist:
- `Output/<job_id>/analysis/voice_refs/<speaker_id>.wav`

Also write a metadata file for auditability:
- `Output/<job_id>/analysis/voice_refs/meta.json`
  - speaker_id → selected source segments
  - total duration
  - extraction method and parameters
  - any fallbacks used

Optionally mirror (or link) into the persistent store:
- `data/voices/<speaker_id>/ref.wav`

But job-local refs are the primary mechanism for pass 2 (job reproducibility).

### 3.3 How TTS should consume “pass-2 refs”

Two-pass requires a deterministic preference order so that pass 2 uses the extracted refs.

Proposed behavior (only when pass 2 is enabled/active):
- Prefer **job-local extracted refs** first:
  - `Output/<job_id>/analysis/voice_refs/<speaker_id>.wav`
- Then fall back to series character refs (if mapping exists).
- Then fall back to global `TTS_SPEAKER_WAV` / preset voices / espeak fallback (existing behavior).

Implementation note:
- `stages/tts.py` already has `voice_ref_dir` support but it is currently checked late.
- Add a small, explicit “ref selection policy” branch that is only used when:
  - `two_pass_voice_clone` is enabled and
  - we are in pass 2 (see trigger design below).

---

## 4) Reference extraction algorithm (pass 2 input builder)

### 4.1 Inputs available without rerunning ASR/translation

We can build refs using only artifacts from pass 1:
- `Output/<job_id>/work/segments/*.wav` (diarization utterance wavs)
- `Output/<job_id>/work/diarization.work.json` (speaker_id assignment + wav paths)
- Optional speech VAD utilities already used by diarization (`dubbing_pipeline.utils.vad`)

We do **not** need:
- ASR outputs beyond what already exists
- translation outputs beyond `translated.json`

### 4.2 Speaker ref construction (deterministic, safe defaults)

For each `speaker_id`, build a ref wav by:
- Select top-N segments by:
  - duration (prefer longer segments)
  - confidence (if present)
  - optionally exclude very short segments (e.g., < 0.7s)
- Concatenate until a target duration budget is reached (e.g., 6–12 seconds).
- Apply safety normalization:
  - enforce mono, 16kHz PCM
  - trim leading/trailing silence (best-effort; VAD gate)
  - reject empty output (fallback to the longest single segment)

Write:
- `Output/<job_id>/analysis/voice_refs/<speaker_id>.wav`
- `Output/<job_id>/analysis/voice_refs/meta.json` with the segment list used.

Safe fallbacks:
- If extraction fails for a speaker:
  - leave the ref missing and log the reason
  - pass 2 will fall back to series refs or preset voices.

---

## 5) Triggering pass 2 (rerun only TTS + mix)

### 5.1 New job/runtime flag (optional)

Add a boolean flag:
- `two_pass_voice_clone` (name TBD)

Sources:
- **Config** (`config/public_config.py`): new setting with default `False`.
- **Mode defaults** (`src/dubbing_pipeline/modes.py`): default `True` in HIGH mode only.
- **CLI** (`src/dubbing_pipeline/cli.py`): add `--two-pass-voice-clone on|off` (or `--two-pass-voice-clone/--no-two-pass-voice-clone`).
- **Web submit**: carry it in job runtime payload (same pattern as other runtime flags).

### 5.2 Pass 2 state marker (prevent loops)

Persist pass state in job runtime (stored in SQLite job record):
- `runtime.two_pass_voice_clone = {"enabled": true, "pass": 1|2, "done": bool, "error": "..."}`

Rules:
- If enabled and not done:
  - run pass 1 normally
  - generate refs
  - run pass 2 (TTS + mix only)
  - set `done=true`
- If pass 2 fails:
  - record `error` and keep pass 1 outputs (do not fail the whole job unless the project wants strictness)

### 5.3 How to rerun only TTS + mix (no ASR/translation)

In `src/dubbing_pipeline/jobs/queue.py`, after pass 1 completes the “translation” stage, we already have:
- `translated.json`
- `diarization.work.json`

To run pass 2:
- Skip transcription/translation entirely.
- Call `dubbing_pipeline.stages.tts.run(...)` again, but:
  - provide `voice_ref_dir=Output/<job_id>/analysis/voice_refs`
  - ensure a “pass 2” marker is included so TTS prefers extracted refs (see section 3.3)
  - write outputs to a separate, pass-tagged location under workdir, then atomically swap the final wav artifacts
- Rerun mixing with the new TTS dialogue wav and the already-computed background/dialogue stems (if enabled).

Checkpoint behavior:
- Do not reuse the pass-1 `tts` checkpoint for pass 2.
- Either:
  - include `pass=2` in the TTS cache key / checkpoint marker, or
  - bypass checkpoint if `two_pass_voice_clone.pass == 2`.

---

## 6) Logging and observability requirements

Write explicit job log lines (via `JobStore.append_log`) and structured logs for:
- diarization speaker count and final `speaker_id` set
- reference extraction summary:
  - per speaker: segment count, total seconds, output path
  - any fallbacks (no segments, too short, extraction failure)
- pass 2 execution:
  - “starting pass 2 (TTS+mix only)”
  - TTS ref selection per speaker (source: extracted_ref | voice_memory_ref | diar_rep | global_override | preset)
  - “pass 2 complete; outputs replaced” (or “pass 2 skipped/failed; keeping pass 1”)

---

## 7) Safe fallback policy (must never make jobs worse)

Pass 2 must be **best-effort**:
- If refs cannot be extracted, skip pass 2.
- If pass 2 TTS fails, keep pass 1 audio artifacts and continue to finalization.
- If only some speakers have refs, allow partial improvement:
  - per-segment fallback remains the same as today.

---

## Summary: required answers (from prompt)

- **Where diarization outputs live**:
  - `Output/<job_id>/work/diarization.work.json`
  - `Output/<job_id>/diarization.json`
  - `Output/<job_id>/work/segments/` (per-utterance wavs)
- **Where speaker IDs are generated**:
  - labels produced by `src/dubbing_pipeline/stages/diarization.py`
  - canonical `speaker_id` assigned in `src/dubbing_pipeline/jobs/queue.py` via voice memory (preferred) or diarization label fallback
- **Where TTS selects speaker refs**:
  - `src/dubbing_pipeline/stages/tts.py` (speaker_wav selection logic; series refs + job refs + global override)
- **Which stage to insert reference extraction**:
  - inside `src/dubbing_pipeline/jobs/queue.py` immediately after diarization mapping (`lab_to_char`) and per-speaker segment grouping is available
- **How to trigger pass 2 rerun**:
  - job/runtime flag `two_pass_voice_clone` (enabled by default only in HIGH mode)
  - rerun only `tts.run` + mixing with pass-2 ref preference and a pass-state marker to avoid loops and avoid ASR/translation reruns

