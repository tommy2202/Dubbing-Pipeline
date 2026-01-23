# V0 Security Checklist

Run this gate before inviting others to use the system:

```bash
python3 scripts/v0_gate.py
```

## What it verifies

The v0 gate runs fast, CPU-only checks:

- Object-level access enforcement
- Safe Range streaming
- Trusted proxy handling
- Single-writer SQLite locks
- Retention sweeper behavior
- Log redaction
- Basic server startup in test mode

If any required protection is missing, the gate fails with a clear error.
