from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from dubbing_pipeline.server import app
from dubbing_pipeline.utils.route_map import collect_route_details, format_route_details


def test_routes_snapshot() -> None:
    snap_path = Path(__file__).parent / "fixtures" / "routes_snapshot.txt"
    expected = snap_path.read_text(encoding="utf-8")
    current = format_route_details(collect_route_details(app))
    if current == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            fromfile=str(snap_path),
            tofile="current",
        )
    )
    pytest.fail(
        "Route snapshot changed. "
        "Run: python scripts/print_routes.py > tests/fixtures/routes_snapshot.txt\n"
        + diff
    )
