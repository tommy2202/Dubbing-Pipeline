# Repo hygiene plan (Phase 0 inventory)

Goal: make the repo safe/reproducible to clone and run on a fresh machine by **stopping git from tracking runtime artifacts** (inputs/outputs/tmp/logs/caches/build outputs) and by adding guardrails (CI + local checks). This document is **inventory + plan only**.

Date: 2026-01-05

---

## 1) Runtime artifacts currently tracked in git (exact paths / categories)

These were identified from `git ls-files` (i.e., **tracked in the index**, not merely present on disk).

### Summary (counts + sample paths)

- **`Input/` (1 tracked)**:
  - `Input/Test.mp4`

- **`backups/` (2 tracked)**:
  - `backups/backup-20260101-0254.manifest.json`
  - `backups/backup-20260101-0254.zip`

- **`_tmp*/` (33 tracked)**:
  - `/_tmp_director/audio.wav`
  - `/_tmp_director/expressive/director_plans.jsonl`
  - `/_tmp_music_detect/*.wav` (many)
  - `/_tmp_ops*/Output/*.db` (auth/jobs db snapshots)
  - `/_tmp_qa_job/**` (QA, review audio, manifests)
  - `/_tmp_speaker_smoothing/**`
  - `/_tmp_style_guide/style_guide.json`

- **`build/` (164 tracked)**:
  - `build/lib/**` (Python build output; should not be tracked)

- **`data/reports/` (4 tracked)**:
  - `data/reports/default/episodes/*.json`
  - `data/reports/default/season_report.md`

- **Python caches (96 tracked total)**:
  - `__pycache__/main.cpython-312.pyc`
  - `src/**/__pycache__/*.pyc` (many)
  - `tests/**/__pycache__/*.pyc` (many)
  - `tools/__pycache__/*.pyc`

### Other tracked artifacts (outside the categories above)

From `git ls-files` output, these are also notable “runtime-ish” items to review:

- **Voice embeddings**:
  - `voices/embeddings/Speaker1.npy` (looks runtime-generated; likely should not be tracked)

### How to reproduce this inventory locally

Run:

```bash
git ls-files
```

And for a focused list (heuristic):

```bash
python3 - <<'PY'
import subprocess, re
files = subprocess.check_output(['git','ls-files'], text=True).splitlines()
deny = re.compile(r'^(Input/|Output/|logs/|backups/|build/|dist/|data/reports/|__pycache__/|_tmp)|/__pycache__/|\\.pyc$|\\.(db|log)$', re.I)
for f in files:
    if deny.search(f):
        print(f)
PY
```

---

## 2) Existing `.gitignore` patterns and gaps

### Current `.gitignore` (high-level)

Already present:

- **Secrets**: `.env`, `.env.secrets`, `secrets/`, `*.key`, `*.pem`
- **Python**: `__pycache__/`, `*.pyc`, `*.egg-info/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`
- **Runtime**: `logs/`, `Output/`, `Input/`, `uploads/`, `outputs/`, `_tmp*/`, `*.db`, `Input/uploads/`
- **ntfy local config**: `docker/ntfy/server.yml`

### Where it falls short (based on what’s currently tracked)

- **Tracked artifacts won’t be ignored retroactively**: even if ignored, they remain tracked until removed from the index (`git rm --cached ...`).
- **Missing patterns for common build outputs**:
  - `build/` is currently tracked and not ignored by the existing rules.
  - `dist/` is not ignored (even if currently absent).
- **Missing patterns for backups and generated reports**:
  - `backups/` is currently tracked and not ignored.
  - `data/reports/` is currently tracked and not ignored.
- **Missing patterns for archive outputs** (nice-to-have safety):
  - `*.zip`, `*.tar.gz`, etc. (backups are zip).
- **Voice embedding artifacts**:
  - `voices/embeddings/*.npy` is currently tracked; likely should be ignored.

---

## 3) Proposed `.gitignore` additions/adjustments

Proposed patterns (in addition to what already exists):

### Build artifacts

- `build/`
- `dist/`

### Backups and generated reports

- `backups/`
- `**/backups/`
- `data/reports/`

### Archives and large generated files (safe defaults)

- `*.zip`
- `*.tar`
- `*.tar.gz`
- `*.tgz`

### Voice embedding artifacts (runtime)

- `voices/embeddings/`
- `voices/**/*.npy`

### Runtime I/O placeholders (later phase)

We want to ignore everything under `Input/` and `Output/` **except** `.gitkeep` files:

- `Input/**`
- `!Input/.gitkeep`
- `Output/**`
- `!Output/.gitkeep`

(Current ignore rules already ignore `Input/` and `Output/`, but we’ll make it explicit and compatible with placeholders.)

---

## 4) What will be untracked vs kept

### Untrack (remove from git index, keep on disk)

These are currently tracked and should become untracked:

- `Input/Test.mp4` (Input is runtime-only; sample files belong under `samples/`)
- `__pycache__/main.cpython-312.pyc`
- `src/**/__pycache__/**`
- `tests/**/__pycache__/**`
- `tools/__pycache__/**`
- `_tmp*/**`
- `backups/**`
- `build/**` (especially `build/lib/**`)
- `data/reports/**`
- `voices/embeddings/Speaker1.npy` (and likely the whole `voices/embeddings/` directory)

### Keep tracked

- `.env.example` and `.env.secrets.example` (templates only; no real secrets)
- `samples/` content (example media intended for repo)
- `voices/presets/README.md`, `voices/registry.json`, and preset config sources (these look like “source assets”)

---

## 5) Existing verification scripts (“gates”) inventory + expected order

Scripts present under `scripts/` include (non-exhaustive highlights):

- **Environment / import sanity**
  - `scripts/verify_env.py`
  - `scripts/smoke_import_all.py`

- **Existing gates**
  - `scripts/polish_gate.py`
  - `scripts/mobile_gate.py`
  - `scripts/security_mobile_gate.py`
  - `scripts/security_smoke.py`
  - `scripts/security_file_smoke.py`

- **Other verifiers** (examples)
  - `scripts/verify_auth_flow.py`, `verify_qr_login.py`, `verify_sessions.py`
  - `scripts/verify_job_submission.py`, `verify_playback_variants.py`, `verify_mobile_outputs.py`
  - `scripts/verify_rbac.py`, `verify_api_keys.py`, `verify_no_secret_leaks.py`

### Proposed “canonical” order for CI gate execution

In later phases, CI should run scripts in this order (skipping missing scripts):

1. `scripts/check_no_tracked_artifacts.py` (new)
2. `scripts/check_no_secrets.py` (new)
3. `python scripts/verify_env.py`
4. `python scripts/smoke_import_all.py`
5. `python scripts/polish_gate.py` *(if present)*
6. `python scripts/security_smoke.py` *(if present)*
7. `python scripts/mobile_gate.py` *(if present)*
8. `python scripts/security_mobile_gate.py` *(if present)*

---

## 6) Proposed CI updates (high-level)

Later phases will:

- Add “repo hygiene” guardrails:
  - fail if artifact paths are tracked
  - fail on high-confidence secret patterns
- Keep existing `make check` gate for code health, and run hygiene gates before expensive steps.
- Cache pip to speed CI.

---

## 7) Proposed implementation approach (what comes next)

After this plan:

- **Phase 1**: expand `.gitignore` + add `Input/.gitkeep` and `Output/.gitkeep`; add `docs/REPO_LAYOUT.md`.
- **Phase 2**: provide opt-in cleanup scripts and run `git rm --cached` for tracked artifacts **without deleting local files**.
- **Phase 3–6**: add guard scripts, CI integration, and runbooks/checklists.

