from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from dubbing_pipeline.api.models import Role, User
from dubbing_pipeline.jobs.models import Job, Visibility
from dubbing_pipeline.utils.log import logger


def _is_admin(user: User | None) -> bool:
    try:
        return bool(user and user.role == Role.admin)
    except Exception:
        return False


def _owner_id_of(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, dict):
        return str(obj.get("owner_user_id") or obj.get("owner_id") or "").strip()
    if hasattr(obj, "get"):
        try:
            return str(obj.get("owner_user_id") or obj.get("owner_id") or "").strip()
        except Exception:
            pass
    if hasattr(obj, "__getitem__"):
        try:
            return str(obj["owner_user_id"] or obj["owner_id"] or "").strip()
        except Exception:
            pass
    return str(getattr(obj, "owner_user_id", None) or getattr(obj, "owner_id", "") or "").strip()


def _visibility_of(obj: Any) -> str:
    if obj is None:
        return "private"
    if isinstance(obj, dict):
        return str(obj.get("visibility") or "private").strip().lower()
    if hasattr(obj, "get"):
        try:
            return str(obj.get("visibility") or "private").strip().lower()
        except Exception:
            pass
    if hasattr(obj, "__getitem__"):
        try:
            return str(obj["visibility"] or "private").strip().lower()
        except Exception:
            pass
    vis = getattr(obj, "visibility", "private")
    if isinstance(vis, Visibility):
        return str(vis.value).strip().lower()
    return str(vis or "private").strip().lower()


def _log_forbidden(*, resource: str, user_id: str, owner_id: str, meta: dict[str, Any]) -> None:
    try:
        logger.warning(
            "ownership_forbidden",
            resource=str(resource),
            user_id=str(user_id),
            owner_id=str(owner_id),
            **meta,
        )
    except Exception:
        return


def _require_owner_or_admin(
    *,
    user: User,
    owner_id: str,
    allow_public: bool,
    visibility: str,
    resource: str,
    meta: dict[str, Any],
) -> None:
    uid = str(user.id)
    owner_id = str(owner_id or "")
    if _is_admin(user):
        return
    if owner_id and owner_id == uid:
        return
    if allow_public and visibility == "public":
        return
    _log_forbidden(resource=resource, user_id=uid, owner_id=owner_id, meta=meta)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_job_owner_or_admin(
    user: User, job: Job | None, *, allow_public: bool = False
) -> None:
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    _require_owner_or_admin(
        user=user,
        owner_id=_owner_id_of(job),
        allow_public=allow_public,
        visibility=_visibility_of(job),
        resource="job",
        meta={"job_id": str(getattr(job, "id", "") or "")},
    )


def require_library_owner_or_admin(
    user: User, item: Any, *, allow_public: bool = True
) -> None:
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    series_slug = ""
    if isinstance(item, dict):
        series_slug = str(item.get("series_slug") or "")
    else:
        series_slug = str(getattr(item, "series_slug", "") or "")
    _require_owner_or_admin(
        user=user,
        owner_id=_owner_id_of(item),
        allow_public=allow_public,
        visibility=_visibility_of(item),
        resource="library",
        meta={"series_slug": series_slug},
    )


def require_file_owner_or_admin(
    user: User, file_meta: dict[str, Any] | None, *, allow_public: bool = False
) -> None:
    if not file_meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    _require_owner_or_admin(
        user=user,
        owner_id=_owner_id_of(file_meta),
        allow_public=allow_public,
        visibility=_visibility_of(file_meta),
        resource="file",
        meta={
            "job_id": str(file_meta.get("job_id") or ""),
            "path": str(file_meta.get("path") or file_meta.get("rel_path") or ""),
        },
    )
