from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DoctorStatus = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    name: str
    status: DoctorStatus
    details: Any = None
    remediation: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    metadata: dict[str, Any]
    checks: list[CheckResult] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for c in self.checks:
            if c.status in counts:
                counts[c.status] += 1
        return counts
