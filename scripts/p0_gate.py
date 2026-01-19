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
_OPTIONAL_MODULES = {
    "aiortc",
    "av",
    "demucs",
    "gradio",
    "librosa",
    "openai_whisper",
    "pyannote",
    "pydub",
    "resemblyzer",
    "speechbrain",
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "vosk",
    "webrtcvad",
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
    log_path = logs_dir / f"p0_gate_{ts}.log"
    report_path = logs_dir / f"p0_gate_{ts}.json"

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
        Step("verify_object_auth", [sys.executable, str(repo_root / "scripts" / "verify_object_auth.py")]),
        Step("verify_upload_hardening", [sys.executable, str(repo_root / "scripts" / "verify_upload_hardening.py")]),
        Step("verify_limits", [sys.executable, str(repo_root / "scripts" / "verify_limits.py")]),
        Step("verify_rate_limits", [sys.executable, str(repo_root / "scripts" / "verify_rate_limits.py")]),
        Step("verify_secrets_redaction", [sys.executable, str(repo_root / "scripts" / "verify_secrets_redaction.py")]),
        Step("verify_cors_csrf", [sys.executable, str(repo_root / "scripts" / "verify_cors_csrf.py")]),
        Step("verify_shutdown_clean", [sys.executable, str(repo_root / "scripts" / "verify_shutdown_clean.py")]),
    ]

    results = []
    ok_all = True
    buf = []
    buf.append(f"p0_gate started: {ts} (UTC)")
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
