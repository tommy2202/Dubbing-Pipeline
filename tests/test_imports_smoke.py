from __future__ import annotations

import importlib


def test_imports_smoke() -> None:
    modules = [
        "dubbing_pipeline",
        "dubbing_pipeline.cli",
        "dubbing_pipeline.cli.commands_run",
        "dubbing_pipeline.cli.args_common",
        "dubbing_pipeline.cli.output_format",
        "dubbing_pipeline.server",
        "dubbing_pipeline.web.app",
        "dubbing_pipeline.jobs.queue",
        "dubbing_pipeline.jobs.store",
        "dubbing_pipeline.stages.tts",
        "dubbing_pipeline.qa.scoring",
        "dubbing_pipeline.queue.redis_queue",
        "dubbing_pipeline.api.routes.admin_actions",
        "dubbing_pipeline.web.routes.jobs_review",
    ]
    for name in modules:
        importlib.import_module(name)
