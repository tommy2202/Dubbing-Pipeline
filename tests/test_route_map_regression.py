from __future__ import annotations

import json
from pathlib import Path

from dubbing_pipeline.server import app
from dubbing_pipeline.utils.route_map import collect_route_map


def test_route_map_snapshot() -> None:
    snap_path = Path(__file__).with_name("route_map_snapshot.json")
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    current = collect_route_map(app)
    assert current == snap, "route map changed; run scripts/update_route_map_snapshot.py"
