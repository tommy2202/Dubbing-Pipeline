from __future__ import annotations

import ast
from pathlib import Path


def _is_scheduler_submit(call: ast.Call) -> bool:
    fn = call.func
    if not isinstance(fn, ast.Attribute) or fn.attr != "submit":
        return False
    base = fn.value
    if isinstance(base, ast.Name) and base.id in {"sched", "scheduler"}:
        return True
    if isinstance(base, ast.Attribute) and base.attr in {"sched", "scheduler", "_scheduler"}:
        return True
    if isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute):
        if base.func.attr in {"instance", "instance_optional"}:
            if isinstance(base.func.value, ast.Name) and base.func.value.id == "Scheduler":
                return True
    return False


def test_scheduler_submit_only_in_fallback_backend() -> None:
    root = Path(__file__).resolve().parents[1]
    src_root = root / "src" / "dubbing_pipeline"
    allowed = {str((src_root / "queue" / "fallback_local_queue.py").resolve())}
    offenders: list[str] = []
    for p in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_scheduler_submit(node):
                if str(p.resolve()) not in allowed:
                    offenders.append(str(p.relative_to(root)))
                    break
    assert offenders == []
