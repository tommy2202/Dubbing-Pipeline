from __future__ import annotations

import subprocess
import sys


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> int:
    scripts = [
        "scripts/verify_invite_only.py",
        "scripts/verify_visibility.py",
        "scripts/verify_quotas.py",
        "scripts/verify_reports.py",
        "scripts/verify_privacy_logging.py",
        "scripts/verify_access_mode.py",
    ]
    for script in scripts:
        _run([sys.executable, script])
    print("privacy_gate: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
