from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from dubbing_pipeline.api.deps import Identity, current_identity
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.system.readiness import collect_readiness

router = APIRouter(prefix="/api/system", tags=["system"])


def _require_readiness_access(
    request: Request, ident: Identity = Depends(current_identity)
) -> Identity:
    s = get_settings()
    allow_operator = bool(getattr(s, "readiness_operator_access", False))
    if ident.user.role == Role.admin:
        return ident
    if allow_operator and ident.user.role == Role.operator:
        return ident
    raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/readiness")
async def system_readiness(
    _: Identity = Depends(_require_readiness_access),
):
    return collect_readiness()
