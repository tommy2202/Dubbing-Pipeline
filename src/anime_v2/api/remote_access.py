from __future__ import annotations

import ipaddress
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from anime_v2.config import get_settings
from anime_v2.utils.log import logger


def _split_list(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    # allow comma or whitespace separated lists
    parts: list[str] = []
    for tok in s.replace(",", " ").split():
        t = tok.strip()
        if t:
            parts.append(t)
    return parts


def _parse_networks(spec: str) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for item in _split_list(spec):
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except Exception:
            # ignore invalid entries; enforcement will log effective networks at boot
            continue
    return nets


def _default_allowed_subnets_for_mode(mode: str) -> list[ipaddress._BaseNetwork]:
    mode = (mode or "off").strip().lower()
    if mode == "tailscale":
        # Allow LAN + Tailscale CGNAT by default. This prevents accidental public exposure
        # while still allowing phone-on-cellular via Tailscale.
        return [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),  # IPv6 ULA (includes tailscale's ULA on some setups)
        ]
    if mode == "cloudflare":
        # In Cloudflare Tunnel mode, the origin should typically receive traffic only from a local proxy
        # (cloudflared/caddy) on the same host/network namespace.
        #
        # Intentionally *not* allowing generic LAN by default: Cloudflare mode is meant to avoid
        # "bypass Cloudflare from the LAN" when the app is bound to 0.0.0.0.
        return [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),
        ]
    return []


def _default_trusted_proxy_subnets() -> list[ipaddress._BaseNetwork]:
    # Trust proxy headers only from local/private proxies.
    return [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
    ]


def _ip_in_any(ip: ipaddress._BaseAddress, nets: list[ipaddress._BaseNetwork]) -> bool:
    for n in nets:
        try:
            if ip in n:
                return True
        except Exception:
            continue
    return False


def _parse_ip(s: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address((s or "").strip())
    except Exception:
        return None


def _extract_forwarded_ip(request: Request) -> str | None:
    # Cloudflare standard header
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    # De-facto standard proxy header: first IP is the original client.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        return first or None
    return None


@dataclass(frozen=True, slots=True)
class RemoteDecision:
    allowed: bool
    mode: str
    client_ip: str
    raw_peer_ip: str
    via_proxy: bool
    reason: str


_JWKS_CACHE: dict[str, Any] = {"ts": 0.0, "jwks": None}


def _jwks_cache_path() -> Path:
    # /tmp is available in containers (tmpfs in provided compose files).
    return Path("/tmp/anime_v2_cf_access_jwks.json")


def _fetch_cf_access_jwks(team_domain: str) -> dict[str, Any]:
    # Public keys (not secrets). This requires egress.
    url = f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs"
    req = urllib.request.Request(url, headers={"user-agent": "anime-v2/remote-access"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or "keys" not in data:
        raise RuntimeError("invalid jwks")
    return data


def _load_cf_access_jwks(team_domain: str, *, max_age_sec: int = 86400) -> dict[str, Any]:
    now = time.time()
    cached = _JWKS_CACHE.get("jwks")
    ts = float(_JWKS_CACHE.get("ts") or 0.0)
    if isinstance(cached, dict) and (now - ts) < max_age_sec:
        return cached

    # Try disk cache first (works even if ALLOW_EGRESS=0 after first fetch).
    p = _jwks_cache_path()
    try:
        if p.exists():
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and "keys" in obj:
                _JWKS_CACHE["jwks"] = obj
                _JWKS_CACHE["ts"] = now
                return obj
    except Exception:
        pass

    s = get_settings()
    if not bool(getattr(s, "allow_egress", True)):
        raise RuntimeError("egress_disabled_no_cached_jwks")

    obj = _fetch_cf_access_jwks(team_domain)
    _JWKS_CACHE["jwks"] = obj
    _JWKS_CACHE["ts"] = now
    try:
        p.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
    return obj


def _verify_cf_access_jwt(token: str, *, team_domain: str, aud: str) -> dict[str, Any]:
    """
    Verify Cloudflare Access JWT (Cf-Access-Jwt-Assertion).

    This verifies signature + audience. Issuer is also checked when present.
    """
    try:
        import jwt  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("pyjwt_not_installed") from ex

    jwks = _load_cf_access_jwks(team_domain)
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise RuntimeError("invalid_jwks")

    # Build candidate public keys by kid (if present)
    unverified = jwt.get_unverified_header(token)
    kid = unverified.get("kid") if isinstance(unverified, dict) else None
    cand = []
    for k in keys:
        if not isinstance(k, dict):
            continue
        if kid and str(k.get("kid") or "") != str(kid):
            continue
        try:
            cand.append(jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k)))
        except Exception:
            continue
    if not cand:
        # fallback: try all keys (rare)
        for k in keys:
            if not isinstance(k, dict):
                continue
            try:
                cand.append(jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k)))
            except Exception:
                continue
    if not cand:
        raise RuntimeError("no_jwks_keys")

    last_err: Exception | None = None
    for pub in cand:
        try:
            data = jwt.decode(
                token,
                key=pub,
                algorithms=["RS256"],
                audience=aud,
                options={"require": ["exp", "iat"]},
            )
            if isinstance(data, dict):
                iss = data.get("iss")
                # Cloudflare Access tokens typically use this issuer format.
                # Do not hard-fail if iss is absent (some setups vary), but enforce when present.
                if isinstance(iss, str) and team_domain not in iss:
                    raise RuntimeError("issuer_mismatch")
                return data
        except Exception as ex:
            last_err = ex
            continue
    raise RuntimeError("invalid_access_jwt") from last_err


def decide_remote_access(request: Request) -> RemoteDecision:
    s = get_settings()
    mode = str(getattr(s, "remote_access_mode", "off") or "off").strip().lower()
    if mode not in {"off", "tailscale", "cloudflare"}:
        mode = "off"

    raw_peer = request.client.host if request.client else ""
    raw_ip = _parse_ip(raw_peer) or ipaddress.ip_address("0.0.0.0")

    allowed_nets = _parse_networks(str(getattr(s, "allowed_subnets", "") or ""))
    if not allowed_nets:
        allowed_nets = _default_allowed_subnets_for_mode(mode)

    trusted_proxy_nets = _parse_networks(str(getattr(s, "trusted_proxy_subnets", "") or ""))
    if not trusted_proxy_nets:
        trusted_proxy_nets = _default_trusted_proxy_subnets()

    eff_ip = raw_ip
    via_proxy = False
    trust_proxy = bool(getattr(s, "trust_proxy_headers", False)) and mode == "cloudflare"
    if trust_proxy and _ip_in_any(raw_ip, trusted_proxy_nets):
        fwd = _extract_forwarded_ip(request)
        fwd_ip = _parse_ip(fwd or "")
        if fwd_ip is not None:
            eff_ip = fwd_ip
            via_proxy = True

    if mode == "off":
        return RemoteDecision(
            allowed=True,
            mode=mode,
            client_ip=str(eff_ip),
            raw_peer_ip=str(raw_peer or ""),
            via_proxy=via_proxy,
            reason="remote_access_off",
        )

    # Always enforce allowed_subnets for non-off modes (prevents accidental public exposure).
    if not _ip_in_any(raw_ip, allowed_nets):
        return RemoteDecision(
            allowed=False,
            mode=mode,
            client_ip=str(eff_ip),
            raw_peer_ip=str(raw_peer or ""),
            via_proxy=via_proxy,
            reason="peer_ip_not_in_allowed_subnets",
        )

    if mode == "tailscale":
        # For tailscale mode, enforce the *effective* client IP too (when not behind proxy).
        if not _ip_in_any(eff_ip, allowed_nets):
            return RemoteDecision(
                allowed=False,
                mode=mode,
                client_ip=str(eff_ip),
                raw_peer_ip=str(raw_peer or ""),
                via_proxy=via_proxy,
                reason="client_ip_not_in_allowed_subnets",
            )
        return RemoteDecision(
            allowed=True,
            mode=mode,
            client_ip=str(eff_ip),
            raw_peer_ip=str(raw_peer or ""),
            via_proxy=via_proxy,
            reason="tailscale_allowlist_ok",
        )

    # cloudflare mode: if Access config is provided, require/verify Access JWT.
    team = getattr(s, "cloudflare_access_team_domain", None)
    aud = getattr(s, "cloudflare_access_aud", None)
    if team and aud:
        tok = (request.headers.get("cf-access-jwt-assertion") or "").strip()
        if not tok:
            return RemoteDecision(
                allowed=False,
                mode=mode,
                client_ip=str(eff_ip),
                raw_peer_ip=str(raw_peer or ""),
                via_proxy=via_proxy,
                reason="missing_cf_access_jwt",
            )
        try:
            _verify_cf_access_jwt(tok, team_domain=str(team), aud=str(aud))
        except Exception as ex:
            return RemoteDecision(
                allowed=False,
                mode=mode,
                client_ip=str(eff_ip),
                raw_peer_ip=str(raw_peer or ""),
                via_proxy=via_proxy,
                reason=f"invalid_cf_access_jwt:{type(ex).__name__}",
            )
        return RemoteDecision(
            allowed=True,
            mode=mode,
            client_ip=str(eff_ip),
            raw_peer_ip=str(raw_peer or ""),
            via_proxy=via_proxy,
            reason="cloudflare_access_ok",
        )

    # Otherwise: rely on app auth (RBAC/API keys) at the route layer, but keep IP allowlist.
    return RemoteDecision(
        allowed=True,
        mode=mode,
        client_ip=str(eff_ip),
        raw_peer_ip=str(raw_peer or ""),
        via_proxy=via_proxy,
        reason="cloudflare_no_access_config_ip_allowlist_only",
    )


async def remote_access_middleware(request: Request, call_next) -> Response:
    """
    Remote access enforcement for mobile-friendly deployments.

    - off: allow all (default)
    - tailscale: allow only LAN/private + tailscale CGNAT (or ALLOWED_SUBNETS)
    - cloudflare: allow only local/private proxies + optional Access JWT verification
    """
    d = decide_remote_access(request)
    if not d.allowed:
        logger.warning(
            "remote_access_denied",
            mode=d.mode,
            peer_ip=d.raw_peer_ip,
            client_ip=d.client_ip,
            via_proxy=bool(d.via_proxy),
            reason=d.reason,
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden", "reason": d.reason, "mode": d.mode},
        )

    resp = await call_next(request)
    # Useful for debugging tunnels without exposing secrets.
    resp.headers.setdefault("x-remote-access-mode", d.mode)
    return resp


def log_remote_access_boot_summary() -> None:
    s = get_settings()
    mode = str(getattr(s, "remote_access_mode", "off") or "off").strip().lower()
    allowed = _parse_networks(str(getattr(s, "allowed_subnets", "") or ""))
    if not allowed:
        allowed = _default_allowed_subnets_for_mode(mode)
    trusted = _parse_networks(str(getattr(s, "trusted_proxy_subnets", "") or ""))
    if not trusted:
        trusted = _default_trusted_proxy_subnets()
    logger.info(
        "remote_access_mode",
        mode=mode,
        trust_proxy_headers=bool(getattr(s, "trust_proxy_headers", False)),
        allowed_subnets=[str(n) for n in allowed],
        trusted_proxy_subnets=[str(n) for n in trusted],
        cloudflare_access_configured=bool(
            getattr(s, "cloudflare_access_team_domain", None)
            and getattr(s, "cloudflare_access_aud", None)
        ),
    )

