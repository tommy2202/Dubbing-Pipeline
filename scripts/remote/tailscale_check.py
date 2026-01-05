from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from config.settings import get_settings


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)  # nosec B603
    return int(p.returncode), p.stdout.strip(), p.stderr.strip()


def _tailscale_installed() -> bool:
    return shutil.which("tailscale") is not None


def _status_json() -> dict[str, Any] | None:
    rc, out, err = _run(["tailscale", "status", "--json"])
    if rc != 0:
        return None
    try:
        obj = json.loads(out)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _get_ips(st: dict[str, Any]) -> list[str]:
    ips: list[str] = []
    self_node = st.get("Self") if isinstance(st.get("Self"), dict) else {}
    for k in ("TailscaleIPs", "TailscaleIPs6", "TailscaleIP"):
        v = self_node.get(k)
        if isinstance(v, list):
            ips.extend([str(x) for x in v if str(x).strip()])
        elif isinstance(v, str) and v.strip():
            ips.append(v.strip())
    # best-effort: dedupe while preserving order
    seen = set()
    out: list[str] = []
    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def main() -> int:
    s = get_settings()
    host = str(getattr(s, "host", "0.0.0.0"))
    port = int(getattr(s, "port", 8000))

    print("Tailscale check")
    print(f"- server bind host/port: {host}:{port}")
    print(f"- REMOTE_ACCESS_MODE: {getattr(s, 'remote_access_mode', 'off')}")

    if not _tailscale_installed():
        print("- tailscale: NOT installed (missing `tailscale` binary)")
        print("  Install Tailscale and log in on both server + phone.")
        return 1

    st = _status_json()
    if not st:
        print("- tailscale: installed, but status unavailable (not running or not logged in).")
        print("  Try: `tailscale up` then re-run this script.")
        return 2

    state = st.get("BackendState")
    print(f"- tailscale backend state: {state}")

    ips = _get_ips(st)
    if not ips:
        print("- tailscale IPs: not found")
        return 3

    print("- tailscale IPs:")
    for ip in ips:
        print(f"  - {ip}")

    # Prefer IPv4 for simplest phone URL.
    ip4 = next((x for x in ips if "." in x), ips[0])
    url = f"http://{ip4}:{port}/ui/login"
    print("")
    print("Open this URL on your phone (on mobile data via Tailscale):")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

