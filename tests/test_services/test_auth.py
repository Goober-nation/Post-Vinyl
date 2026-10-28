"""Auth service (#87) with the fake Navidrome and a real SQLite file."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.navidrome import NavidromeError
from app.clients.navidrome_fake import FakeNavidromeClient
from app.services.auth import (
    FAILED_LOGIN_WINDOW_S,
    MAX_FAILED_LOGINS,
    SESSION_LIFETIME_S,
    AuthService,
    InvalidCredentials,
    LoginLocked,
    NavidromeUnavailable,
    hash_token,
)
from app.services.encryption import SecretBox, generate_key
from app.stores.database import Database
from app.stores.users import UsersStore

NOW = 1_800_000_000


class Clock:
    def __init__(self) -> None:
        self.t = float(NOW)

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup(
    tmp_path: Path,
) -> tuple[AuthService, Database, FakeNavidromeClient, Clock, SecretBox]:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    nd = FakeNavidromeClient({"alice": ("pw", True), "bob": ("pw2", False)})
    box, clock = SecretBox(generate_key()), Clock()
    return AuthService(db, nd, box, clock), db, nd, clock, box


Setup = tuple[AuthService, Database, FakeNavidromeClient, Clock, SecretBox]


def test_first_login_creates_user_and_later_login_refreshes(setup: Setup) -> None:
    auth, _, nd, _, box = setup
    first = auth.login("alice", "pw")
    assert first.user.is_admin == 1 and box.decrypt(first.user.nd_password_enc) == "pw"
    nd.users["alice"] = ("new", False)  # changed in Navidrome
    again = auth.login("alice", "new")
    assert again.user.id == first.user.id and again.user.is_admin == 0
    assert box.decrypt(again.user.nd_password_enc) == "new"
    assert auth.authenticate(first.token) == auth.authenticate(again.token)


def test_token_is_stored_only_hashed(setup: Setup) -> None:
    auth, db, *_ = setup
    token = auth.login("alice", "pw").token
    with db.transaction() as c:
        stored = [r[0] for r in c.execute("SELECT token_hash FROM sessions")]
    assert stored == [hash_token(token)] and token not in stored
    assert auth.authenticate(stored[0]) is None  # the hash itself isn't a token


def test_wrong_password_and_unknown_user_are_the_same_error(setup: Setup) -> None:
    auth, *_ = setup
    with pytest.raises(InvalidCredentials) as a:
        auth.login("alice", "nope")
    with pytest.raises(InvalidCredentials) as b:
        auth.login("nobody", "pw")
    assert str(a.value) == str(b.value)


def test_failure_cap_locks_everyone_until_window_ends(setup: Setup) -> None:
    auth, _, _, clock, _ = setup
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(InvalidCredentials):
            auth.login("mallory", "x")
    clock.t += 600
    with pytest.raises(LoginLocked) as locked:
        auth.login("bob", "pw2")  # right password, still locked
    assert locked.value.retry_after_s == FAILED_LOGIN_WINDOW_S - 600
    clock.t = NOW + FAILED_LOGIN_WINDOW_S
    assert auth.login("bob", "pw2").user.username == "bob"


def test_success_does_not_reset_the_count(setup: Setup) -> None:
    auth, *_ = setup
    for _ in range(MAX_FAILED_LOGINS - 1):
        with pytest.raises(InvalidCredentials):
            auth.login("alice", "x")
    auth.login("alice", "pw")
    with pytest.raises(InvalidCredentials):
        auth.login("alice", "x")
    with pytest.raises(LoginLocked):
        auth.login("alice", "pw")


def test_navidrome_down_fails_clearly_and_is_not_counted(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, _, nd, _, _ = setup

    def down(username: str, password: str) -> None:
        raise NavidromeError("POST /auth/login: connection refused")

    monkeypatch.setattr(nd, "login", down)
    for _ in range(MAX_FAILED_LOGINS + 1):
        with pytest.raises(NavidromeUnavailable):
            auth.login("alice", "pw")
    monkeypatch.undo()
    assert auth.login("alice", "pw")


def test_sliding_expiry_touches_at_most_hourly(setup: Setup) -> None:
    auth, db, _, clock, _ = setup
    token = auth.login("alice", "pw").token
    users = UsersStore()

    def expires() -> int:
        with db.transaction() as c:
            s = users.get_session(c, hash_token(token))
            assert s
            return s.expires_at

    clock.t += 1800
    assert auth.authenticate(token) and expires() == NOW + SESSION_LIFETIME_S
    clock.t += 1800
    assert auth.authenticate(token) and expires() == NOW + 3600 + SESSION_LIFETIME_S
    clock.t = expires() - 1
    assert auth.authenticate(token)  # used just before expiry: pushed forward
    clock.t += SESSION_LIFETIME_S + 1
    assert auth.authenticate(token) is None
    with db.transaction() as c:
        assert users.get_session(c, hash_token(token)) is None  # expired row removed


def test_logout_ends_only_that_session(setup: Setup) -> None:
    auth, *_ = setup
    a, b = auth.login("alice", "pw").token, auth.login("alice", "pw").token
    auth.logout(a)
    auth.logout("not-a-token")
    assert auth.authenticate(a) is None and auth.authenticate(b)


def test_lockout_does_not_end_running_sessions(setup: Setup) -> None:
    auth, *_ = setup
    token = auth.login("alice", "pw").token
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(InvalidCredentials):
            auth.login("mallory", "x")
    with pytest.raises(LoginLocked):
        auth.login("alice", "pw")
    assert auth.authenticate(token)  # already logged in: keeps working
