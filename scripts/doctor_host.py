#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("STRICT_SECRETS", "0")

from dubbing_pipeline.config import get_settings  # noqa: E402
from dubbing_pipeline.utils.doctor_redaction import redact  # noqa: E402
from dubbing_pipeline.utils.doctor_report import format_report_text  # noqa: E402
from dubbing_pipeline.utils.doctor_runner import run_checks, write_report  # noqa: E402
from dubbing_pipeline.utils.doctor_types import CheckResult  # noqa: E402


@dataclass(frozen=True, slots=True)
class CmdResult:
    code: int
    out: str
    err: str


def _run_cmd(cmd: list[str], *, timeout: int = 10) -> CmdResult:
    try:
        p = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)
        return CmdResult(code=int(p.returncode), out=str(p.stdout or ""), err=str(p.stderr or ""))
    except Exception as ex:
        return CmdResult(code=1, out="", err=str(ex))


def _first_line(text: str) -> str:
    lines = (text or "").splitlines()
    return lines[0] if lines else ""


def _default_report_path() -> Path:
    return (Path.cwd() / "Logs" / "setup_report_host.txt").resolve()


def check_docker_installed() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            id="docker_installed",
            name="Docker installed",
            status="FAIL",
            details={"installed": False},
            remediation=[
                "sudo apt-get update && sudo apt-get install -y docker.io",
                "sudo systemctl enable --now docker",
            ],
        )

    res = _run_cmd(["docker", "--version"], timeout=5)
    ok = res.code == 0
    line = redact(_first_line(res.out or res.err))
    return CheckResult(
        id="docker_installed",
        name="Docker installed",
        status="PASS" if ok else "FAIL",
        details={"installed": True, "version": line},
        remediation=["sudo apt-get update && sudo apt-get install -y docker.io"] if not ok else [],
    )


def check_docker_daemon() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            id="docker_daemon",
            name="Docker daemon running",
            status="FAIL",
            details={"running": False, "reason": "docker not installed"},
            remediation=["sudo apt-get update && sudo apt-get install -y docker.io"],
        )
    res = _run_cmd(["docker", "info"], timeout=8)
    ok = res.code == 0
    err_line = redact(_first_line(res.err or res.out))
    return CheckResult(
        id="docker_daemon",
        name="Docker daemon running",
        status="PASS" if ok else "FAIL",
        details={"running": bool(ok), "error": err_line if not ok else ""},
        remediation=[
            "sudo systemctl start docker",
            "sudo systemctl enable docker",
            "sudo usermod -aG docker $USER",
        ]
        if not ok
        else [],
    )


def check_nvidia_driver(*, require_gpu: bool) -> CheckResult:
    if platform.system().lower() != "linux":
        return CheckResult(
            id="nvidia_driver",
            name="NVIDIA driver present",
            status="FAIL" if require_gpu else "WARN",
            details={"platform": platform.system()},
            remediation=["Install NVIDIA drivers on a Linux host."],
        )

    smi_ok = False
    if shutil.which("nvidia-smi"):
        res = _run_cmd(["nvidia-smi"], timeout=5)
        smi_ok = res.code == 0

    proc_ok = Path("/proc/driver/nvidia/version").exists()
    ok = smi_ok or proc_ok
    status = "PASS" if ok else ("FAIL" if require_gpu else "WARN")
    return CheckResult(
        id="nvidia_driver",
        name="NVIDIA driver present",
        status=status,
        details={"nvidia_smi": bool(smi_ok), "proc_driver": bool(proc_ok)},
        remediation=["Install NVIDIA drivers (nvidia-smi should work)."] if not ok else [],
    )


def check_nvidia_container_toolkit(*, require_gpu: bool, cuda_tag: str) -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            id="nvidia_container_toolkit",
            name="NVIDIA Container Toolkit",
            status="FAIL" if require_gpu else "WARN",
            details={"available": False, "reason": "docker not installed"},
            remediation=["Install Docker and NVIDIA Container Toolkit."],
        )
    res = _run_cmd(["docker", "info"], timeout=8)
    if res.code != 0:
        return CheckResult(
            id="nvidia_container_toolkit",
            name="NVIDIA Container Toolkit",
            status="FAIL" if require_gpu else "WARN",
            details={"available": False, "reason": "docker daemon not running"},
            remediation=["sudo systemctl start docker"],
        )

    cmd = ["docker", "run", "--rm", "--gpus", "all", f"nvidia/cuda:{cuda_tag}", "nvidia-smi"]
    res = _run_cmd(cmd, timeout=60)
    ok = res.code == 0
    out_line = redact(_first_line(res.out or res.err))
    status = "PASS" if ok else ("FAIL" if require_gpu else "WARN")
    return CheckResult(
        id="nvidia_container_toolkit",
        name="NVIDIA Container Toolkit",
        status=status,
        details={"ok": bool(ok), "output": out_line},
        remediation=[
            "sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit",
            "sudo systemctl restart docker",
        ]
        if not ok
        else [],
    )


def check_disk_free() -> CheckResult:
    try:
        s = get_settings()
        output_dir = Path(str(getattr(s, "output_dir", "Output") or "Output")).resolve()
        min_free_gb = int(getattr(s, "min_free_gb", 10) or 10)
    except Exception:
        output_dir = (Path.cwd() / "Output").resolve()
        min_free_gb = 10

    if "MIN_FREE_GB" in os.environ:
        try:
            min_free_gb = int(os.environ.get("MIN_FREE_GB", "10") or "10")
        except Exception:
            min_free_gb = min_free_gb

    usage = shutil.disk_usage(str(output_dir))
    free_gb = float(usage.free) / (1024**3)
    total_gb = float(usage.total) / (1024**3)
    low = free_gb < float(min_free_gb)
    status = "WARN" if low else "PASS"
    return CheckResult(
        id="disk_free",
        name="Disk free space",
        status=status,
        details={"path": str(output_dir), "free_gb": round(free_gb, 1), "total_gb": round(total_gb, 1)},
        remediation=["Free disk space or increase MIN_FREE_GB."] if low else [],
    )


def check_port_available() -> CheckResult:
    host = "127.0.0.1"
    port = 8000
    try:
        s = get_settings()
        host = str(getattr(s, "host", host) or host)
        port = int(getattr(s, "port", port) or port)
    except Exception:
        host = os.environ.get("HOST", host) or host
        try:
            port = int(os.environ.get("PORT", str(port)) or port)
        except Exception:
            port = port

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", int(port)))
        status = "PASS"
        details = {"host": host, "port": int(port), "available": True}
        remediation: list[str] = []
    except OSError as ex:
        if ex.errno == errno.EADDRINUSE:
            status = "FAIL"
            details = {"host": host, "port": int(port), "available": False}
            remediation = ["export PORT=8000  # choose a free port", "lsof -iTCP -sTCP:LISTEN -n -P"]
        else:
            status = "WARN"
            details = {"host": host, "port": int(port), "available": False, "error": str(ex)}
            remediation = ["export PORT=8000  # choose a free port"]
    finally:
        sock.close()
    return CheckResult(
        id="port_available",
        name="Web port availability",
        status=status,
        details=details,
        remediation=remediation,
    )


def check_tailscale() -> CheckResult:
    if not shutil.which("tailscale"):
        return CheckResult(
            id="tailscale",
            name="Tailscale status (optional)",
            status="WARN",
            details={"installed": False, "state": "not_installed"},
            remediation=["Install Tailscale and run: tailscale up"],
        )

    res = _run_cmd(["tailscale", "status", "--json"], timeout=5)
    state = "unknown"
    ok = False
    if res.code == 0 and res.out:
        try:
            data = json.loads(res.out)
            state = str(data.get("BackendState") or "unknown")
            ok = state.lower() == "running"
        except Exception:
            state = "unknown"
            ok = False

    status = "PASS" if ok else "WARN"
    return CheckResult(
        id="tailscale",
        name="Tailscale status (optional)",
        status=status,
        details={"installed": True, "state": state},
        remediation=["tailscale up"] if not ok else [],
    )


def _build_checks(*, require_gpu: bool, cuda_tag: str) -> Iterable:
    checks: list = [
        check_docker_installed,
        check_docker_daemon,
        check_disk_free,
        check_port_available,
        check_tailscale,
    ]
    if require_gpu:
        checks.extend(
            [
                lambda: check_nvidia_driver(require_gpu=require_gpu),
                lambda: check_nvidia_container_toolkit(require_gpu=require_gpu, cuda_tag=cuda_tag),
            ]
        )
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description="Host doctor (container setup checks).")
    ap.add_argument("--require-gpu", action="store_true", help="Require GPU checks to pass.")
    ap.add_argument(
        "--cuda-tag",
        default="12.3.2-base-ubuntu22.04",
        help="CUDA container tag for nvidia-smi test.",
    )
    args = ap.parse_args()

    report_path = _default_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    report = run_checks(_build_checks(require_gpu=bool(args.require_gpu), cuda_tag=str(args.cuda_tag)))
    text = format_report_text(report)
    write_report(report_path, text=text)

    summary = report.summary()
    print(f"Host doctor: PASS={summary['PASS']} WARN={summary['WARN']} FAIL={summary['FAIL']}")
    print(f"Report: {report_path}")
    return 2 if summary["FAIL"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
