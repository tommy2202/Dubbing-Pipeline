from __future__ import annotations

from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.api.routes.library_artifacts import router as artifacts_router
from dubbing_pipeline.api.routes.library_browse import router as browse_router
from dubbing_pipeline.api.routes.library_search import router as search_router

router = secure_router(prefix="/api/library", tags=["library"])

router.include_router(browse_router)
router.include_router(search_router)
router.include_router(artifacts_router)
