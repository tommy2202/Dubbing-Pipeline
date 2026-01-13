from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Notification:
    event: str
    title: str
    message: str
    url: str | None = None
    tags: Sequence[str] | None = None
    priority: int | None = None  # 1..5 (ntfy convention)
