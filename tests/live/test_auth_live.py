"""Live login against the test Navidrome (#87).

set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_auth_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.clients.navidrome import NavidromeClient
from app.services.auth import AuthService, InvalidCredentials
from app.services.encryption import SecretBox, generate_key
from app.stores.database import Database

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")


def test_live_login_session_logout(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    auth = AuthService(db, NavidromeClient(NAVIDROME_URL), box)
    username, password = (
        os.environ["NAVIDROME_USERNAME"],
        os.environ["NAVIDROME_PASSWORD"],
    )

    result = auth.login(username, password)
    assert result.user.username.lower() == username.lower()
    assert box.decrypt(result.user.nd_password_enc) == password
    assert result.navidrome.jwt and result.navidrome.subsonic_token
    assert auth.authenticate(result.token) == result.user

    with pytest.raises(InvalidCredentials):
        auth.login(username, password + "-wrong")

    auth.logout(result.token)
    assert auth.authenticate(result.token) is None
