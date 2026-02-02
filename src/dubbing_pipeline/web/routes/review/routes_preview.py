from __future__ import annotations

from fastapi import HTTPException, Request, Response

from dubbing_pipeline.api.access import require_job_access
from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.web.routes.jobs_common import _file_range_response, _get_store, _job_base_dir

from .helpers import _review_audio_path


async def get_job_review_audio(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity,
) -> Response:
    store = _get_store(request)
    job = require_job_access(store=store, ident=ident, job_id=id)
    base_dir = _job_base_dir(job)
    p = _review_audio_path(base_dir, int(segment_id))
    if p is None:
        raise HTTPException(status_code=404, detail="audio not found")
    return _file_range_response(
        request, p, media_type="audio/wav", allowed_roots=[_job_base_dir(job)]
    )
