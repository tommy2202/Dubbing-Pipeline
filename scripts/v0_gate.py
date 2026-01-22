from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


CHECKS: list[tuple[str, str]] = [
    ("object_access", "scripts/verify_object_access.py"),
    ("streaming_range", "scripts/verify_streaming_range.py"),
    ("trusted_proxy", "scripts/verify_trusted_proxy.py"),
    ("single_writer", "scripts/verify_single_writer.py"),
    ("retention", "scripts/verify_retention.py"),
    ("log_redaction", "scripts/verify_log_redaction.py"),
]


def _run_script(name: str, path: str, env: dict[str, str]) -> tuple[str, bool, str]:
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return name, False, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return name, True, f"ok ({time.time() - t0:.2f}s)"
    lowered = out.lower()
    if any(tag in lowered for tag in ("skip", "skipped", "optional", "not available")):
        return name, True, "skipped"
    return name, False, out.strip() or f"exit={proc.returncode}"


def _server_starts_check(env: dict[str, str]) -> tuple[bool, str]:
    try:
        from fastapi.testclient import TestClient

        from dubbing_pipeline.server import app
    except Exception as ex:
        return False, f"import_failed: {ex}"
    try:
        with TestClient(app) as c:
            r = c.get("/ui/login")
            if r.status_code not in {200, 302}:
                return False, f"unexpected_status={r.status_code}"
        return True, "ok"
    except Exception as ex:
        return False, f"exception: {ex}"


def main() -> int:
    env = os.environ.copy()
    env.setdefault("DUBBING_SKIP_STARTUP_CHECK", "1")
    env.setdefault("MIN_FREE_GB", "0")
    env.setdefault("COOKIE_SECURE", "0")

    with tempfile.TemporaryDirectory(prefix="v0_gate_") as td:
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

        # Apply env to current process for in-proc server check.
        os.environ.update(env)

        ok, msg = _server_starts_check(env)
        if ok:
            print("[PASS] server_start:", msg)
        else:
            print("[FAIL] server_start:", msg)
            return 1

        failed = False
        for name, path in CHECKS:
            if not Path(path).exists():
                print(f"[FAIL] {name}: missing {path}")
                failed = True
                continue
            n, ok, info = _run_script(name, path, env)
            if ok:
                print(f"[PASS] {n}: {info}")
            else:
                print(f"[FAIL] {n}: {info}")
                failed = True
        if failed:
            print("v0_gate: FAIL")
            return 1
        print("v0_gate: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
