from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterable

from dubbing_pipeline.utils.doctor_redaction import redact_obj
from dubbing_pipeline.utils.doctor_types import CheckResult, DoctorReport
from dubbing_pipeline.utils.io import atomic_write_text, write_json
from dubbing_pipeline.utils.log import logger

_STATUS_SET = {"PASS", "WARN", "FAIL"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_git_commit(root: Path) -> str | None:
    git_dir = root / ".git"
    head = git_dir / "HEAD"
    if not head.exists():
        return None
    try:
        head_text = head.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None
    if head_text.startswith("ref:"):
        ref = head_text.split(":", 1)[1].strip()
        ref_path = git_dir / ref
        try:
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            return None
        packed = git_dir / "packed-refs"
        if packed.exists():
            try:
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("#") or " " not in line:
                        continue
                    sha, name = line.split(" ", 1)
                    if name.strip() == ref:
                        return sha.strip()
            except Exception:
                return None
    return head_text or None


def _build_metadata() -> dict[str, Any]:
    ts = datetime.now(tz=timezone.utc).isoformat()
    app_version = None
    try:
        app_version = metadata.version("dubbing-pipeline")
    except Exception:
        app_version = None

    commit = None
    for key in ("GIT_COMMIT", "GITHUB_SHA", "COMMIT_SHA", "SOURCE_VERSION"):
        val = os.environ.get(key)
        if val:
            commit = str(val).strip()
            break
    if commit is None:
        commit = _read_git_commit(_repo_root())

    return {
        "timestamp": ts,
        "app_version": app_version,
        "git_commit": commit,
    }


def _normalize_status(status: str | None) -> str:
    s = str(status or "").strip().upper()
    if s not in _STATUS_SET:
        return "FAIL"
    return s


def _normalize_result(result: CheckResult, *, fallback_id: str) -> CheckResult:
    res = result
    if not res.id:
        res = replace(res, id=str(fallback_id))
    if not res.name:
        res = replace(res, name=str(res.id))
    status = _normalize_status(res.status)
    if status != res.status:
        res = replace(res, status=status)
    rem = [str(x) for x in (res.remediation or []) if str(x).strip()]
    if rem != res.remediation:
        res = replace(res, remediation=rem)
    return res


def run_checks(checks: Iterable[Callable[[], CheckResult]]) -> DoctorReport:
    metadata = _build_metadata()
    logger.info("doctor_run_start", metadata=redact_obj(metadata))

    results: list[CheckResult] = []
    for check in checks:
        check_name = getattr(check, "__name__", "check")
        logger.info("doctor_check_start", check_name=str(check_name))
        try:
            res = check()
            if not isinstance(res, CheckResult):
                raise TypeError("Check did not return CheckResult")
            res = _normalize_result(res, fallback_id=str(check_name))
        except Exception as ex:
            res = CheckResult(
                id=str(check_name),
                name=str(check_name),
                status="FAIL",
                details=str(ex),
                remediation=[],
            )
        results.append(res)
        logger.info(
            "doctor_check_done",
            check_id=str(res.id),
            check_name=str(res.name),
            status=str(res.status),
            details=redact_obj(res.details),
        )

    report = DoctorReport(metadata=metadata, checks=results)
    logger.info("doctor_run_done", summary=report.summary())
    return report


def write_report(
    path: str | Path,
    *,
    text: str | None = None,
    json_data: dict[str, Any] | None = None,
) -> None:
    if text is None and json_data is None:
        raise ValueError("write_report requires text or json_data")
    target = Path(path)
    if text is not None:
        atomic_write_text(target, text)
    if json_data is not None:
        safe = redact_obj(json_data)
        if text is None:
            write_json(target, safe)
        else:
            write_json(target.with_suffix(target.suffix + ".json"), safe)
