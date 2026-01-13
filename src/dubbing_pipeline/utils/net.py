from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

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
