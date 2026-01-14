#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _run(cmd: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _print_fix_steps() -> None:
    print(
        "\nFix steps (common):\n"
        "- If running in Docker, ensure you use the NVIDIA runtime and the container can see libcuda:\n"
        "    - install nvidia-container-toolkit on host\n"
        "    - run with: --gpus all\n"
        "- Ensure PyTorch build matches your driver/CUDA:\n"
        "    - check `nvidia-smi` driver version\n"
        "    - install the matching torch wheel from pytorch.org\n"
        "- If torch is CPU-only, install a CUDA-enabled torch build.\n",
        file=sys.stderr,
    )


def main() -> int:
    # Opt-out for environments where GPU probing is undesirable.
    if os.environ.get("SKIP_GPU_TESTS", "").strip() in {"1", "true", "yes"}:
        print("SKIP: SKIP_GPU_TESTS is set")
        return 0

    has_nvidia_smi = bool(shutil.which("nvidia-smi"))
    smi_out = ""
    if has_nvidia_smi:
        p = _run(["nvidia-smi"], timeout=10)
        smi_out = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode != 0:
            print("FAIL: nvidia-smi present but failed", file=sys.stderr)
            print(smi_out.strip(), file=sys.stderr)
            _print_fix_steps()
            return 2
        print("nvidia-smi: OK")
        print(smi_out.splitlines()[0])
    else:
        print("nvidia-smi: not found (this is normal on CPU runners)")

    try:
        import torch  # type: ignore
    except Exception as ex:
        if has_nvidia_smi:
            print(
                "FAIL: GPU appears present (nvidia-smi ok) but torch is not installed.\n"
                f"torch import error: {ex}",
                file=sys.stderr,
            )
            _print_fix_steps()
            return 2
        print(f"SKIP: torch not installed ({ex})")
        return 0

    torch_ver = getattr(torch, "__version__", "unknown")
    cuda_build = getattr(getattr(torch, "version", None), "cuda", None)
    cudnn = None
    try:
        cudnn = torch.backends.cudnn.version()  # type: ignore[attr-defined]
    except Exception:
        cudnn = None

    print(f"torch: {torch_ver}")
    print(f"torch.version.cuda: {cuda_build}")
    print(f"cudnn: {cudnn}")

    try:
        available = bool(torch.cuda.is_available())
    except Exception as ex:
        print(f"FAIL: torch.cuda.is_available() raised: {ex}", file=sys.stderr)
        _print_fix_steps()
        return 2

    if not has_nvidia_smi and not available:
        print("SKIP: no GPU detected (nvidia-smi missing, torch reports cuda unavailable)")
        return 0

    if has_nvidia_smi and not available:
        print(
            "FAIL: nvidia-smi is available but torch reports CUDA unavailable.\n"
            "This usually indicates a driver/runtime mismatch or a CPU-only torch build.",
            file=sys.stderr,
        )
        _print_fix_steps()
        return 2

    if available:
        try:
            dev = torch.device("cuda:0")
            x = torch.zeros((1,), device=dev)
            _ = x + 1
            name = torch.cuda.get_device_name(0)
            print(f"cuda: OK device={name}")
        except Exception as ex:
            print(f"FAIL: CUDA allocation/test failed: {ex}", file=sys.stderr)
            _print_fix_steps()
            return 2

    print("e2e_gpu_sanity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

