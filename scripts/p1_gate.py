from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


CHECKS: list[tuple[str, str]] = [
    ("verify_mobile_upload", "scripts/verify_mobile_upload_flow.py"),
    ("verify_job_timeline", "scripts/verify_job_timeline.py"),
    ("verify_ntfy_notifications", "scripts/verify_ntfy_notifications.py"),
    ("verify_readiness", "scripts/verify_readiness.py"),
    ("e2e_two_users_submit", "scripts/e2e_two_users_submit.py"),
    ("e2e_upload_resume", "scripts/e2e_upload_resume.py"),
    ("e2e_cancel_midrun", "scripts/e2e_cancel_midrun.py"),
    ("e2e_restart_worker_midrun", "scripts/e2e_restart_worker_midrun.py"),
    ("e2e_redis_fallback", "scripts/e2e_redis_fallback.py"),
]


def _run_script(name: str, path: str, env: dict[str, str]) -> tuple[str, str, str]:
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path],
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return name, "FAIL", "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        lowered = out.lower()
        if any(tag in lowered for tag in ("skip", "skipped", "optional", "not available")):
            return name, "SKIP", "skipped"
        return name, "PASS", f"ok ({time.time() - t0:.2f}s)"
    return name, "FAIL", out.strip() or f"exit={proc.returncode}"


def main() -> int:
    env = os.environ.copy()
    env.setdefault("DUBBING_SKIP_STARTUP_CHECK", "1")
    env.setdefault("MIN_FREE_GB", "0")
    env.setdefault("COOKIE_SECURE", "0")
    env.setdefault("STRICT_SECRETS", "0")

    with tempfile.TemporaryDirectory(prefix="p1_gate_") as td:
        root = Path(td).resolve()
        env.setdefault("APP_ROOT", str(root))
        env.setdefault("INPUT_DIR", str(root / "Input"))
        env.setdefault("DUBBING_OUTPUT_DIR", str(root / "Output"))
        env.setdefault("DUBBING_LOG_DIR", str(root / "logs"))
        env.setdefault("DUBBING_STATE_DIR", str(root / "_state"))
        (root / "Input").mkdir(parents=True, exist_ok=True)
        (root / "Output").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "_state").mkdir(parents=True, exist_ok=True)

        failed = False
        for name, path in CHECKS:
            if not Path(path).exists():
                print(f"[FAIL] {name}: missing {path}")
                failed = True
                continue
            n, status, info = _run_script(name, path, env)
            if status == "PASS":
                print(f"[PASS] {n}: {info}")
            elif status == "SKIP":
                print(f"[SKIP] {n}: {info}")
            else:
                print(f"[FAIL] {n}: {info}")
                failed = True
        if failed:
            print("p1_gate: FAIL")
            return 1
        print("p1_gate: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
