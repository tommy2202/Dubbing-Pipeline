from __future__ import annotations

from fastapi import HTTPException, Request

from dubbing_pipeline.ops.metrics import job_errors
from dubbing_pipeline.runtime.scheduler import Scheduler


def _store(request: Request):
    st = getattr(request.app.state, "job_store", None)
    if st is None:
        raise HTTPException(status_code=500, detail="Job store not initialized")
    return st


def _auth_store(request: Request):
    st = getattr(request.app.state, "auth_store", None)
    if st is None:
        raise HTTPException(status_code=500, detail="Auth store not initialized")
    return st


def _queue(request: Request):
    q = getattr(request.app.state, "job_queue", None)
    if q is None:
        raise HTTPException(status_code=500, detail="Job queue not initialized")
    return q


def _scheduler(request: Request) -> Scheduler:
    s = getattr(request.app.state, "scheduler", None)
    if s is None:
        s = Scheduler.instance_optional()
    if s is None:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    return s


def _queue_backend(request: Request):
    qb = getattr(request.app.state, "queue_backend", None)
    return qb


def _job_queue(request: Request):
    q = getattr(request.app.state, "job_queue", None)
    return q


def _collect_job_error_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for metric in job_errors.collect():
            for sample in metric.samples:
                if not sample.name.endswith("_total"):
                    continue
                stage = sample.labels.get("stage") if isinstance(sample.labels, dict) else None
                if not stage:
                    continue
                out[str(stage)] = int(sample.value)
    except Exception:
        return {}
    return out
