#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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


def _scan_text_files(root: Path, *, globs: Iterable[str]) -> list[tuple[str, str, int, str]]:
    hits: list[tuple[str, str, int, str]] = []
    import re

    patterns = [
        ("TODO", re.compile(r"\bTODO\b")),
        ("FIXME", re.compile(r"\bFIXME\b")),
        ("WIP", re.compile(r"\bWIP\b")),
        ("placeholder", re.compile(r"\bplaceholder\b", re.IGNORECASE)),
        ("stub", re.compile(r"\bstub\b", re.IGNORECASE)),
        ("not implemented", re.compile(r"\bnot implemented\b", re.IGNORECASE)),
    ]
    for pat in globs:
        for path in root.rglob(pat):
            if not path.is_file():
                continue
            # Skip known noisy/generated directories
            if any(part in {".git", "__pycache__", "Output", "_tmp_qa_job"} for part in path.parts):
                continue
            # Don't self-trigger on the gate script itself.
            if path.name == "polish_gate.py":
                continue
            try:
                txt = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for idx, line in enumerate(txt.splitlines(), 1):
                for key, rx in patterns:
                    if rx.search(line):
                        hits.append((str(path), key, idx, line.strip()[:200]))
    return hits


def _check_canonical_modules(repo_root: Path) -> list[str]:
    """
    Fast sanity checks for duplicate/obsolete modules.
    Returns list of error strings.
    """
    errors: list[str] = []

    must_exist = [
        "src/anime_v2/audio/music_detect.py",
        "src/anime_v2/text/pg_filter.py",
        "src/anime_v2/qa/scoring.py",
        "src/anime_v2/text/style_guide.py",
        "src/anime_v2/diarization/smoothing.py",
        "src/anime_v2/expressive/director.py",
        "src/anime_v2/stages/export.py",
        "src/anime_v2/audio/tracks.py",
    ]
    must_not_exist = [
        "src/anime_v2/stages/diarize.py",  # removed legacy path
    ]

    for rel in must_exist:
        if not (repo_root / rel).exists():
            errors.append(f"missing canonical module: {rel}")
    for rel in must_not_exist:
        if (repo_root / rel).exists():
            errors.append(f"obsolete module still present: {rel}")

    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    log_path = logs_dir / f"polish_gate_{ts}.log"
    report_path = logs_dir / f"polish_gate_{ts}.json"

    steps = [
        Step("smoke_import_all", [sys.executable, str(repo_root / "scripts" / "smoke_import_all.py")]),
        Step("verify_env", [sys.executable, str(repo_root / "scripts" / "verify_env.py")]),
        Step("verify_audio_pipeline", [sys.executable, str(repo_root / "scripts" / "verify_audio_pipeline.py")]),
        Step("verify_timing_fit", [sys.executable, str(repo_root / "scripts" / "verify_timing_fit.py")]),
        Step("verify_pg_filter", [sys.executable, str(repo_root / "scripts" / "verify_pg_filter.py")]),
        Step("verify_style_guide", [sys.executable, str(repo_root / "scripts" / "verify_style_guide.py")]),
        Step("verify_qa", [sys.executable, str(repo_root / "scripts" / "verify_qa.py")]),
        Step("verify_multitrack_mux", [sys.executable, str(repo_root / "scripts" / "verify_multitrack_mux.py")]),
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

    # Obsolete/wireframe scan (fail only if we see known bad markers)
    scan_hits = _scan_text_files(repo_root, globs=["*.py"])
    canonical_errors = _check_canonical_modules(repo_root)
    if scan_hits:
        ok_all = False
    if canonical_errors:
        ok_all = False

    buf.append("\n== scans ==")
    buf.append(f"bad_marker_hits: {len(scan_hits)}")
    for path, kind, ln, preview in scan_hits[:50]:
        buf.append(f"- {kind} {path}:{ln} {preview}")
    if len(scan_hits) > 50:
        buf.append(f"... truncated ({len(scan_hits)} total)")
    buf.append(f"canonical_errors: {len(canonical_errors)}")
    for e in canonical_errors:
        buf.append(f"- {e}")

    log_path.write_text("\n".join(buf) + "\n", encoding="utf-8")
    report = {
        "timestamp_utc": ts,
        "ok": bool(ok_all),
        "steps": results,
        "scan_bad_marker_hits": len(scan_hits),
        "scan_canonical_errors": canonical_errors,
        "log_path": str(log_path),
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print("PASS" if ok_all else "FAIL")
    print(f"Details: {log_path}")
    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())

