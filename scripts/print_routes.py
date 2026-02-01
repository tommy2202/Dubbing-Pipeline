from __future__ import annotations

import sys

from dubbing_pipeline.server import app
from dubbing_pipeline.utils.route_map import collect_route_details, format_route_details


def main() -> int:
    items = collect_route_details(app)
    sys.stdout.write(format_route_details(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
