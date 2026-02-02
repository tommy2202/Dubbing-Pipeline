from __future__ import annotations

from typing import Any

from fastapi import Depends, Request, Response

from dubbing_pipeline.api.deps import Identity, require_scope
from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.web.routes.review import (
    routes_preview,
    routes_rerun,
    routes_segments,
    routes_transcript,
)

router = secure_router()


@router.get("/api/jobs/{id}/overrides")
async def get_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    return await routes_transcript.get_job_overrides(request, id, ident)


@router.get("/api/jobs/{id}/overrides/music/effective")
async def get_job_music_regions_effective(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    return await routes_transcript.get_job_music_regions_effective(request, id, ident)


@router.put("/api/jobs/{id}/overrides")
async def put_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_transcript.put_job_overrides(request, id, ident)


@router.post("/api/jobs/{id}/overrides/apply")
async def apply_job_overrides(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_transcript.apply_job_overrides(request, id, ident)


@router.get("/api/jobs/{id}/characters")
async def get_job_characters(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    return await routes_transcript.get_job_characters(request, id, ident)


@router.put("/api/jobs/{id}/characters")
async def put_job_characters(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_transcript.put_job_characters(request, id, ident)


@router.get("/api/jobs/{id}/segments")
async def get_job_segments(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    return await routes_segments.get_job_segments(request, id, ident)


@router.patch("/api/jobs/{id}/segments/{segment_id}")
async def patch_job_segment(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.patch_job_segment(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/segments/{segment_id}/approve")
async def post_job_segment_approve(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_segment_approve(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/segments/{segment_id}/reject")
async def post_job_segment_reject(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_segment_reject(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/segments/rerun")
async def post_job_segments_rerun(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_rerun.post_job_segments_rerun(request, id, ident)


@router.get("/api/jobs/{id}/transcript")
async def get_job_transcript(
    request: Request,
    id: str,
    page: int = 1,
    per_page: int = 50,
    ident: Identity = Depends(require_scope("read:job")),
) -> dict[str, Any]:
    return await routes_transcript.get_job_transcript(request, id, page, per_page, ident)


@router.put("/api/jobs/{id}/transcript")
async def put_job_transcript(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_transcript.put_job_transcript(request, id, ident)


@router.post("/api/jobs/{id}/overrides/speaker")
async def set_speaker_overrides_from_ui(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_transcript.set_speaker_overrides_from_ui(request, id, ident)


@router.post("/api/jobs/{id}/transcript/synthesize")
async def synthesize_from_approved(
    request: Request, id: str, ident: Identity = Depends(require_scope("edit:job"))
) -> dict[str, Any]:
    return await routes_rerun.synthesize_from_approved(request, id, ident)


@router.get("/api/jobs/{id}/review/segments")
async def get_job_review_segments(
    request: Request, id: str, ident: Identity = Depends(require_scope("read:job"))
) -> dict[str, Any]:
    return await routes_segments.get_job_review_segments(request, id, ident)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/helper")
async def post_job_review_helper(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_review_helper(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/edit")
async def post_job_review_edit(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_review_edit(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/regen")
async def post_job_review_regen(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_rerun.post_job_review_regen(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/lock")
async def post_job_review_lock(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_review_lock(request, id, segment_id, ident)


@router.post("/api/jobs/{id}/review/segments/{segment_id}/unlock")
async def post_job_review_unlock(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("edit:job")),
) -> dict[str, Any]:
    return await routes_segments.post_job_review_unlock(request, id, segment_id, ident)


@router.get("/api/jobs/{id}/review/segments/{segment_id}/audio")
async def get_job_review_audio(
    request: Request,
    id: str,
    segment_id: int,
    ident: Identity = Depends(require_scope("read:job")),
) -> Response:
    return await routes_preview.get_job_review_audio(request, id, segment_id, ident)
