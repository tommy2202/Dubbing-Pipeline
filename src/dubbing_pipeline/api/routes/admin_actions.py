from __future__ import annotations

from .admin_actions_impl import (  # noqa: F401,F403
    _INVITE_TTL_DEFAULT_HOURS,
    _INVITE_TTL_MAX_HOURS,
    _infer_failure_stage,
)
from .admin_actions_impl import *  # noqa: F401,F403

_IMPL_MODULE = "dubbing_pipeline.api.routes.admin_actions_impl"
for _name, _obj in list(globals().items()):
    if getattr(_obj, "__module__", None) == _IMPL_MODULE:
        try:
            _obj.__module__ = __name__
        except Exception:
            pass
del _name, _obj, _IMPL_MODULE
