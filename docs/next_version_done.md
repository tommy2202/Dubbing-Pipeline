## Next Version (A–M) — Done

This release implements the “Next Version” upgrades without introducing parallel systems. All new behavior is **opt-in** unless explicitly mode-gated (and still respects CLI overrides and project profiles).

### What changed (high level)
- **Mode contract tests (A)**: `scripts/verify_modes_contract.py` prevents mode drift.
- **Retention/cache policies (B)**: per-job safe cleanup via `--cache-policy` + `Output/<job>/analysis/retention_report.json`.
- **Per-project profiles (C)**: deterministic loader + job provenance artifacts; includes delivery profile overlays.
- **Overrides (D)**: per-job music/speaker/smoothing overrides stored under `Output/<job>/review/overrides.json` with CLI + UI wiring.
- **Subtitle formatting + variants (E)**: formatted `subs/*.srt`/`*.vtt` plus pre-format QA warnings.
- **Voice tools (F/G)**: merge/undo + audition WAVs with manifests.
- **QA UX (H)**: rewrite-heavy/pacing-heavy/outlier checks + direct “Fix” links to segment editor.
- **Streaming context bridging (I)**: overlap de-dup + context hint bridging across chunks + boundary QA checks.
- **Lip-sync improvements (J)**: `lipsync preview` + scene-limited lip-sync with per-range logs.
- **Per-character delivery profiles (K)**: rate/pause/expressive/voice-mode defaults per character (voice memory + project overlay).
- **Cross-episode drift reports (L)**: per-job `drift_report.md` + per-project `season_report.md`.
- **Offline LLM rewrite hook (M)**: optional local-only provider; always falls back to heuristic timing-fit.

### Quick test commands (≤6)

```bash
python3 scripts/polish_gate.py
python3 scripts/verify_retention.py
python3 scripts/verify_overrides.py
python3 scripts/verify_stream_context.py
python3 scripts/verify_drift_reports.py
python3 scripts/verify_rewrite_provider.py
```

### Known limitations / tuning tips
- **Local LLM rewrite (Feature M)**:
  - Endpoint must be **localhost-only**; if it fails or violates constraints, the pipeline falls back to heuristic timing-fit.
  - See `docs/offline_llm_rewrite.md` for setup.
- **Lip-sync scene limiting (Feature J)**:
  - Face detection is best-effort and depends on optional local tooling; if unavailable, scene-limited mode will skip lipsync and produce pass-through output.
- **Drift reports (Feature L)**:
  - Embedding similarity only compares when embeddings exist and match dimensionality.
  - Glossary drift counts are simple substring frequency over translated text (deterministic).

