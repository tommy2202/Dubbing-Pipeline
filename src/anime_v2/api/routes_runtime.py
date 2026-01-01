from __future__ import annotations

from fastapi import APIRouter, Depends

from anime_v2.api.deps import Identity, require_role
from anime_v2.api.models import Role
from anime_v2.runtime.scheduler import Scheduler


router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/state")
async def runtime_state(_: Identity = Depends(require_role(Role.operator))):
    s = Scheduler.instance_optional()
    if s is None:
        return {"ok": False, "detail": "scheduler not installed"}
    return {"ok": True, "state": s.state()}

