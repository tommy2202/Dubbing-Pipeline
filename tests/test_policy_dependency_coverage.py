from __future__ import annotations

from fastapi.routing import APIRoute

from dubbing_pipeline.security import policy
from dubbing_pipeline.server import app


def _iter_deps(dep) -> list:
    out = []
    for d in dep.dependencies:
        out.append(d)
        out.extend(_iter_deps(d))
    return out


def _has_dependency(route: APIRoute, target) -> bool:
    dep = getattr(route, "dependant", None)
    if dep is None:
        return False
    for d in _iter_deps(dep):
        if getattr(d, "call", None) is target:
            return True
    return False


def test_policy_dependencies_attached() -> None:
    protected_paths = {
        "/api/library/series",
        "/api/library/search",
        "/api/jobs",
        "/api/jobs/{id}",
        "/api/jobs/{id}/files",
        "/api/jobs/{id}/logs",
        "/api/jobs/events",
        "/api/uploads/init",
        "/api/admin/metrics",
        "/api/system/security-posture",
        "/api/runtime/state",
        "/files/{path:path}",
        "/video/{job}",
    }

    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path not in protected_paths:
            continue
        if not _has_dependency(route, policy.require_invite_member):
            missing.append((route.path, "require_invite_member"))
        if not _has_dependency(route, policy.require_request_allowed):
            missing.append((route.path, "require_request_allowed"))

    assert not missing, f"missing policy deps: {missing}"
