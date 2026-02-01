from __future__ import annotations

from dubbing_pipeline.security.policy_deps import secure_router
from dubbing_pipeline.api.routes.admin_actions import router as actions_router
from dubbing_pipeline.api.routes.admin_reports import router as reports_router

router = secure_router(prefix="/api/admin", tags=["admin"])

router.include_router(actions_router)
router.include_router(reports_router)
