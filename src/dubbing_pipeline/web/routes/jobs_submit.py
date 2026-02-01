from __future__ import annotations

from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.web.routes.jobs_submit_batch import router as batch_router
from dubbing_pipeline.web.routes.jobs_submit_core import router as core_router
from dubbing_pipeline.web.routes.jobs_submit_upload import router as upload_router

router = secure_router()

router.include_router(core_router)
router.include_router(batch_router)
router.include_router(upload_router)
