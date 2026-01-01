from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from anime_v2.utils.log import logger


def random_id(prefix: str = "", n: int = 24) -> str:
    # URL-safe token without padding, deterministic length-ish.
    tok = secrets.token_urlsafe(n)
    return f"{prefix}{tok}"


def random_prefix(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


@dataclass(frozen=True, slots=True)
class PasswordHasher:
    """
    Thin wrapper around argon2-cffi PasswordHasher.
    """

    time_cost: int = 2
    memory_cost: int = 102400
    parallelism: int = 8

    def _impl(self):
        try:
            from argon2 import PasswordHasher as _PH  # type: ignore
        except Exception as ex:  # pragma: no cover
            raise RuntimeError("argon2-cffi not installed") from ex
        return _PH(time_cost=self.time_cost, memory_cost=self.memory_cost, parallelism=self.parallelism)

    def hash(self, password: str) -> str:
        return self._impl().hash(password)

    def verify(self, hashed: str, password: str) -> bool:
        try:
            return bool(self._impl().verify(hashed, password))
        except Exception:
            return False


def hash_secret(secret: str) -> str:
    return PasswordHasher().hash(secret)


def verify_secret(hashed: str, secret: str) -> bool:
    return PasswordHasher().verify(hashed, secret)

