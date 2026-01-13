"""
Security & Mobile Gate

This wrapper runs the repo's verification scripts that together validate:
- hardened auth (incl. QR login)
- upload safety
- job lifecycle + queue/progress
- optional private ntfy notifications
- mobile playback variants
- no secret leaks to logs

It is safe to run on synthetic media (no real inputs required).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    rel: str
    optional: bool = False


STEPS: list[Step] = [
    Step("verify_env", "scripts/verify_env.py"),
    Step("verify_auth_flow", "scripts/verify_auth_flow.py"),
    Step("verify_qr_login", "scripts/verify_qr_login.py"),
    # upload safety (traversal, oversize limits, allowlists)
    Step("security_file_smoke", "scripts/security_file_smoke.py"),
    # broader security smoke (CORS/CSRF/rate limits, etc.)
    Step("security_smoke", "scripts/security_smoke.py"),
    Step("verify_job_submission", "scripts/verify_job_submission.py"),
    Step("verify_playback_variants", "scripts/verify_playback_variants.py"),
    # optional by design (script exits 0 if not configured)
    Step("verify_ntfy", "scripts/verify_ntfy.py", optional=True),
    Step("verify_no_secret_leaks", "scripts/verify_no_secret_leaks.py"),
]


def _run(step: Step) -> int:
    p = (ROOT / step.rel).resolve()
    if not p.exists():
        print(f"[skip] {step.name}: missing {step.rel}")
        return 0
    print(f"\n==> {step.name}")
    r = subprocess.run([PY, str(p)], cwd=str(ROOT))
    if r.returncode != 0:
        label = "warn" if step.optional else "fail"
        print(f"[{label}] {step.name} exited {r.returncode}")
    return int(r.returncode)


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    for step in STEPS:
        rc = _run(step)
        if rc != 0:
            if step.optional:
                warnings.append(step.name)
            else:
                failures.append(step.name)

    print("\n---\nSummary:")
    if failures:
        print("FAILED:")
        for n in failures:
            print(f"- {n}")
    else:
        print("FAILED: none")
    if warnings:
        print("WARN:")
        for n in warnings:
            print(f"- {n}")
    else:
        print("WARN: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

