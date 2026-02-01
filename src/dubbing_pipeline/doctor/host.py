from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from dubbing_pipeline.utils.doctor_types import CheckResult


def _inside_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cg = Path("/proc/1/cgroup")
        if cg.exists():
            text = cg.read_text(encoding="utf-8", errors="replace")
            if "docker" in text or "kubepods" in text or "containerd" in text:
                return True
    except Exception:
        pass
    return False


def _cmd_version(argv: list[str]) -> str:
    try:
        res = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
        )
        out = (res.stdout or "").strip().splitlines()
        return out[0] if out else ""
    except Exception:
        return ""


def _install_steps_docker() -> dict[str, list[str]]:
    return {
        "linux": [
            "sudo apt-get update",
            "sudo apt-get install -y docker.io",
            "sudo systemctl enable --now docker",
            "sudo usermod -aG docker $USER  # re-login required",
        ],
        "macos": [
            "Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/",
        ],
        "windows": [
            "Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/",
        ],
    }


def _install_steps_nvidia_toolkit() -> dict[str, list[str]]:
    return {
        "linux": [
            "sudo apt-get update",
            "sudo apt-get install -y nvidia-container-toolkit",
            "sudo systemctl restart docker",
        ],
        "macos": [
            "GPU containers are not supported on macOS; use a Linux host.",
        ],
        "windows": [
            "GPU containers require WSL2 + NVIDIA drivers; see: https://docs.nvidia.com/datacenter/cloud-native/",
        ],
    }


def _install_steps_tailscale() -> dict[str, list[str]]:
    return {
        "linux": [
            "curl -fsSL https://tailscale.com/install.sh | sh",
            "sudo tailscale up",
        ],
        "macos": [
            "brew install tailscale",
            "sudo tailscale up",
        ],
        "windows": [
            "Download from https://tailscale.com/download",
            "Run the installer and sign in",
        ],
    }


def check_docker_installed() -> CheckResult:
    inside = _inside_container()
    docker_bin = shutil.which("docker")
    sock = Path("/var/run/docker.sock")
    detected = bool(docker_bin or sock.exists())
    required = not inside
    status = "PASS" if detected else ("FAIL" if required else "WARN")
    details = {
        "stage": "A",
        "required": bool(required),
        "inside_container": bool(inside),
        "detected": bool(detected),
        "docker_bin": bool(docker_bin),
        "docker_sock": bool(sock.exists()),
        "version": _cmd_version(["docker", "--version"]) if docker_bin else "",
        "os": platform.system(),
        "install_steps": _install_steps_docker(),
    }
    remediation = ["Install Docker (see install_steps)."] if not detected else []
    return CheckResult(
        id="host_docker",
        name="Stage A: Host Docker installed",
        status=status,
        details=details,
        remediation=remediation,
    )


def check_nvidia_container_toolkit(*, require_gpu: bool) -> CheckResult:
    inside = _inside_container()
    requested = bool(require_gpu or os.environ.get("GPU_MODE") or os.environ.get("REQUIRE_GPU"))
    nvidia_cli = shutil.which("nvidia-container-cli")
    nvidia_ctk = shutil.which("nvidia-ctk")
    nvidia_smi = shutil.which("nvidia-smi")
    detected = bool(nvidia_cli or nvidia_ctk or nvidia_smi)
    required = bool(requested)
    status = "PASS" if detected else ("FAIL" if required else "WARN")
    details = {
        "stage": "A",
        "required": bool(required),
        "inside_container": bool(inside),
        "gpu_requested": bool(requested),
        "detected": bool(detected),
        "nvidia_container_cli": bool(nvidia_cli),
        "nvidia_ctk": bool(nvidia_ctk),
        "nvidia_smi": bool(nvidia_smi),
        "install_steps": _install_steps_nvidia_toolkit(),
    }
    remediation = ["Install NVIDIA Container Toolkit (see install_steps)."] if not detected else []
    return CheckResult(
        id="host_nvidia_container_toolkit",
        name="Stage A: Host NVIDIA container toolkit",
        status=status,
        details=details,
        remediation=remediation,
    )


def check_tailscale_installed(*, access_mode: str) -> CheckResult:
    mode = (access_mode or "off").strip().lower()
    ts_bin = shutil.which("tailscale")
    detected = bool(ts_bin)
    required = mode == "tailscale"
    status = "PASS" if (detected or not required) else "FAIL"
    details = {
        "stage": "A",
        "required": bool(required),
        "access_mode": mode,
        "detected": bool(detected),
        "tailscale_bin": bool(ts_bin),
        "install_steps": _install_steps_tailscale(),
    }
    remediation = ["Install Tailscale (see install_steps)."] if not detected and required else []
    return CheckResult(
        id="host_tailscale",
        name="Stage A: Host Tailscale installed",
        status=status,
        details=details,
        remediation=remediation,
    )
