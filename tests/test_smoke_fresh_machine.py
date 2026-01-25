from __future__ import annotations

from pathlib import Path

import pytest

from tests._helpers.smoke_fresh_machine import run_smoke_fresh_machine


@pytest.mark.smoke
def test_smoke_fresh_machine(tmp_path: Path) -> None:
    run_smoke_fresh_machine(
        tmp_path, ffmpeg_skip_message="ffmpeg not available; install ffmpeg to run smoke test"
    )
