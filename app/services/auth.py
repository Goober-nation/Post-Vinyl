"""Login, sessions and the failed-login cap (#87).

Credentials are checked against Navidrome on every login; every Post-Vinyl
user is a Navidrome user, and their row is created on first login. Sessions
are a random cookie token of which only the SHA-256 is stored, with a 30-day
sliding expiry written at most once an hour.

Failed logins are capped **server-wide** (user, 2026-10-28): 20 failures in
an hour lock every login until that hour's window ends. Kept in memory; a
restart clears it. Anyone can trip it, which is accepted for this app.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.clients.navidrome import (
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeError,
)
from app.clients.navidrome import Session as NavidromeSession
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.users import User, UsersStore

SESSION_LIFETIME_S = 30 * 24 * 3600
SESSION_TOUCH_EVERY_S = 3600
MAX_FAILED_LOGINS = 20
FAILED_LOGIN_WINDOW_S = 3600


class AuthError(Exception):
    """Base for login failures the login route shows to the user."""


class InvalidCredentials(AuthError):
    """Wrong username or password (deliberately doesn't say which)."""


class LoginLocked(AuthError):
    """Too many failed logins; ``retry_after_s`` until the window resets."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"too many failed logins; try again in {retry_after_s} s")
        self.retry_after_s = retry_after_s


class NavidromeUnavailable(AuthError):
    """Navidrome couldn't be reached, so no password can be checked."""


@dataclass(frozen=True)
class LoginResult:
    token: str  # raw cookie value; never stored
    user: User
    navidrome: NavidromeSession


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthServiceBase(ABC):
    @abstractmethod
    def login(self, username: str, password: str) -> LoginResult: ...

    @abstractmethod
    def authenticate(self, token: str) -> User | None:
        """The session's user, or None for an unknown or expired token."""

    @abstractmethod
    def logout(self, token: str) -> None: ...


class AuthService(AuthServiceBase):
    def __init__(
        self,
        db: Database,
        navidrome: NavidromeClientBase,
        box: SecretBox,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._navidrome = navidrome
        self._box = box
        self._clock = clock
        self._users = UsersStore()
        self._lock = threading.Lock()
        self._window_start = 0
        self._failures = 0

    def _now(self) -> int:
        return int(self._clock())

    def _check_cap(self, now: int) -> None:
        with self._lock:
            if now - self._window_start >= FAILED_LOGIN_WINDOW_S:
                self._window_start, self._failures = now, 0
            if self._failures >= MAX_FAILED_LOGINS:
                raise LoginLocked(self._window_start + FAILED_LOGIN_WINDOW_S - now)

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1

    def login(self, username: str, password: str) -> LoginResult:
        now = self._now()
        self._check_cap(now)
        try:
            nd = self._navidrome.login(username, password)
        except NavidromeAuthError as e:
            self._record_failure()
            raise InvalidCredentials("wrong username or password") from e
        except NavidromeError as e:
            raise NavidromeUnavailable(f"Navidrome can't check the login: {e}") from e
        token = secrets.token_urlsafe(32)
        with self._db.transaction() as conn:
            user = self._users.upsert_on_login(
                conn, nd.username, self._box.encrypt(password), nd.is_admin, now
            )
            self._users.create_session(
                conn, user.id, hash_token(token), now, now + SESSION_LIFETIME_S
            )
        return LoginResult(token=token, user=user, navidrome=nd)

    def authenticate(self, token: str) -> User | None:
        now = self._now()
        with self._db.transaction() as conn:
            session = self._users.get_session(conn, hash_token(token))
            if session is None:
                return None
            if session.expires_at <= now:
                self._users.delete_session(conn, session.id)
                return None
            if now - session.last_seen_at >= SESSION_TOUCH_EVERY_S:
                self._users.touch_session(
                    conn, session.id, now, now + SESSION_LIFETIME_S
                )
            return self._users.get(conn, session.user_id)

    def logout(self, token: str) -> None:
        with self._db.transaction() as conn:
            session = self._users.get_session(conn, hash_token(token))
            if session:
                self._users.delete_session(conn, session.id)
