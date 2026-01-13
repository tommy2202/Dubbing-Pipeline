from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    print(f"+ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run full library verification gate suite.")
    ap.add_argument("--include-ui", action="store_true", help="Also run UI library route smoke test.")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)

    scripts = [
        "scripts/verify_db_migration_library.py",
        "scripts/verify_manifest_and_paths.py",
        "scripts/verify_library_endpoints_sorting.py",
        "scripts/verify_job_submit_requires_metadata.py",
        "scripts/verify_queue_limits.py",
    ]
    if args.include_ui:
        scripts.append("scripts/verify_ui_library_routes.py")

    for s in scripts:
        _run([sys.executable, s])

    print("library_full_gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

