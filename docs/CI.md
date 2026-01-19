## CI overview

This repo uses two workflows:
- `ci-core` for required lint/test/verify checks.
- `ci-release` for tagged container releases and optional hardening steps.

### Core CI (required)

`ci-core` runs on PRs and pushes to `main`/`master`. It is required to pass and includes:
- Guardrails (`scripts/check_no_tracked_artifacts.py`, `scripts/check_no_secrets.py`)
- Smoke imports and lightweight verify scripts
- Release zip build + verify
- Reliability gates (nightly only via `p1_gate.py`, see below)

Core CI should remain enabled and is not skipped by default.

### Optional release hardening

`ci-release` runs on tag pushes (`v*`) and builds the container image.
Release hardening steps are **optional** and only run when explicitly enabled:
- SBOM generation
- Image signing + SBOM attestation
- Build provenance attestation

To enable them, set one of the following:
- Workflow dispatch input: `release_hardening=true`
- Repository variable: `RELEASE_HARDENING=true`

Signing additionally requires secrets:
- `COSIGN_PRIVATE_KEY`
- `COSIGN_PASSWORD`

If hardening is not enabled, the workflow prints a **skipped** message and continues.

### Release packaging hygiene

Release zips are built by `scripts/package_release.py`. The packager excludes:
- caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`)
- tests (`tests/`)
- legacy sources (`src/dubbing_pipeline_legacy/` unless `INCLUDE_LEGACY=1`)
- build outputs (`dist/`, `build/`, `releases/`)

### P1 reliability gate

The P1 reliability gate can be run locally:

```bash
python scripts/p1_gate.py
```

It is wired into the nightly CI run and will **skip** gracefully if optional
dependencies (e.g., FastAPI or ffmpeg) are missing.
