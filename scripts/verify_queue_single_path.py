#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _has_scheduler_submit(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "submit":
            if isinstance(fn.value, ast.Name) and fn.value.id == "scheduler":
                return True
    return False


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    routes_dir = root / "src" / "dubbing_pipeline" / "web" / "routes"
    offenders: list[str] = []
    for p in sorted(routes_dir.rglob("*.py")):
        if _has_scheduler_submit(p):
            offenders.append(str(p.relative_to(root)))
    if offenders:
        print("FAIL: scheduler.submit used in web routes:", file=sys.stderr)
        for o in offenders:
            print(f"- {o}", file=sys.stderr)
        return 2

    server_py = root / "src" / "dubbing_pipeline" / "server.py"
    text = server_py.read_text(encoding="utf-8")
    if "app.state.queue_backend" not in text:
        print("FAIL: app.state.queue_backend not set in server.py", file=sys.stderr)
        return 2

    print("OK: queue submission single path verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
