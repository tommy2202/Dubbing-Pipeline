# Fresh machine smoke test

This smoke test validates a minimal end-to-end path on a clean machine:

- Generates a tiny MP4 via ffmpeg
- Submits a low-mode job through the API
- Waits for completion
- Verifies output artifact + manifest + default visibility

## Pytest

```
pytest -k smoke_fresh_machine
```

If ffmpeg is missing, the test skips with:
`ffmpeg not available; install ffmpeg to run smoke test`.

## Script wrapper (optional)

```
python3 scripts/smoke_fresh_machine.py
```

The script uses a temporary workspace and exits non-zero if ffmpeg is missing.

## Post-upgrade gate

To run the full post-upgrade gate (including this smoke test when ffmpeg is
available), use:

```
python3 scripts/post_upgrade_gate.py
```

## Notes

- No GPU is required.
- The test does not log tokens or transcript content.
