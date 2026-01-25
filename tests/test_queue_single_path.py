from __future__ import annotations

import ast
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


def test_no_scheduler_submit_in_web_routes() -> None:
    root = Path(__file__).resolve().parents[1]
    routes_dir = root / "src" / "dubbing_pipeline" / "web" / "routes"
    offenders = []
    for p in sorted(routes_dir.rglob("*.py")):
        if _has_scheduler_submit(p):
            offenders.append(str(p.relative_to(root)))
    assert offenders == []


def test_server_sets_queue_backend() -> None:
    root = Path(__file__).resolve().parents[1]
    server_py = root / "src" / "dubbing_pipeline" / "server.py"
    text = server_py.read_text(encoding="utf-8")
    assert "app.state.queue_backend" in text
