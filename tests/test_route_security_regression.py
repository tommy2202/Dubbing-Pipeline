from __future__ import annotations

from fastapi.routing import APIRoute

from dubbing_pipeline.api.public_endpoints import PUBLIC_API_ALLOWLIST
from dubbing_pipeline.server import app


def _iter_deps(dep) -> list:
    out = []
    for d in dep.dependencies:
        out.append(d)
        out.extend(_iter_deps(d))
    return out


def _protected_by_scope_or_role(route: APIRoute) -> bool:
    dep = getattr(route, "dependant", None)
    if dep is None:
        return False
    for d in _iter_deps(dep):
        call = getattr(d, "call", None)
        if call is None:
            continue
        mod = getattr(call, "__module__", "")
        qual = getattr(call, "__qualname__", "")
        if mod == "dubbing_pipeline.api.deps" and qual == "current_identity":
            return True
        if mod == "dubbing_pipeline.api.deps" and qual.endswith("require_scope.<locals>.dep"):
            return True
        if mod == "dubbing_pipeline.api.deps" and qual.endswith("require_role.<locals>.dep"):
            return True
    return False


def test_api_routes_protected_or_allowlisted() -> None:
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = str(route.path or "")
        if not path.startswith("/api/"):
            continue
        if path in PUBLIC_API_ALLOWLIST:
            continue
        if _protected_by_scope_or_role(route):
            continue
        missing.append(path)
    assert not missing, f"unprotected /api routes: {sorted(set(missing))}"
