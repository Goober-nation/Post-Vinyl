"""App skeleton (#88): default deny, CSRF, cookie flags, login/logout."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import ConfigError, Settings
from app.main import Core, create_app
from app.middleware import SESSION_COOKIE
from app.services.auth import MAX_FAILED_LOGINS, AuthService
from app.services.catalog_fake import FakeCatalogService
from app.services.dispatch_fake import FakeDownloadDispatch
from app.services.encryption import SecretBox, generate_key
from app.services.requests_fake import FakeRequestService
from app.stores.database import Database
from app.workers.core_loop import CoreLoopWorkers

ORIGIN = "https://testserver"
SAME = {"Origin": ORIGIN}


def client_for(tmp_path: Path, dev: bool = False) -> TestClient:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    nd = FakeNavidromeClient({"alice": ("pw", True)})
    auth = AuthService(db, nd, SecretBox(generate_key()))
    settings = Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN, dev)
    return TestClient(create_app(settings, auth), base_url=ORIGIN)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return client_for(tmp_path)


def login(client: TestClient) -> None:
    r = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw"}, headers=SAME
    )
    assert r.status_code == 200, r.text


def test_default_deny_and_open_routes(client: TestClient) -> None:
    assert client.get("/api/system/ping").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/no/such/route").status_code == 401  # deny before 404
    assert client.post("/api/auth/logout", headers=SAME).status_code == 401
    client.cookies.set(SESSION_COOKIE, "forged")
    assert client.get("/api/auth/me").status_code == 401


def test_login_sets_strict_cookie_and_logout_ends_it(client: TestClient) -> None:
    r = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pw"}, headers=SAME
    )
    assert r.json() == {"username": "alice", "is_admin": True}
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=lax", "max-age=2592000", "path=/"):
        assert flag in cookie
    assert client.get("/api/auth/me").json()["username"] == "alice"
    assert client.post("/api/auth/logout", headers=SAME).status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_login_errors_map_to_status(client: TestClient) -> None:
    bad = {"username": "alice", "password": "x"}
    for _ in range(MAX_FAILED_LOGINS):
        assert client.post("/api/auth/login", json=bad, headers=SAME).status_code == 401
    r = client.post("/api/auth/login", json=bad, headers=SAME)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example"},
        {"Referer": "https://evil.example/x"},
        {"Origin": "http://testserver"},
    ],
)
def test_csrf_refuses_foreign_or_missing_origin(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = {"username": "alice", "password": "pw"}
    assert client.post("/api/auth/login", json=body, headers=headers).status_code == 403
    login(client)
    assert client.post("/api/auth/logout", headers=headers).status_code == 403
    assert client.get("/api/auth/me").status_code == 200  # still logged in


def test_referer_is_the_fallback(client: TestClient) -> None:
    body = {"username": "alice", "password": "pw"}
    r = client.post(
        "/api/auth/login", json=body, headers={"Referer": f"{ORIGIN}/login"}
    )
    assert r.status_code == 200


def test_docs_only_in_dev(tmp_path: Path) -> None:
    assert client_for(tmp_path).get("/docs").status_code == 401
    (tmp_path / "dev").mkdir()
    assert client_for(tmp_path / "dev", dev=True).get("/docs").status_code == 200


def test_settings_refuse_missing_or_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_DB_PATH", "pv.db")
    monkeypatch.setenv("PV_NAVIDROME_URL", "http://nd:4533")
    monkeypatch.setenv("PV_PUBLIC_ORIGIN", "https://music.example/")
    monkeypatch.setenv("PV_CONFIG_PATH", "config/post-vinyl.example.yaml")
    monkeypatch.setenv("PV_SLSKD_URL", "http://slskd:5030")
    monkeypatch.setenv("PV_SLSKD_API_KEY", "key")
    assert Settings.from_env().public_origin == "https://music.example"
    monkeypatch.setenv("PV_PUBLIC_ORIGIN", "https://music.example/app")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("PV_PUBLIC_ORIGIN", "https://music.example")
    monkeypatch.delenv("PV_NAVIDROME_URL")
    with pytest.raises(ConfigError, match="PV_NAVIDROME_URL"):
        Settings.from_env()


class RecordingWorkers(CoreLoopWorkers):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


def test_lifespan_starts_and_stops_core_workers(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    auth = AuthService(db, FakeNavidromeClient({}), SecretBox(generate_key()))
    workers = RecordingWorkers()
    core = Core(
        FakeCatalogService(),
        FakeRequestService(),
        FakeDownloadDispatch(),
        workers=workers,
    )
    app = create_app(Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN), auth, core)
    with TestClient(app, base_url=ORIGIN):
        assert workers.calls == ["start"]
    assert workers.calls == ["start", "stop"]


def test_web_files_open_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<p>login</p>")
    (dist / "assets" / "app.js").write_text("1")
    monkeypatch.setattr("app.main.WEB_DIST", dist)
    client = client_for(tmp_path)
    assert client.get("/").text == "<p>login</p>"
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/index.html").status_code == 401  # only / and /assets/
    assert client.get("/api/auth/me").status_code == 401
    login(client)
    assert client.get("/api/auth/me").status_code == 200  # the API still wins
