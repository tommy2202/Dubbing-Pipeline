#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_MISSING_RE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_TOOL_RE = re.compile(r"Missing required tool: ([A-Za-z0-9_\-]+)")
_OPTIONAL_MODULES = {
    "fastapi",
    "pydantic",
    "httpx",
    "redis",
    "torch",
    "transformers",
    "pyannote",
    "speechbrain",
    "demucs",
}


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    cmd: list[str]


def _missing_optional(output: str) -> bool:
    hits = _MISSING_RE.findall(output or "")
    for mod in hits:
        base = (mod or "").split(".", 1)[0]
        if base in _OPTIONAL_MODULES:
            return True
    tool = _TOOL_RE.search(output or "")
    if tool:
        return True
    return False


def _run(step: Step, *, env: dict[str, str]) -> tuple[str, str]:
    p = subprocess.run(step.cmd, check=False, capture_output=True, text=True, env=env)
    out = []
    out.append(f"$ {' '.join(step.cmd)}")
    if p.stdout:
        out.append(p.stdout.rstrip("\n"))
    if p.stderr:
        out.append(p.stderr.rstrip("\n"))
    output = "\n".join(out) + "\n"
    if p.returncode == 0:
        return "ok", output
    if _missing_optional(output):
        return "skip", output
    return "fail", output + f"[exit={p.returncode}]\n"


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    log_path = logs_dir / f"p1_gate_{ts}.log"
    report_path = logs_dir / f"p1_gate_{ts}.json"

    env = dict(os.environ)
    py_path = os.pathsep.join(
        [
            str(repo_root / "src"),
            str(repo_root),
            env.get("PYTHONPATH", ""),
        ]
    ).strip(os.pathsep)
    env["PYTHONPATH"] = py_path

    steps = [
        Step(
            "verify_job_timeline",
            [sys.executable, str(repo_root / "scripts" / "verify_job_timeline.py")],
        ),
        Step(
            "verify_notifications_ntfy",
            [sys.executable, str(repo_root / "scripts" / "verify_notifications_ntfy.py")],
        ),
        Step(
            "verify_readiness_endpoint",
            [sys.executable, str(repo_root / "scripts" / "verify_readiness_endpoint.py")],
        ),
        Step(
            "e2e_two_users_submit",
            [sys.executable, str(repo_root / "scripts" / "e2e_two_users_submit.py")],
        ),
        Step(
            "e2e_upload_resume",
            [sys.executable, str(repo_root / "scripts" / "e2e_upload_resume.py")],
        ),
        Step(
            "e2e_cancel_midrun",
            [sys.executable, str(repo_root / "scripts" / "e2e_cancel_midrun.py")],
        ),
        Step(
            "e2e_restart_worker_midrun",
            [sys.executable, str(repo_root / "scripts" / "e2e_restart_worker_midrun.py")],
        ),
        Step(
            "e2e_redis_fallback",
            [sys.executable, str(repo_root / "scripts" / "e2e_redis_fallback.py")],
        ),
    ]

    results = []
    ok_all = True
    buf = []
    buf.append(f"p1_gate started: {ts} (UTC)")
    for s in steps:
        status, output = _run(s, env=env)
        results.append({"step": s.name, "status": status})
        ok_all = ok_all and status in {"ok", "skip"}
        buf.append(f"\n== {s.name} ({status}) ==\n{output}")
        if status == "fail":
            break

    log_path.write_text("\n".join(buf) + "\n", encoding="utf-8")
    report = {
        "timestamp_utc": ts,
        "ok": bool(ok_all),
        "steps": results,
        "log_path": str(log_path),
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print("PASS" if ok_all else "FAIL")
    print(f"Details: {log_path}")
    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())
