#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path


def _run(path: Path) -> int:
    spec = importlib.util.spec_from_file_location("e2e_two_users_target", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load target script")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "main"):
        return int(mod.main())
    raise RuntimeError("target script missing main()")


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "e2e_concurrency_two_users.py"
    raise SystemExit(_run(target))
