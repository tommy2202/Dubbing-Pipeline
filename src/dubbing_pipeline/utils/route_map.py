from __future__ import annotations

from typing import Any

from starlette.routing import Mount, WebSocketRoute


def collect_route_map(app) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for r in app.routes:
        if isinstance(r, Mount):
            items.append({"method": "MOUNT", "path": r.path, "name": r.name or ""})
            continue
        if isinstance(r, WebSocketRoute):
            items.append({"method": "WEBSOCKET", "path": r.path, "name": r.name or ""})
            continue
        methods = getattr(r, "methods", None)
        if not methods:
            items.append({"method": "ANY", "path": r.path, "name": r.name or ""})
            continue
        for m in sorted(methods):
            items.append({"method": str(m), "path": r.path, "name": r.name or ""})
    return sorted(items, key=lambda x: (x["path"], x["method"], x["name"]))
