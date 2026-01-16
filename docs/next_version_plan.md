## Next version plan (features A–M)

### Current canonical modules map (what exists today)

#### Core pipeline

- **Config (public + secret + merged settings)**: `config/public_config.py`, `config/secret_config.py`, `config/settings.py`
- **CLI entrypoint**: `src/dubbing_pipeline/cli.py` (default group + subcommands)
- **Web server**: `src/dubbing_pipeline/server.py`, `src/dubbing_pipeline/web/app.py`, templates under `src/dubbing_pipeline/web/templates/`
- **Job queue orchestration**: `src/dubbing_pipeline/jobs/queue.py`, `src/dubbing_pipeline/jobs/store.py`, `src/dubbing_pipeline/jobs/models.py`
- **Stages**:
  - audio extract: `src/dubbing_pipeline/stages/audio_extractor.py`
  - diarization: `src/dubbing_pipeline/stages/diarization.py`
  - transcription: `src/dubbing_pipeline/stages/transcription.py`
  - translation: `src/dubbing_pipeline/stages/translation.py`
  - alignment: `src/dubbing_pipeline/stages/align.py`
  - TTS: `src/dubbing_pipeline/stages/tts.py`, provider selection `src/dubbing_pipeline/stages/tts_engine.py`
  - mixing/export: `src/dubbing_pipeline/audio/mix.py`, `src/dubbing_pipeline/stages/mixing.py`, `src/dubbing_pipeline/stages/export.py`
- **Timeline / pacing**: `src/dubbing_pipeline/timing/fit_text.py`, `src/dubbing_pipeline/timing/pacing.py`

#### Tier features already in place

- **Music detection/preservation**: `src/dubbing_pipeline/audio/music_detect.py`
- **PG filter**: `src/dubbing_pipeline/text/pg_filter.py`
- **Style guide**: `src/dubbing_pipeline/text/style_guide.py` (+ `projects/example/style_guide.yaml`)
- **Speaker smoothing**: `src/dubbing_pipeline/diarization/smoothing.py`
- **Director mode**: `src/dubbing_pipeline/expressive/director.py`
- **QA scoring + CLI**: `src/dubbing_pipeline/qa/scoring.py`, `src/dubbing_pipeline/qa/cli.py`
- **Voice memory store**: `src/dubbing_pipeline/voice_memory/store.py`, embeddings `src/dubbing_pipeline/voice_memory/embeddings.py`
- **Review loop**: `src/dubbing_pipeline/review/state.py`, `src/dubbing_pipeline/review/ops.py`, `src/dubbing_pipeline/review/cli.py`
- **Streaming**: `src/dubbing_pipeline/streaming/chunker.py`, `src/dubbing_pipeline/streaming/runner.py`
- **Lip-sync plugin**: `src/dubbing_pipeline/plugins/lipsync/base.py`, `registry.py`, `wav2lip_plugin.py`
- **Multi-track artifacts/mux**: `src/dubbing_pipeline/audio/tracks.py`, `src/dubbing_pipeline/stages/export.py:export_mkv_multitrack`

#### Ops / retention / caching / logs

- **Workdir pruning**: `src/dubbing_pipeline/ops/storage.py` (server periodic prune)
- **Retention**: `src/dubbing_pipeline/ops/retention.py` (inputs + logs)
- **Cross-job cache**: `src/dubbing_pipeline/cache/store.py` (index.json)
- **Stage checkpoints**: `src/dubbing_pipeline/jobs/checkpoint.py`
- **Stage manifests (best-effort)**: `src/dubbing_pipeline/jobs/manifests.py`
- **Per-job logs helper**: `src/dubbing_pipeline/utils/job_logs.py`
- **FFmpeg runner with stderr capture**: `src/dubbing_pipeline/utils/ffmpeg_safe.py`

### Conflict list (overlaps / duplicates to delete or reroute)

- **Mode logic is duplicated**:
  - `src/dubbing_pipeline/cli.py` has `MODE_TO_MODEL` + ad-hoc defaults.
  - `src/dubbing_pipeline/jobs/queue.py` has its own `MODE_TO_MODEL`.
  - **Plan**: introduce one canonical resolver (new module) and route both CLI and queue through it; keep existing CLI flags as overrides.
- **Project “profiles” split across sources**:
  - style guide uses `projects/<name>/style_guide.yaml` (canonical)
  - web has “projects” objects in `jobs/store.py` via `/api/projects`
  - mix profile is separate (`mix_profile`, `mix_mode`) and QA thresholds are hardcoded/defaulted.
  - **Plan**: unify into a single “project profile” schema, backed by `projects/<name>/profile.yaml` (and optionally mirrored in the store).
- **Subtitle generation is implemented in multiple places**:
  - `utils/subtitles.py` has minimal writers, but CLI/queue also manually format SRT.
  - **Plan**: add a canonical formatting pass in `utils/subtitles_format.py` (or extend `utils/subtitles.py`) and route all SRT/VTT writes through it.
- **Retention + cache policy are not unified**:
  - retention only removes inputs + logs; cache/store has no TTL policy; separation has its own cache dir; model caches live elsewhere.
  - **Plan**: add a single policy layer and make retention aware of caches and per-job artifacts, without creating parallel mechanisms.

### Feature-by-feature design specs

For each feature: **data model**, **pipeline insertion**, **fallbacks**, **CLI/config/UI**.

#### A) Mode Contract Tests (prevent mode drift)

- **Data model**: `docs/mode_contract_matrix.md` is the source-of-truth table.
- **Implementation**:
  - `scripts/verify_modes.py` reads the matrix (simple markdown table parse) and asserts the resolver produces matching `EffectiveConfig`.
  - Add unit tests under `tests/` that assert key flags for HIGH/MED/LOW.
- **Pipeline insertion**: N/A (tests only).
- **Fallbacks**: if parser fails, test fails with actionable error.
- **CLI/config/UI**: none.

#### B) Artifact retention + cache policy levels (full/balanced/minimal)

- **Data model**:
  - New setting: `retention_policy: full|balanced|minimal`
  - New per-job metadata: `Output/<job>/manifests/retention.json` (policy applied, what was kept/pruned).
- **Pipeline insertion**:
  - Extend `ops/retention.py` to:
    - prune caches under `Output/cache/` and any configured `cache_dir` based on policy
    - prune per-job artifacts (clips, chunk dirs, tmp, analysis) based on policy and job age
  - Extend server periodic pruning to respect the policy.
- **Fallbacks**:
  - If deletion fails, log warnings; never fail the running job.
- **CLI/config/UI**:
  - CLI flag: `--retention-policy full|balanced|minimal` (job-level override)
  - Config keys: `RETENTION_POLICY`, optional per-policy TTLs.
  - UI: optional dropdown in settings (admin).

#### C) Per-project profiles (style guide + QA thresholds + audio mix presets)

- **Data model**:
  - `projects/<name>/profile.yaml` (canonical) containing:
    - `style_guide_path` (or embed style guide config)
    - `qa` thresholds (speaking rate, clipping, drift tolerances)
    - `audio_mix` preset (mix_mode, lufs, ducking strength, limiter)
    - optional `subtitle_format` preset (see E)
  - Store effective profile snapshot at `Output/<job>/analysis/project_profile.json`.
- **Pipeline insertion**:
  - CLI/queue: load profile early (after output dir selection), apply to defaults before stage runs.
  - QA scoring consumes thresholds from effective profile.
  - Mixing consumes audio preset from effective profile.
- **Fallbacks**:
  - Missing profile: proceed with global defaults.
  - Invalid profile: log warning, ignore invalid sections.
- **CLI/config/UI**:
  - CLI: `--project <name>` already exists; extend to load profile.
  - UI: project picker already exists; extend project object to include profile path/name.

#### D) UI Overrides: music regions + speaker smoothing changes

- **Data model**:
  - `Output/<job>/analysis/music_regions.override.json` (manual overrides)
  - `Output/<job>/analysis/speaker_smoothing.override.json` (manual scene boundaries or forced speaker ids)
- **Pipeline insertion**:
  - music: `audio/music_detect.py` should accept override regions that replace/merge detected regions.
  - smoothing: `diarization/smoothing.py` should accept manual scene cuts and/or manual speaker merges.
  - QA: should reflect overrides (e.g. music overlap warnings).
- **Fallbacks**:
  - overrides invalid: ignore with warning; keep detector output.
- **CLI/config/UI**:
  - UI:
    - job detail page: add editor for regions list (start/end/kind/confidence/reason)
    - smoothing overrides: allow marking a scene cut and assigning speaker id corrections
  - API endpoints:
    - `PUT /api/jobs/{id}/analysis/music_regions`
    - `PUT /api/jobs/{id}/analysis/speaker_smoothing`

#### E) Subtitle formatting pass (line breaks, max chars, durations)

- **Data model**:
  - `SubtitleFormatConfig`: `max_chars_per_line`, `max_lines`, `min_duration_s`, `max_duration_s`, `max_cps`, `prefer_sentence_breaks`.
  - Optionally store applied formatting report: `Output/<job>/analysis/subtitle_format.json`.
- **Pipeline insertion**:
  - Create/extend `utils/subtitles.py` to include `format_subtitles(lines, cfg)`.
  - Route all SRT/VTT generation through it: CLI, queue, streaming.
- **Fallbacks**:
  - formatting fails: write minimal subtitles as today.
- **CLI/config/UI**:
  - CLI: `--subs-formatting <preset>` or explicit flags.
  - Project profile can define defaults.

#### F) Character merge tools for voice memory (CLI + optional UI)

- **Data model**:
  - Extend `voice_memory/store.py` with `merge_characters(src_id, dst_id, strategy=...)`:
    - move refs/embeddings
    - update episodes mappings
    - mark `src_id` as merged/alias
  - Keep audit log: `data/voice_memory/merge_log.jsonl`
- **Pipeline insertion**: N/A (management operations).
- **Fallbacks**:
  - If embeddings unavailable: still merge refs and metadata.
- **CLI/config/UI**:
  - CLI: `--merge-character <from> <to> [--delete-from]`
  - UI: optional “merge” button in job characters panel.

#### G) Voice audition tool (top-N candidate voices)

- **Data model**:
  - Output: `Output/<job>/analysis/voice_audition.json` listing candidates with similarity and preview paths.
- **Pipeline insertion**:
  - Extend voice memory matching step to optionally compute top-N matches for each diar label / ref wav.
  - Add a CLI subcommand: `voice audition <wav> --top N`.
- **Fallbacks**:
  - If embedding provider missing: return empty candidates with reason.
- **CLI/config/UI**:
  - CLI: `dubbing-pipeline voice audition ...`
  - UI: show candidate list in characters tab.

#### H) QA warning for heavy timing-fit compression (“rewrite-heavy”)

- **Data model**:
  - Record per-segment fit stats already exist in debug artifacts; standardize a `fit_ratio` (est_before / target).
- **Pipeline insertion**:
  - In timing-fit module, compute `rewrite_heavy = fit_ratio >= threshold`.
  - QA scoring reads segment debug jsonl (or summary) and emits issue `rewrite_heavy`.
- **Fallbacks**:
  - No timing debug artifacts: skip check.
- **CLI/config/UI**:
  - QA config threshold from project profile / settings.

#### I) Streaming context bridging across chunks

- **Data model**:
  - `Output/<job>/stream/context.json` storing:
    - glossary/style guide state
    - last chunk trailing transcript
    - speaker mapping carry-over (if introduced)
- **Pipeline insertion**:
  - `streaming/runner.py`: carry context from previous chunk to next:
    - overlap reconciliation (dedupe repeated text)
    - translation context hints (non-LLM)
    - optional TTS continuity (voice ids)
- **Fallbacks**:
  - If context file missing/corrupt: proceed per-chunk as today.
- **CLI/config/UI**:
  - CLI: `--stream-context on|off` (default on for streaming).

#### J) Scene-limited lip-sync + face-detect preview (plugin improvements)

- **Data model**:
  - `Output/<job>/analysis/lipsync_plan.json` listing time ranges to apply lipsync.
  - `Output/<job>/analysis/face_preview/` thumbnails or short mp4 (best-effort).
- **Pipeline insertion**:
  - Lip-sync plugin request should accept:
    - optional `segments` / time ranges to process
    - optional face bbox preview step before full run
  - Implement with ffmpeg slicing + stitch (avoid full video processing when not needed).
- **Fallbacks**:
  - If face detect unavailable: fall back to center crop/bbox.
  - If segmentation fails: run full lipsync or skip based on strict flag.
- **CLI/config/UI**:
  - CLI: `--lipsync-range auto|scene|full`, `--lipsync-preview`
  - UI: “Preview face box” button.

#### K) Per-character delivery profiles (rate/energy/pause style)

- **Data model**:
  - Extend `voice_memory/characters.json` per character:
    - `delivery`: `{rate_mul, pitch_mul, energy_mul, pause_ms, style_tags}`
- **Pipeline insertion**:
  - `stages/tts.py`: when character_id resolved, apply delivery defaults before director/expressive overrides.
- **Fallbacks**:
  - Missing delivery: neutral multipliers.
- **CLI/config/UI**:
  - CLI: `--set-character-delivery <id> ...`
  - UI: edit delivery parameters in characters tab.

#### L) Cross-episode drift reports (voices + glossary consistency)

- **Data model**:
  - `data/voice_memory/reports/drift_<date>.json`
  - `Output/<job>/analysis/drift_report.json` (job-local)
- **Pipeline insertion**:
  - Offline tool that scans:
    - voice similarity changes across episodes (embedding drift)
    - glossary/style-guide forbidden terms hits frequency
  - Integrate into QA as informational summary.
- **Fallbacks**:
  - No historical episodes: skip with info.
- **CLI/config/UI**:
  - CLI: `dubbing-pipeline voice drift-report --show-id ...`
  - UI: optional “Drift” tab under project page.

#### M) Optional OFFLINE LLM transcreation/rewrite provider hook

- **Data model**:
  - Provider interface: `TranscreationProvider.rewrite(text, target_seconds, constraints) -> text`
  - Config keys:
    - `transcreate: off|on`
    - `transcreate_provider: none|local_llm`
    - provider params (model path, context size)
- **Pipeline insertion**:
  - After translation + style + PG, before timing-fit:
    - if enabled: call provider to rewrite for duration while preserving meaning.
  - Must be optional and default OFF.
- **Fallbacks**:
  - provider missing: log warning; use heuristic timing-fit as today.
- **CLI/config/UI**:
  - CLI: `--transcreate on --transcreate-provider local`
  - UI: toggle in advanced settings (off by default).

### Staged implementation checklist (order)

1) **A + docs lock-in**: parse `docs/mode_contract_matrix.md`, add `scripts/verify_modes.py`, add CI hook in `polish_gate`.
2) **C (project profiles)**: introduce `projects/<name>/profile.yaml`, wire into CLI + queue; add QA/mix preset consumption.
3) **E (subtitle formatting)**: implement formatting pass and route SRT/VTT writes through it.
4) **B (retention/cache policy)**: extend `ops/retention.py` + cache store pruning; add per-policy defaults and tests.
5) **F + G (voice tools)**: merge characters + audition top-N; add CLI and minimal UI integration.
6) **H (rewrite-heavy QA check)**: add check fed by timing-fit stats.
7) **I (stream context bridging)**: add context file + overlap dedupe across chunks.
8) **J (lipsync improvements)**: scene-limited runs + face preview; keep plugin API stable.
9) **K (delivery profiles)**: store per-character delivery and apply in TTS.
10) **L (drift reports)**: add offline report generator + UI/QA surfacing.
11) **M (offline LLM hook)**: provider interface + safe defaults + hard OFF by default.

