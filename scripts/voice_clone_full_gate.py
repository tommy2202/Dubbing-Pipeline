from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    print(f"+ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)

    scripts = [
        "scripts/verify_diarization_input_routing.py",
        "scripts/verify_voice_ref_extraction.py",
        "scripts/verify_two_pass_orchestration.py",
        "scripts/verify_voice_store_layout.py",
        "scripts/verify_character_ref_resolution.py",
        "scripts/verify_character_mapping_api.py",
    ]
    for s in scripts:
        _run([sys.executable, s])

    print("voice_clone_full_gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
