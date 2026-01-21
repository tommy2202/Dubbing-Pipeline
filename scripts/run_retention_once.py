from __future__ import annotations

import json
from dataclasses import asdict

from dubbing_pipeline.ops.retention import run_once


def main() -> None:
    res = run_once()
    print(json.dumps(asdict(res), sort_keys=True))


if __name__ == "__main__":
    main()
