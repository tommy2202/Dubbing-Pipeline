from __future__ import annotations

from dubbing_pipeline.security.policy import (  # re-export legacy shim
    audit_policy_event,
    dep_invite_only,
    dep_request_allowed,
    dep_user,
    require_can_view_artifact,
    require_can_view_job,
    require_invite_member,
    require_invite_only,
    require_quota,
    require_quota_for_submit,
    require_quota_for_upload,
    require_remote_access,
)
