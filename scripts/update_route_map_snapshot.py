from __future__ import annotations

import json
from pathlib import Path

from dubbing_pipeline.server import app
from dubbing_pipeline.utils.route_map import collect_route_map


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    snap_path = repo_root / "tests" / "route_map_snapshot.json"
    items = collect_route_map(app)
    snap_path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {snap_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
