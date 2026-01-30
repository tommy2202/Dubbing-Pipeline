from __future__ import annotations

from fastapi import APIRouter

from dubbing_pipeline.web.routes.admin import router as admin_router
from dubbing_pipeline.web.routes.jobs_actions import router as jobs_actions_router
from dubbing_pipeline.web.routes.jobs_events import router as jobs_events_router, ws_router as jobs_ws_router
from dubbing_pipeline.web.routes.jobs_files import router as jobs_files_router
from dubbing_pipeline.web.routes.jobs_logs import router as jobs_logs_router
from dubbing_pipeline.web.routes.jobs_read import router as jobs_read_router
from dubbing_pipeline.web.routes.jobs_review import router as jobs_review_router
from dubbing_pipeline.web.routes.jobs_submit import router as jobs_submit_router
from dubbing_pipeline.web.routes.jobs_voice_refs import router as jobs_voice_refs_router
from dubbing_pipeline.web.routes.library import router as library_router
from dubbing_pipeline.web.routes.uploads import router as uploads_router

router = APIRouter()

# Uploads + file picker
router.include_router(uploads_router)

# Job submission + batch
router.include_router(jobs_submit_router)

# Job listing/detail + project profiles
router.include_router(jobs_read_router)

# Job logs + timeline
router.include_router(jobs_logs_router)

# Job actions (cancel/pause/resume/delete)
router.include_router(jobs_actions_router)

# Admin/operator controls (tags/archive/presets/projects/kill/rerun)
router.include_router(admin_router)

# Review + overrides + transcript editing
router.include_router(jobs_review_router)

# Voice refs + speaker mapping
router.include_router(jobs_voice_refs_router)

# Series/character library routes
router.include_router(library_router)

# Job files + streaming + QR
router.include_router(jobs_files_router)

# SSE job events
router.include_router(jobs_events_router)
# WebSocket job events (custom auth flow)
router.include_router(jobs_ws_router)
