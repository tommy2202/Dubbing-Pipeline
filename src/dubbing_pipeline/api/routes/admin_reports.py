from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.ops import audit
from dubbing_pipeline.security import policy

from .admin_helpers import _store

router = APIRouter()


@router.get("/reports")
async def admin_list_reports(
    request: Request,
    status: str | None = "open",
    limit: int = 200,
    offset: int = 0,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    items = store.list_library_reports(limit=int(limit), offset=int(offset), status=status)
    return {"ok": True, "items": items}


@router.get("/reports/summary")
async def admin_reports_summary(
    request: Request,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    s = get_settings()
    count_open = store.count_library_reports(status="open")
    admin_topic = str(getattr(s, "ntfy_admin_topic", "") or "").strip()
    ntfy_configured = bool(getattr(s, "ntfy_enabled", False)) and bool(admin_topic)
    return {"ok": True, "open_reports": int(count_open), "ntfy_admin_configured": ntfy_configured}


@router.post("/reports/{id}/resolve")
async def admin_resolve_report(
    request: Request,
    id: str,
    ident: Identity = Depends(policy.require_admin),
) -> dict:
    store = _store(request)
    store.update_report_status(str(id), status="resolved")
    audit.emit(
        "admin.report_resolved",
        user_id=str(ident.user.id),
        meta={"report_id": str(id)},
    )
    return {"ok": True, "report_id": str(id)}
