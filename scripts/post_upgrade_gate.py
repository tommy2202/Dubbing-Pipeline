#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str = ""


def _run_pytest(args: list[str], *, name: str) -> Result:
    cmd = [sys.executable, "-m", "pytest"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return Result(name=name, status="PASS")
    out = (res.stdout or "") + (res.stderr or "")
    detail = out.strip()
    if "No module named pytest" in detail:
        detail = "pytest not available"
    return Result(name=name, status="FAIL", detail=detail)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _ = root  # keep for future path extensions

    results: list[Result] = []
    results.append(_run_pytest(["tests/test_marker_compat.py"], name="marker_compat"))
    results.append(_run_pytest(["tests/test_queue_single_path.py"], name="queue_single_path"))
    results.append(_run_pytest(["tests/test_scheduler_submit_scope.py"], name="scheduler_submit_scope"))

    if shutil.which("ffmpeg") is None:
        results.append(
            Result(
                name="smoke_fresh_machine",
                status="SKIP",
                detail="ffmpeg not available; install ffmpeg to run smoke test",
            )
        )
    else:
        results.append(_run_pytest(["-k", "smoke_fresh_machine"], name="smoke_fresh_machine"))

    print("\nPOST-UPGRADE GATE SUMMARY")
    for r in results:
        line = f"- {r.name}: {r.status}"
        if r.detail:
            line += f" ({r.detail})"
        print(line)

    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
