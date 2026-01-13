#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterable
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
        # config + modes
        "config/public_config.py",
        "config/settings.py",
        "src/dubbing_pipeline/modes.py",
        # profiles + text transforms
        "src/dubbing_pipeline/projects/loader.py",
        "src/dubbing_pipeline/text/style_guide.py",
        "src/dubbing_pipeline/text/pg_filter.py",
        # timing + rewrite hook
        "src/dubbing_pipeline/timing/fit_text.py",
        "src/dubbing_pipeline/timing/rewrite_provider.py",
        # audio/music + overrides
        "src/dubbing_pipeline/audio/music_detect.py",
        "src/dubbing_pipeline/review/overrides.py",
        # QA
        "src/dubbing_pipeline/qa/scoring.py",
        # voice memory tools
        "src/dubbing_pipeline/voice_memory/store.py",
        "src/dubbing_pipeline/voice_memory/tools.py",
        "src/dubbing_pipeline/voice_memory/audition.py",
        # streaming
        "src/dubbing_pipeline/streaming/runner.py",
        "src/dubbing_pipeline/streaming/context.py",
        # lipsync plugin
        "src/dubbing_pipeline/plugins/lipsync/base.py",
        "src/dubbing_pipeline/plugins/lipsync/registry.py",
        "src/dubbing_pipeline/plugins/lipsync/wav2lip_plugin.py",
        "src/dubbing_pipeline/plugins/lipsync/preview.py",
        # subs formatting
        "src/dubbing_pipeline/subs/formatting.py",
        # retention/cache policy
        "src/dubbing_pipeline/storage/retention.py",
        # drift reports
        "src/dubbing_pipeline/reports/drift.py",
        # existing required modules
        "src/dubbing_pipeline/diarization/smoothing.py",
        "src/dubbing_pipeline/expressive/director.py",
        "src/dubbing_pipeline/stages/export.py",
        "src/dubbing_pipeline/audio/tracks.py",
    ]
    must_not_exist = [
        "src/dubbing_pipeline/stages/diarize.py",  # removed legacy path
    ]

    for rel in must_exist:
        if not (repo_root / rel).exists():
            errors.append(f"missing canonical module: {rel}")
    for rel in must_not_exist:
        if (repo_root / rel).exists():
            errors.append(f"obsolete module still present: {rel}")

    # Duplicate implementation guardrails (tight allowlist).
    # We permit two retention modules:
    # - src/dubbing_pipeline/storage/retention.py (per-job cache policy retention)
    # - src/dubbing_pipeline/ops/retention.py (global input/log retention)
    allowed_retention = {
        (repo_root / "src/dubbing_pipeline/storage/retention.py").resolve(),
        (repo_root / "src/dubbing_pipeline/ops/retention.py").resolve(),
    }
    found_retention = {p.resolve() for p in repo_root.glob("src/dubbing_pipeline/**/retention.py")}
    extra = sorted(str(p) for p in (found_retention - allowed_retention))
    missing = sorted(str(p) for p in (allowed_retention - found_retention))
    if missing:
        errors.append(f"missing expected retention modules: {missing}")
    if extra:
        errors.append(f"unexpected retention modules found: {extra}")

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
        # Next-version contract tests + profiles
        Step("verify_modes_contract", [sys.executable, str(repo_root / "scripts" / "verify_modes_contract.py")]),
        Step("verify_project_profiles", [sys.executable, str(repo_root / "scripts" / "verify_project_profiles.py")]),
        # Core synthetic checks
        Step("verify_audio_pipeline", [sys.executable, str(repo_root / "scripts" / "verify_audio_pipeline.py")]),
        Step("verify_music_detect", [sys.executable, str(repo_root / "scripts" / "verify_music_detect.py")]),
        Step("verify_overrides", [sys.executable, str(repo_root / "scripts" / "verify_overrides.py")]),
        Step("verify_timing_fit", [sys.executable, str(repo_root / "scripts" / "verify_timing_fit.py")]),
        Step("verify_rewrite_provider", [sys.executable, str(repo_root / "scripts" / "verify_rewrite_provider.py")]),
        Step("verify_pg_filter", [sys.executable, str(repo_root / "scripts" / "verify_pg_filter.py")]),
        Step("verify_style_guide", [sys.executable, str(repo_root / "scripts" / "verify_style_guide.py")]),
        Step("verify_sub_formatting", [sys.executable, str(repo_root / "scripts" / "verify_sub_formatting.py")]),
        Step("verify_voice_tools", [sys.executable, str(repo_root / "scripts" / "verify_voice_tools.py")]),
        Step("verify_stream_context", [sys.executable, str(repo_root / "scripts" / "verify_stream_context.py")]),
        Step("verify_drift_reports", [sys.executable, str(repo_root / "scripts" / "verify_drift_reports.py")]),
        Step("verify_retention", [sys.executable, str(repo_root / "scripts" / "verify_retention.py")]),
        Step("verify_qa", [sys.executable, str(repo_root / "scripts" / "verify_qa.py")]),
        Step("verify_qa_rewrite_heavy", [sys.executable, str(repo_root / "scripts" / "verify_qa_rewrite_heavy.py")]),
        Step("verify_multitrack_mux", [sys.executable, str(repo_root / "scripts" / "verify_multitrack_mux.py")]),
        Step("verify_lipsync_preview", [sys.executable, str(repo_root / "scripts" / "verify_lipsync_preview.py")]),
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

