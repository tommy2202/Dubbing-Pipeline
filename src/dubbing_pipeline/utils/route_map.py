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


def _endpoint_details(endpoint: object | None) -> tuple[str, str]:
    if endpoint is None:
        return "", ""
    module = getattr(endpoint, "__module__", "") or getattr(
        getattr(endpoint, "__class__", type(endpoint)), "__module__", ""
    )
    name = getattr(endpoint, "__name__", "") or getattr(
        getattr(endpoint, "__class__", type(endpoint)), "__name__", ""
    )
    return str(module), str(name)


def collect_route_details(app) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for r in app.routes:
        if isinstance(r, Mount):
            module, name = _endpoint_details(getattr(r, "app", None))
            items.append(
                {"method": "MOUNT", "path": str(r.path), "module": module, "endpoint": name}
            )
            continue
        if isinstance(r, WebSocketRoute):
            module, name = _endpoint_details(getattr(r, "endpoint", None))
            items.append(
                {"method": "WEBSOCKET", "path": str(r.path), "module": module, "endpoint": name}
            )
            continue
        methods = getattr(r, "methods", None)
        module, name = _endpoint_details(getattr(r, "endpoint", None))
        if not methods:
            items.append({"method": "ANY", "path": str(r.path), "module": module, "endpoint": name})
            continue
        for m in sorted(methods):
            items.append({"method": str(m), "path": str(r.path), "module": module, "endpoint": name})
    return sorted(items, key=lambda x: (x["path"], x["method"], x["module"], x["endpoint"]))


def format_route_details(items: list[dict[str, str]]) -> str:
    lines = [
        f"{item['method']}\t{item['path']}\t{item['module']}\t{item['endpoint']}"
        for item in items
    ]
    return "\n".join(lines) + ("\n" if lines else "")
