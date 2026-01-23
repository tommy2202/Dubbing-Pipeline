from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.jobs.models import Job
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.library.paths import get_job_output_root

_JOB_SEGMENT_RE = re.compile(r"^job-(.+)$")


def _is_admin(ident: Identity) -> bool:
    role = getattr(ident.user.role, "value", ident.user.role)
    return str(role) == str(Role.admin.value)


def require_job_access(
    *,
    store: JobStore,
    ident: Identity,
    job_id: str | None = None,
    job: Job | None = None,
) -> Job:
    if job is None:
        if not job_id:
            raise HTTPException(status_code=404, detail="Job not found")
        job = store.get(str(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    owner_id = str(getattr(job, "owner_id", "") or "")
    if _is_admin(ident) or owner_id == str(ident.user.id):
        return job
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_upload_access(
    *,
    store: JobStore,
    ident: Identity,
    upload_id: str | None = None,
    upload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if upload is None:
        if not upload_id:
            raise HTTPException(status_code=404, detail="Upload not found")
        upload = store.get_upload(str(upload_id))
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    owner_id = str(upload.get("owner_id") or "")
    if _is_admin(ident) or owner_id == str(ident.user.id):
        return dict(upload)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_library_access(
    *,
    store: JobStore,
    ident: Identity,
    series_slug: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    job_id: str | None = None,
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if item is None:
        con = store._conn()
        try:
            if job_id:
                row = con.execute(
                    "SELECT * FROM job_library WHERE job_id = ? LIMIT 1;",
                    (str(job_id),),
                ).fetchone()
            else:
                slug = str(series_slug or "").strip()
                if not slug:
                    raise HTTPException(status_code=404, detail="Library item not found")
                if season_number is not None and episode_number is not None:
                    row = con.execute(
                        """
                        SELECT * FROM job_library
                        WHERE series_slug = ? AND season_number = ? AND episode_number = ?
                        LIMIT 1;
                        """,
                        (slug, int(season_number), int(episode_number)),
                    ).fetchone()
                elif season_number is not None:
                    row = con.execute(
                        """
                        SELECT * FROM job_library
                        WHERE series_slug = ? AND season_number = ?
                        LIMIT 1;
                        """,
                        (slug, int(season_number)),
                    ).fetchone()
                else:
                    row = con.execute(
                        "SELECT * FROM job_library WHERE series_slug = ? LIMIT 1;",
                        (slug,),
                    ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Library item not found")
            item = {k: row[k] for k in row.keys()}
        finally:
            con.close()

    owner_id = str(item.get("owner_user_id") or "")
    if _is_admin(ident) or owner_id == str(ident.user.id):
        return dict(item)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _job_for_path(*, store: JobStore, path: Path) -> Job | None:
    p = Path(path).resolve()
    for part in p.parts:
        m = _JOB_SEGMENT_RE.match(part)
        if m:
            jid = m.group(1)
            job = store.get(str(jid))
            if job is not None:
                return job
    jobs = store.list(limit=100000)
    for job in jobs:
        with suppress(Exception):
            root = get_job_output_root(job).resolve()
            p.relative_to(root)
            return job
    return None


def require_file_access(
    *,
    store: JobStore,
    ident: Identity,
    path: Path,
) -> Job:
    job = _job_for_path(store=store, path=path)
    if job is None:
        raise HTTPException(status_code=404, detail="File not found")
    return require_job_access(store=store, ident=ident, job=job)
