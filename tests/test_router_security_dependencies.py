from __future__ import annotations

from fastapi.routing import APIRoute

from dubbing_pipeline.security import policy
from dubbing_pipeline.server import app


SENSITIVE_PREFIXES = (
    "/api/library",
    "/api/jobs",
    "/api/uploads",
    "/api/admin",
    "/files",
    "/video",
)


def _iter_deps(dep) -> list:
    out = []
    for d in dep.dependencies:
        out.append(d)
        out.extend(_iter_deps(d))
    return out


def _dep_calls(route: APIRoute) -> list:
    dep = getattr(route, "dependant", None)
    if dep is None:
        return []
    calls = []
    for d in _iter_deps(dep):
        call = getattr(d, "call", None)
        if call is not None:
            calls.append(call)
    return calls


def _has_dependency(route: APIRoute, target) -> bool:
    return any(call is target for call in _dep_calls(route))


def _has_auth_dependency(route: APIRoute) -> bool:
    for call in _dep_calls(route):
        if call in {policy.dep_user, policy.require_invite_member, policy.require_authenticated_user}:
            return True
        mod = getattr(call, "__module__", "")
        qual = getattr(call, "__qualname__", "")
        if mod == "dubbing_pipeline.api.deps" and qual == "current_identity":
            return True
        if mod == "dubbing_pipeline.api.deps" and qual.endswith("require_scope.<locals>.dep"):
            return True
        if mod == "dubbing_pipeline.api.deps" and qual.endswith("require_role.<locals>.dep"):
            return True
    return False


def test_sensitive_routes_require_security_dependencies() -> None:
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = str(route.path or "")
        if not path.startswith(SENSITIVE_PREFIXES):
            continue
        if not _has_auth_dependency(route):
            missing.append((path, "auth"))
        if not (
            _has_dependency(route, policy.dep_invite_only)
            or _has_dependency(route, policy.require_invite_member)
        ):
            missing.append((path, "invite"))
        if not (
            _has_dependency(route, policy.dep_request_allowed)
            or _has_dependency(route, policy.require_request_allowed)
        ):
            missing.append((path, "remote_access"))

    assert not missing, f"missing security deps: {missing}"
