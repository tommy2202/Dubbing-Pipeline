from __future__ import annotations

from fastapi import APIRouter, Depends

from dubbing_pipeline.security import policy

BASE_POLICY_DEPENDENCIES = [
    Depends(policy.dep_user),
    Depends(policy.dep_invite_only),
    Depends(policy.dep_request_allowed),
]


def secure_router(*, dependencies: list | None = None, **kwargs) -> APIRouter:
    deps = list(BASE_POLICY_DEPENDENCIES)
    if dependencies:
        deps.extend(dependencies)
    return APIRouter(dependencies=deps, **kwargs)
