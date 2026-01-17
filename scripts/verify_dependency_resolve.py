from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(cmd: list[str], *, cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed: {' '.join(cmd)}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    constraints = repo_root / "docker" / "constraints.txt"
    if not constraints.exists():
        print(f"Missing constraints file: {constraints}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        venv_dir = Path(td) / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=repo_root)
        py = _venv_python(venv_dir)

        try:
            _run([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=repo_root)
            _run(
                [
                    str(py),
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    ".[web]",
                    "-c",
                    str(constraints),
                ],
                cwd=repo_root,
            )
            _run([str(py), "scripts/smoke_import_all.py"], cwd=repo_root)
        except Exception as ex:
            print("verify_dependency_resolve: FAIL", file=sys.stderr)
            print(f"- error: {ex}", file=sys.stderr)
            print("- hint: check pyproject bounds and docker/constraints.txt alignment", file=sys.stderr)
            return 2

    print("verify_dependency_resolve: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
