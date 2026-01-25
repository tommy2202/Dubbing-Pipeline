from __future__ import annotations

from fastapi.testclient import TestClient


def login_admin(
    client: TestClient, *, username: str = "admin", password: str = "adminpass"
) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}
