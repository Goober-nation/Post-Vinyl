"""Live login -> session -> logout through the app, against the test Navidrome (#88).

set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_app_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.navidrome import NavidromeClient
from app.config import Settings
from app.main import create_app
from app.services.auth import AuthService
from app.services.encryption import SecretBox, generate_key
from app.stores.database import Database

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")
ORIGIN = "https://testserver"


def test_live_login_session_logout(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    auth = AuthService(db, NavidromeClient(NAVIDROME_URL), SecretBox(generate_key()))
    app = create_app(Settings(str(tmp_path / "pv.db"), NAVIDROME_URL, ORIGIN), auth)
    client = TestClient(app, base_url=ORIGIN)
    creds = {
        "username": os.environ["NAVIDROME_USERNAME"],
        "password": os.environ["NAVIDROME_PASSWORD"],
    }
    same = {"Origin": ORIGIN}

    wrong = {**creds, "password": creds["password"] + "-wrong"}
    assert client.post("/api/auth/login", json=wrong, headers=same).status_code == 401
    r = client.post("/api/auth/login", json=creds, headers=same)
    assert r.status_code == 200, r.text
    assert (
        client.get("/api/auth/me").json()["username"].lower()
        == creds["username"].lower()
    )
    assert client.post("/api/auth/logout", headers=same).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
