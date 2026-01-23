## CI overview

This repo separates **core checks** (always required) from **optional release hardening** steps.

---

## Core CI checks (required)

These run on PRs and pushes to `main/master`:

- Lint/static checks (via `scripts/*gate*.py`)
- Unit tests (`pytest`)
- Verify scripts (security + environment)
- Packaging allowlist (`scripts/package_release.py` + `scripts/verify_release_zip.py`)

See `.github/workflows/ci-core.yml`.

---

## Optional release hardening (best-effort)

Release workflow: `.github/workflows/ci-release.yml`

These steps **only run when an image is pushed** (tag push or workflow dispatch with `push_image=true`).
They **skip cleanly** if required keys/env are missing:

- **SBOM generation** (Syft)
- **Vulnerability scan** (Trivy)
- **Cosign signing**
- **Cosign attest SBOM**
- **Build provenance attestation**

Signing/attestation requires:

- `COSIGN_PRIVATE_KEY`
- `COSIGN_PASSWORD`

If missing, the workflow emits a **SKIPPED** notice and continues.

---

## P1 reliability gate (nightly, non-blocking)

`scripts/p1_gate.py` runs lightweight E2E and verifier checks.
It is wired as **non-blocking** in the nightly CI (continue-on-error).

---

## Local checks

```bash
python3 scripts/v0_gate.py
python3 scripts/p1_gate.py
```
