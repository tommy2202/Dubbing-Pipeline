from __future__ import annotations

import os
import secrets
import socket
import tempfile
import time
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config.secret_config import SecretConfig
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.runtime import lifecycle
from dubbing_pipeline.runtime.lifecycle import _ensure_writable_dir, _run_version
from dubbing_pipeline.doctor.models import build_model_requirement_checks, selected_pipeline_mode
from dubbing_pipeline.utils.doctor_types import CheckResult
from dubbing_pipeline.utils.ffmpeg_safe import FFmpegError, run_ffmpeg
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.utils.paths import default_paths

_IMPORT_MODULES = (
    "config.settings",
    "dubbing_pipeline.config",
    "dubbing_pipeline.utils.log",
    "dubbing_pipeline.utils.ffmpeg_safe",
    "dubbing_pipeline.server",
    "dubbing_pipeline.web.app",
    "dubbing_pipeline.cli",
)


def default_report_path() -> Path:
    if Path("/data").exists():
        base = Path("/data/Logs")
    else:
        base = Path.cwd() / "Logs"
    return base / "setup_report_container.txt"


@contextmanager
def _temp_env(overrides: dict[str, str]) -> Iterable[None]:
    prev: dict[str, str | None] = {}
    for k, v in overrides.items():
        prev[k] = os.environ.get(k)
        os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, old in prev.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _secret_raw(value: object | None) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        try:
            return str(value.get_secret_value() or "")
        except Exception:
            return ""
    return str(value)


def _default_marker(field_name: str) -> str:
    field = getattr(SecretConfig, "model_fields", {}).get(field_name)
    if not field:
        return ""
    default = getattr(field, "default", None)
    return _secret_raw(default)


def _classify_secret(value: str, *, default_marker: str) -> str:
    v = (value or "").strip()
    if not v:
        return "missing"
    if default_marker and v == default_marker:
        return "default/change-me"
    if v.lower() in {"change-me", "changeme", "admin", "adminpass", "password", "123456"}:
        return "default/change-me"
    if len(v) < 12:
        return "too short"
    return "ok"


def _secret_remediation(env_name: str) -> str:
    if env_name == "ADMIN_USERNAME":
        return 'export ADMIN_USERNAME="admin"'
    if env_name == "ADMIN_PASSWORD":
        return (
            "export ADMIN_PASSWORD=\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')\""
        )
    return (
        f"export {env_name}=\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\""
    )


def check_import_smoke() -> CheckResult:
    failures: dict[str, str] = {}
    with _temp_env(
        {
            "STRICT_SECRETS": "0",
            "OFFLINE_MODE": "1",
            "ALLOW_EGRESS": "0",
            "ALLOW_HF_EGRESS": "0",
            "ENABLE_PYANNOTE": "0",
            "COQUI_TOS_AGREED": "0",
        }
    ):
        for mod in _IMPORT_MODULES:
            try:
                import_module(mod)
            except Exception as ex:  # pragma: no cover - variable failure types
                failures[mod] = f"{type(ex).__name__}"

    if failures:
        return CheckResult(
            id="imports_smoke",
            name="Imports smoke (core modules)",
            status="FAIL",
            details={"failed": failures, "attempted": list(_IMPORT_MODULES)},
            remediation=["python3 -m pip install -e .", "python3 scripts/smoke_import_all.py"],
        )
    return CheckResult(
        id="imports_smoke",
        name="Imports smoke (core modules)",
        status="PASS",
        details={"attempted": list(_IMPORT_MODULES), "failed": {}},
        remediation=[],
    )


def check_ffmpeg() -> CheckResult:
    s = get_settings()
    ffmpeg_bin = str(getattr(s, "ffmpeg_bin", "ffmpeg") or "ffmpeg")
    ver = _run_version([ffmpeg_bin, "-version"])
    if not ver:
        return CheckResult(
            id="ffmpeg",
            name="ffmpeg available",
            status="FAIL",
            details={"available": False},
            remediation=["sudo apt-get install -y ffmpeg"],
        )
    return CheckResult(
        id="ffmpeg",
        name="ffmpeg available",
        status="PASS",
        details={"available": True, "version": ver},
        remediation=[],
    )


def check_torch_cuda(*, require_gpu: bool) -> CheckResult:
    try:
        import torch  # type: ignore
    except Exception:
        status = "FAIL" if require_gpu else "WARN"
        return CheckResult(
            id="torch_cuda",
            name="Torch CUDA availability",
            status=status,
            details={"torch_installed": False, "cuda_available": False},
            remediation=["python3 -m pip install torch --index-url https://download.pytorch.org/whl/cu121"],
        )

    cuda_available = bool(getattr(torch.cuda, "is_available", lambda: False)())
    device_name = ""
    if cuda_available:
        try:
            device_name = str(torch.cuda.get_device_name(0))
        except Exception:
            device_name = ""
    status = "PASS"
    if require_gpu and not cuda_available:
        status = "FAIL"
    elif not cuda_available:
        status = "WARN"
    return CheckResult(
        id="torch_cuda",
        name="Torch CUDA availability",
        status=status,
        details={
            "torch_installed": True,
            "cuda_available": bool(cuda_available),
            "device_name": device_name if cuda_available else "",
        },
        remediation=["Ensure NVIDIA drivers/CUDA are installed and the container is started with GPU access."],
    )


def check_required_secrets() -> CheckResult:
    try:
        sec = SecretConfig()
    except Exception:
        sec = None
    required = {
        "JWT_SECRET": "jwt_secret",
        "CSRF_SECRET": "csrf_secret",
        "SESSION_SECRET": "session_secret",
        "API_TOKEN": "api_token",
        "ADMIN_USERNAME": "admin_username",
        "ADMIN_PASSWORD": "admin_password",
    }
    details: dict[str, str] = {}
    missing = False
    weak = False
    remediation: list[str] = []

    for env_name, attr in required.items():
        raw = _secret_raw(getattr(sec, attr, None) if sec is not None else None)
        marker = _default_marker(attr)
        classification = _classify_secret(raw, default_marker=marker)
        details[env_name] = classification
        if classification == "missing":
            missing = True
            remediation.append(_secret_remediation(env_name))
        elif classification in {"default/change-me", "too short"}:
            weak = True
            remediation.append(_secret_remediation(env_name))

    if missing:
        status = "FAIL"
    elif weak:
        status = "WARN"
    else:
        status = "PASS"

    if remediation:
        remediation.insert(0, "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")

    return CheckResult(
        id="secrets_strength",
        name="Required secrets presence/strength",
        status=status,
        details=details,
        remediation=remediation,
    )


def check_writable_dirs() -> CheckResult:
    s = get_settings()
    root = Path(str(getattr(s, "app_root", Path.cwd()))).resolve()
    input_dir = Path(str(getattr(s, "input_dir", "") or root / "Input")).resolve()
    output_dir = Path(str(getattr(s, "output_dir", "") or root / "Output")).resolve()
    log_dir = Path(str(getattr(s, "log_dir", "") or root / "logs")).resolve()
    uploads_dir = default_paths().uploads_dir.resolve()
    temp_dir = Path(tempfile.gettempdir()).resolve()

    checks = {
        "Input dir": input_dir,
        "Output dir": output_dir,
        "Logs dir": log_dir,
        "Uploads dir": uploads_dir,
        "Temp dir": temp_dir,
    }
    details: dict[str, str] = {}
    failed = False
    for label, path in checks.items():
        try:
            _ensure_writable_dir(path, label=label)
            details[label] = "ok"
        except Exception:
            failed = True
            details[label] = "not writable"
            logger.warning("doctor_dir_not_writable", label=label)

    status = "FAIL" if failed else "PASS"
    remediation = [
        "mkdir -p Input Output Logs",
        "chmod -R u+rwX Input Output Logs",
    ]
    return CheckResult(
        id="writable_dirs",
        name="Writable runtime directories",
        status=status,
        details=details,
        remediation=remediation if failed else [],
    )


def _random_secret(n: int = 24) -> str:
    try:
        return secrets.token_urlsafe(int(max(8, n)))
    except Exception:
        return secrets.token_urlsafe(12)


def _ensure_tiny_mp4(path: Path) -> None:
    s = get_settings()
    ffmpeg_bin = str(getattr(s, "ffmpeg_bin", "ffmpeg") or "ffmpeg")
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        ffmpeg_bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=10",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t",
        "1.5",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    run_ffmpeg(argv, timeout_s=30, retries=0, capture=True)


def _login_admin(client, *, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise RuntimeError(f"admin_login_failed status={r.status_code}")
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def _pick_output_path(files: dict) -> Path | None:
    for key in ("mkv", "mp4", "mobile_mp4", "mobile_original_mp4", "lipsync_mp4"):
        ent = files.get(key)
        if isinstance(ent, dict) and ent.get("path"):
            return Path(str(ent["path"]))
    return None


def check_full_job(*, timeout_s: int = 120) -> CheckResult:
    """
    Run a tiny low-mode job via the API and validate outputs.
    """
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        return CheckResult(
            id="full_job",
            name="Full mode tiny job",
            status="FAIL",
            details={"error": f"testclient_unavailable:{type(ex).__name__}"},
            remediation=["python3 -m pip install fastapi"],
        )

    admin_user = "admin"
    admin_pass = _random_secret(16)
    with tempfile.TemporaryDirectory(prefix="dp_doctor_full_") as td:
        root = Path(td).resolve()
        in_dir = root / "Input"
        out_dir = root / "Output"
        logs_dir = root / "logs"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = in_dir / "Doctor.mp4"
        try:
            _ensure_tiny_mp4(mp4_path)
        except FFmpegError as ex:
            return CheckResult(
                id="full_job",
                name="Full mode tiny job",
                status="FAIL",
                details={"error": "ffmpeg_failed", "message": str(ex)[:200]},
                remediation=["ffmpeg -version", "sudo apt-get install -y ffmpeg"],
            )
        except Exception as ex:
            return CheckResult(
                id="full_job",
                name="Full mode tiny job",
                status="FAIL",
                details={"error": "ffmpeg_failed", "message": str(ex)[:200]},
                remediation=["ffmpeg -version", "sudo apt-get install -y ffmpeg"],
            )

        env = {
            "APP_ROOT": str(root),
            "INPUT_DIR": str(in_dir),
            "DUBBING_OUTPUT_DIR": str(out_dir),
            "DUBBING_LOG_DIR": str(logs_dir),
            "ADMIN_USERNAME": admin_user,
            "ADMIN_PASSWORD": admin_pass,
            "COOKIE_SECURE": "0",
            "STRICT_SECRETS": "0",
            "OFFLINE_MODE": "1",
            "ALLOW_EGRESS": "0",
            "ALLOW_HF_EGRESS": "0",
            "ENABLE_MODEL_DOWNLOADS": "0",
            "ENABLE_PYANNOTE": "0",
            "COQUI_TOS_AGREED": "0",
            "TTS_PROVIDER": "espeak",
            "JWT_SECRET": _random_secret(32),
            "CSRF_SECRET": _random_secret(32),
            "SESSION_SECRET": _random_secret(32),
            "API_TOKEN": _random_secret(32),
        }

        with _temp_env(env):
            get_settings.cache_clear()
            lifecycle.end_draining()
            try:
                import importlib
                import dubbing_pipeline.server as server

                server = importlib.reload(server)
                app = server.app
            except Exception as ex:
                return CheckResult(
                    id="full_job",
                    name="Full mode tiny job",
                    status="FAIL",
                    details={"error": f"app_import_failed:{type(ex).__name__}"},
                    remediation=["python3 -m pip install -e ."],
                )

            with TestClient(app) as client:
                try:
                    headers = _login_admin(client, username=admin_user, password=admin_pass)
                except Exception as ex:
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": f"login_failed:{type(ex).__name__}"},
                        remediation=["Check ADMIN_USERNAME/ADMIN_PASSWORD configuration."],
                    )

                resp = client.post(
                    "/api/jobs",
                    headers=headers,
                    json={
                        "video_path": str(mp4_path),
                        "mode": "low",
                        "device": "cpu",
                        "series_title": "Doctor Series",
                        "season_number": 1,
                        "episode_number": 1,
                    },
                )
                if resp.status_code != 200:
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": "job_submit_failed", "status": int(resp.status_code)},
                        remediation=["Check logs under Output/ and ensure Input/Output are writable."],
                    )
                job_id = resp.json().get("id")
                if not job_id:
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": "job_submit_no_id"},
                        remediation=["Check server logs for job submission errors."],
                    )

                deadline = time.monotonic() + float(timeout_s)
                job = None
                state = ""
                while time.monotonic() < deadline:
                    jr = client.get(f"/api/jobs/{job_id}", headers=headers)
                    if jr.status_code != 200:
                        time.sleep(1.0)
                        continue
                    job = jr.json()
                    state = str(job.get("state") or "")
                    if state in {"DONE", "FAILED", "CANCELED"}:
                        break
                    time.sleep(1.0)

                if not job or state != "DONE":
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={
                            "error": "job_timeout_or_failed",
                            "state": state or "unknown",
                            "job_id": str(job_id),
                            "message": (job or {}).get("message") if isinstance(job, dict) else None,
                            "job_error": (job or {}).get("error") if isinstance(job, dict) else None,
                        },
                        remediation=[
                            "python3 scripts/smoke_run.py",
                            "Check logs under Output/ for pipeline errors.",
                        ],
                    )

                if str(job.get("visibility") or "") != "private":
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": "visibility_not_private", "visibility": job.get("visibility")},
                        remediation=["Verify default visibility settings in the server config."],
                    )

                base_dir = None
                try:
                    from dubbing_pipeline.jobs.models import Job
                    from dubbing_pipeline.library.paths import get_job_output_root

                    base_dir = get_job_output_root(Job.from_dict(job))
                except Exception:
                    base_dir = None

                files = client.get(f"/api/jobs/{job_id}/files", headers=headers)
                if files.status_code != 200:
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": "files_endpoint_failed", "status": int(files.status_code)},
                        remediation=["Check job output directories for artifacts."],
                    )

                data = files.json()
                output_path = _pick_output_path(data)
                if not output_path or not output_path.exists():
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={"error": "output_missing", "job_id": str(job_id)},
                        remediation=["Check job output directories for artifacts."],
                    )

                manifest = (Path(base_dir) if base_dir else output_path.parent) / "manifest.json"
                if not manifest.exists():
                    return CheckResult(
                        id="full_job",
                        name="Full mode tiny job",
                        status="FAIL",
                        details={
                            "error": "manifest_missing",
                            "job_id": str(job_id),
                            "base_dir": str(base_dir) if base_dir else None,
                        },
                        remediation=["Check job output directories for manifest.json."],
                    )

        return CheckResult(
            id="full_job",
            name="Full mode tiny job",
            status="PASS",
            details={"job": "ok", "output": "present", "manifest": "present"},
            remediation=[],
        )


def check_redis() -> CheckResult:
    s = get_settings()
    redis_url = str(getattr(s, "redis_url", "") or "").strip()
    if not redis_url:
        return CheckResult(
            id="redis",
            name="Redis connectivity (optional)",
            status="WARN",
            details={"configured": False, "reachable": False},
            remediation=["export REDIS_URL=\"redis://host:6379/0\""],
        )

    url = redis_url if "://" in redis_url else f"redis://{redis_url}"
    parsed = urlparse(url)
    if parsed.scheme == "unix":
        socket_path = parsed.path
        ok = bool(socket_path and Path(socket_path).exists())
    else:
        host = parsed.hostname or ""
        port = int(parsed.port or 6379)
        ok = False
        try:
            with socket.create_connection((host, port), timeout=2):
                ok = True
        except Exception:
            ok = False
    status = "PASS" if ok else "WARN"
    return CheckResult(
        id="redis",
        name="Redis connectivity (optional)",
        status=status,
        details={"configured": True, "reachable": bool(ok)},
        remediation=["Check REDIS_URL and ensure Redis is reachable."],
    )


def check_ntfy() -> CheckResult:
    s = get_settings()
    enabled = bool(getattr(s, "ntfy_enabled", False))
    base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    topic = str(getattr(s, "ntfy_topic", "") or "").strip()
    if not enabled or not base or not topic:
        return CheckResult(
            id="ntfy",
            name="ntfy notifications (optional)",
            status="WARN",
            details={
                "enabled": bool(enabled),
                "base_configured": bool(base),
                "topic_configured": bool(topic),
                "reachable": False,
            },
            remediation=["Set NTFY_ENABLED=1, NTFY_BASE_URL, and NTFY_TOPIC."],
        )

    reachable = False
    try:
        req = Request(base, method="HEAD")
        with urlopen(req, timeout=3):
            reachable = True
    except Exception:
        try:
            req = Request(base, method="GET")
            with urlopen(req, timeout=3):
                reachable = True
        except Exception:
            reachable = False

    status = "PASS" if reachable else "WARN"
    return CheckResult(
        id="ntfy",
        name="ntfy notifications (optional)",
        status=status,
        details={
            "enabled": bool(enabled),
            "base_configured": True,
            "topic_configured": True,
            "reachable": bool(reachable),
        },
        remediation=["Verify NTFY_BASE_URL is reachable from the container."],
    )


def _turn_scheme_ok(raw: str) -> bool:
    v = (raw or "").strip().lower()
    return v.startswith("turn:") or v.startswith("turns:")


def check_turn() -> CheckResult:
    s = get_settings()
    turn_url = _secret_raw(getattr(s, "turn_url", None))
    turn_username = _secret_raw(getattr(s, "turn_username", None))
    turn_password = _secret_raw(getattr(s, "turn_password", None))
    if not turn_url:
        return CheckResult(
            id="turn",
            name="TURN config (optional)",
            status="WARN",
            details={"configured": False, "url_format_ok": False, "creds_set": False},
            remediation=["Set TURN_URL, TURN_USERNAME, TURN_PASSWORD to enable WebRTC relay."],
        )

    url_ok = _turn_scheme_ok(turn_url)
    creds_ok = bool(turn_username and turn_password)
    status = "PASS" if (url_ok and creds_ok) else "WARN"
    return CheckResult(
        id="turn",
        name="TURN config (optional)",
        status=status,
        details={"configured": True, "url_format_ok": bool(url_ok), "creds_set": bool(creds_ok)},
        remediation=["Ensure TURN_URL uses turn: or turns: and credentials are set."],
    )


def build_container_quick_checks(
    *,
    require_gpu: bool,
    include_model_checks: bool = True,
    include_secrets: bool = True,
) -> list[Callable[[], CheckResult]]:
    mode = selected_pipeline_mode()
    checks: list[Callable[[], CheckResult]] = [
        check_import_smoke,
        check_ffmpeg,
        lambda: check_torch_cuda(require_gpu=require_gpu),
    ]
    if include_secrets:
        checks.append(check_required_secrets)
    checks.append(check_writable_dirs)
    if include_model_checks:
        checks.extend(build_model_requirement_checks(mode=mode))
    checks.extend([check_redis, check_ntfy, check_turn])
    return checks


def build_container_full_checks(*, require_gpu: bool) -> list[Callable[[], CheckResult]]:
    checks = build_container_quick_checks(
        require_gpu=require_gpu, include_model_checks=False, include_secrets=False
    )
    checks.append(check_full_job)
    return checks
