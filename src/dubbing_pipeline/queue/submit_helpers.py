from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.utils.log import logger


async def submit_job_or_503(
    request: Request,
    *,
    job_id: str,
    user_id: str,
    mode: str,
    device: str,
    priority: int = 100,
    meta: dict[str, Any] | None = None,
) -> None:
    qb = getattr(request.app.state, "queue_backend", None)
    if qb is None:
        logger.error(
            "queue_backend_missing",
            job_id=str(job_id),
            user_id=str(user_id),
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=503,
            detail="Queue backend not initialized; try again later",
        )
    try:
        await qb.submit_job(
            job_id=str(job_id),
            user_id=str(user_id),
            mode=str(mode),
            device=str(device),
            priority=int(priority),
            meta=meta,
        )
    except RuntimeError as ex:
        if "draining" in str(ex).lower():
            ra = str(lifecycle.retry_after_seconds(60))
            raise HTTPException(
                status_code=503,
                detail="Server is draining; try again later",
                headers={"Retry-After": ra},
            ) from ex
        logger.warning(
            "queue_submit_failed",
            job_id=str(job_id),
            user_id=str(user_id),
            error=str(ex),
        )
        raise HTTPException(
            status_code=503,
            detail="Queue backend unavailable; try again later",
        ) from ex
    except Exception as ex:
        logger.warning(
            "queue_submit_failed",
            job_id=str(job_id),
            user_id=str(user_id),
            error=str(ex),
        )
        raise HTTPException(
            status_code=503,
            detail="Queue backend unavailable; try again later",
        ) from ex
