from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dubbing_pipeline.api.models import Role, User, now_ts
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security import quotas
from tests._helpers.redis import redis_available, redis_client, redis_prefix


class DummyStore:
    def __init__(self) -> None:
        self._storage: dict[str, int] = {}

    def list(self, *, limit: int = 2000, state: str | None = None):
        _ = (limit, state)
        return []

    def get_user_quota(self, user_id: str):
        _ = user_id
        return {}

    def get_user_storage_bytes(self, user_id: str) -> int:
        return int(self._storage.get(str(user_id), 0))

    def set_user_storage_bytes(self, user_id: str, bytes_count: int) -> None:
        self._storage[str(user_id)] = int(bytes_count)


def _dummy_request(store: DummyStore):
    state = SimpleNamespace(job_store=store, queue_backend=None)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app)


def _make_user(user_id: str) -> User:
    return User(
        id=user_id,
        username=f"user_{user_id}",
        password_hash="x",
        role=Role.viewer,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


def _reset_quota_state() -> None:
    quotas._LOCAL_DAILY_RESERVATIONS.clear()
    quotas._LOCAL_PENDING_STORAGE.clear()
    quotas._REDIS_CLIENT = None


def test_job_reservation_release_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_quota_state()
    monkeypatch.setenv("JOBS_PER_DAY_PER_USER", "5")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()

    store = DummyStore()
    user = _make_user("u_local_jobs")
    enforcer = quotas.QuotaEnforcer.from_request(request=_dummy_request(store), user=user)

    reservation = asyncio.run(enforcer.reserve_daily_jobs(count=2, action="test"))
    day = quotas._utc_day_key()
    key = (day, str(user.id))
    assert quotas._LOCAL_DAILY_RESERVATIONS.get(key) == 2

    asyncio.run(reservation.release())
    assert quotas._LOCAL_DAILY_RESERVATIONS.get(key, 0) == 0

    asyncio.run(reservation.release())
    assert quotas._LOCAL_DAILY_RESERVATIONS.get(key, 0) == 0
    assert reservation.released is True


def test_storage_reservation_release_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_quota_state()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1000")
    monkeypatch.setenv("MAX_STORAGE_BYTES_PER_USER", "1000")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()

    store = DummyStore()
    user = _make_user("u_local_storage")
    enforcer = quotas.QuotaEnforcer.from_request(request=_dummy_request(store), user=user)

    reservation = asyncio.run(enforcer.reserve_storage_bytes(bytes_count=200, action="test"))
    assert quotas._LOCAL_PENDING_STORAGE.get(str(user.id)) == 200

    asyncio.run(reservation.release())
    assert quotas._LOCAL_PENDING_STORAGE.get(str(user.id), 0) == 0

    asyncio.run(reservation.release())
    assert quotas._LOCAL_PENDING_STORAGE.get(str(user.id), 0) == 0
    assert reservation.released is True


@pytest.mark.skipif(not redis_available(), reason="redis not available")
def test_job_reservation_release_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_quota_state()
    monkeypatch.setenv("JOBS_PER_DAY_PER_USER", "5")
    get_settings.cache_clear()

    client = redis_client()
    assert client is not None

    store = DummyStore()
    user = _make_user("u_redis_jobs")
    enforcer = quotas.QuotaEnforcer.from_request(request=_dummy_request(store), user=user)

    day = quotas._utc_day_key()
    key = f"{redis_prefix()}:quota:daily:{day}:{user.id}"
    client.delete(key)

    reservation = asyncio.run(enforcer.reserve_daily_jobs(count=2, action="test"))
    assert int(client.get(key) or 0) == 2

    asyncio.run(reservation.release())
    assert int(client.get(key) or 0) == 0

    asyncio.run(reservation.release())
    assert int(client.get(key) or 0) == 0
    client.delete(key)
