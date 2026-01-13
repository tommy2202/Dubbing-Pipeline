## Release checklist (safe zip workflow)

### 1) Run cleanup gate

- Ensure no sensitive runtime DBs or artifact zips are present:

```bash
python3 scripts/check_no_sensitive_runtime_files.py
```

### 2) Build release zip (allowlist)

```bash
python3 scripts/package_release.py --out dist
```

### 3) Verify release zip contents (no runtime artifacts)

```bash
python3 scripts/verify_release_zip.py dist/dubbing-pipeline-release-*.zip
```

### 4) Run smoke tests

```bash
make check
python3 scripts/security_smoke.py
python3 scripts/verify_auth_flow.py
```

