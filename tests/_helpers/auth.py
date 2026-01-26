from __future__ import annotations

from fastapi.testclient import TestClient


def login_user(
    client: TestClient, *, username: str, password: str, clear_cookies: bool = False
) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    if clear_cookies:
        # Avoid cookie-based CSRF enforcement when using Bearer tokens.
        client.cookies.clear()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def login_admin(
    client: TestClient, *, username: str = "admin", password: str = "adminpass"
) -> dict[str, str]:
    return login_user(client, username=username, password=password)
