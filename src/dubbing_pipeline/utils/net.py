from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from dubbing_pipeline.config import get_settings


class EgressDenied(RuntimeError):
    pass


def _is_local_host(host: str) -> bool:
    h = (host or "").lower()
    return h in {"localhost", "127.0.0.1", "::1"} or h.startswith("127.")


def _is_hf_host(host: str) -> bool:
    h = (host or "").lower()
    return (
        h == "huggingface.co"
        or h.endswith(".huggingface.co")
        or h == "hf.co"
        or h.endswith(".hf.co")
    )


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allow_egress: bool
    allow_hf: bool


_installed = False
_orig_create_connection: Callable | None = None


def install_egress_policy() -> None:
    """
    Global kill-switch for outbound connections.

    - OFFLINE_MODE=1 => deny all egress except localhost
    - ALLOW_EGRESS=0 => deny all egress except localhost (and optionally HF if ALLOW_HF_EGRESS=1)
    """
    global _installed, _orig_create_connection
    if _installed:
        return
    s = get_settings()
    allow_egress = bool(s.allow_egress) and not bool(s.offline_mode)
    allow_hf = bool(s.allow_hf_egress) and not bool(s.offline_mode)
    policy = EgressPolicy(allow_egress=allow_egress, allow_hf=allow_hf)

    _orig_create_connection = socket.create_connection

    def guarded_create_connection(address, *args, **kwargs):
        # address can be (host, port) or str
        host = ""
        if isinstance(address, tuple) and address:
            host = str(address[0] or "")
        elif isinstance(address, str):
            host = address

        if _is_local_host(host):
            return _orig_create_connection(address, *args, **kwargs)  # type: ignore[misc]
        if policy.allow_hf and _is_hf_host(host):
            return _orig_create_connection(address, *args, **kwargs)  # type: ignore[misc]
        if policy.allow_egress:
            return _orig_create_connection(address, *args, **kwargs)  # type: ignore[misc]

        raise EgressDenied(
            "Outbound network access is disabled. "
            "Set ALLOW_EGRESS=1 to enable, or (if you only need Hugging Face downloads) set ALLOW_HF_EGRESS=1. "
            "If OFFLINE_MODE=1, pre-download models into caches and disable any download steps."
        )

    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    _installed = True


@contextmanager
def egress_guard() -> Iterator[None]:
    """
    Convenience context. Installs global policy if not already installed.
    """
    install_egress_policy()
    yield


def _split_list(spec: str) -> list[str]:
    s = (spec or "").strip()
    if not s:
        return []
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
            continue
    return nets


def trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    s = get_settings()
    spec = str(getattr(s, "trusted_proxy_subnets", "") or "")
    return _parse_networks(spec)


def is_trusted_proxy(peer_ip: str) -> bool:
    ip = None
    try:
        ip = ipaddress.ip_address((peer_ip or "").strip())
    except Exception:
        return False
    nets = trusted_proxy_networks()
    if not nets:
        return False
    for n in nets:
        try:
            if ip in n:
                return True
        except Exception:
            continue
    return False


def _forwarded_ip_candidates(headers: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    cf = headers.get("cf-connecting-ip")
    if cf:
        out.append(str(cf).strip())
    xff = headers.get("x-forwarded-for")
    if xff:
        for part in str(xff).split(","):
            tok = part.strip()
            if tok:
                out.append(tok)
    xr = headers.get("x-real-ip")
    if xr:
        out.append(str(xr).strip())
    return out


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    try:
        return bool(getattr(ip, "is_global", False))
    except Exception:
        return False


def get_client_ip_from_headers(*, peer_ip: str, headers: Mapping[str, Any]) -> str:
    """
    Return the effective client IP using trusted proxy configuration.
    """
    peer = (peer_ip or "").strip() or "unknown"
    s = get_settings()
    trust_proxy_headers = bool(getattr(s, "trust_proxy_headers", False))
    trust_for_tests = bool(int(os.environ.get("TRUST_PROXY_HEADERS_FOR_TESTS", "0") or "0"))
    if not trust_proxy_headers and not trust_for_tests:
        return peer
    if not is_trusted_proxy(peer):
        if not (trust_for_tests and (peer == "testclient" or _is_local_host(peer))):
            return peer
    cands = _forwarded_ip_candidates(headers)
    first_valid: str | None = None
    for raw in cands:
        try:
            ip = ipaddress.ip_address(raw)
        except Exception:
            continue
        if first_valid is None:
            first_valid = str(ip)
        if _is_public_ip(ip):
            return str(ip)
    return first_valid or peer


def get_client_ip(request: Any) -> str:
    """
    Canonical client IP extractor.
    """
    peer = ""
    try:
        if getattr(request, "client", None) and getattr(request.client, "host", None):
            peer = str(request.client.host)
    except Exception:
        peer = ""
    headers = getattr(request, "headers", {}) or {}
    return get_client_ip_from_headers(peer_ip=peer, headers=headers)
