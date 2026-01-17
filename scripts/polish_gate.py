#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    cmd: list[str]


def _run(step: Step) -> tuple[bool, str]:
    p = subprocess.run(step.cmd, check=False, capture_output=True, text=True)
    out = []
    out.append(f"$ {' '.join(step.cmd)}")
    if p.stdout:
        out.append(p.stdout.rstrip("\n"))
    if p.stderr:
        out.append(p.stderr.rstrip("\n"))
    ok = p.returncode == 0
    if not ok:
        out.append(f"[exit={p.returncode}]")
    return ok, "\n".join(out) + "\n"


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    log_path = logs_dir / f"polish_gate_{ts}.log"
    report_path = logs_dir / f"polish_gate_{ts}.json"

    steps = [
        Step(
            "verify_shutdown_clean",
            [sys.executable, str(repo_root / "scripts" / "verify_shutdown_clean.py")],
        ),
        Step(
            "verify_dependency_resolve",
            [sys.executable, str(repo_root / "scripts" / "verify_dependency_resolve.py")],
        ),
        Step("smoke_import_all", [sys.executable, str(repo_root / "scripts" / "smoke_import_all.py")]),
        Step("verify_env", [sys.executable, str(repo_root / "scripts" / "verify_env.py")]),
        Step("verify_modes_contract", [sys.executable, str(repo_root / "scripts" / "verify_modes_contract.py")]),
    ]

    results = []
    ok_all = True
    buf = []
    buf.append(f"polish_gate started: {ts} (UTC)")
    for s in steps:
        ok, output = _run(s)
        results.append({"step": s.name, "ok": ok})
        ok_all = ok_all and ok
        buf.append(f"\n== {s.name} ==\n{output}")
        if not ok:
            # fail-fast: still write report/logs
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

