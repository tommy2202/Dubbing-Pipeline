from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _ok(msg: str) -> None:
    print(f"[ok] {msg}")


def _warn(msg: str) -> None:
    print(f"[warn] {msg}")


def _err(msg: str) -> None:
    print(f"[err] {msg}")


def _looks_like_yaml(path: Path) -> bool:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return "tunnel:" in txt and "ingress:" in txt


def main() -> int:
    print("cloudflared check")

    if shutil.which("cloudflared") is None:
        _warn("`cloudflared` binary not found in PATH (ok if using docker compose)")
    else:
        _ok("cloudflared binary found")

    tmpl = Path("scripts/remote/cloudflared/config.yml")
    if tmpl.exists() and _looks_like_yaml(tmpl):
        _ok(f"template config present: {tmpl}")
    else:
        _warn(f"template config missing or unexpected: {tmpl}")

    # Repo deploy file uses CLOUDFLARE_TUNNEL_TOKEN env var.
    tok = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if tok:
        _ok("CLOUDFLARE_TUNNEL_TOKEN is set (do not print it)")
    else:
        _warn("CLOUDFLARE_TUNNEL_TOKEN is not set (expected in .env.secrets for docker tunnel mode)")

    # Access enforcement vars (not secrets)
    team = os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "").strip()
    aud = os.environ.get("CLOUDFLARE_ACCESS_AUD", "").strip()
    if team and aud:
        _ok("Cloudflare Access verification appears configured (TEAM_DOMAIN + AUD set)")
    else:
        _warn("Cloudflare Access verification not configured (TEAM_DOMAIN and/or AUD missing)")

    _ok("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

