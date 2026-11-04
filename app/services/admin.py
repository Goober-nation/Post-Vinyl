"""Admin views (#112): bad slskd peers and clearing them, the user list.

Navidrome owns the accounts; this shows what Post-Vinyl keeps per user, never
a secret, and creates accounts there. Admin-only, enforced by the routes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.clients.navidrome import (
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeDuplicate,
    NavidromeError,
    NavidromeNotFound,
    Session,
)
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.slskd import SlskdPeer, SlskdStore
from app.stores.users import User, UsersStore


class UsernameTaken(Exception):
    """Navidrome already has an account with this name."""


class AdminLoginStale(Exception):
    """The admin's stored Navidrome password no longer works."""


class UserNotCreated(Exception):
    """Navidrome couldn't make the change."""


class UserNotFound(Exception):
    """No such Post-Vinyl user, or no such Navidrome account."""


class CannotDeleteSelf(Exception):
    """An admin can't delete their own account."""


@dataclass(frozen=True)
class UserView:
    id: int
    username: str
    is_admin: bool
    status: str  # active | needs_reauth
    has_listenbrainz_token: bool
    created_at: int


def user_view(u: User) -> UserView:
    return UserView(
        u.id,
        u.username,
        bool(u.is_admin),
        u.status,
        u.lb_token_enc is not None,
        u.created_at,
    )


class AdminServiceBase(ABC):
    @abstractmethod
    def peers(self) -> list[SlskdPeer]:
        """Every peer with a bad record; ``blocked_at`` set = banned."""

    @abstractmethod
    def clear_peer(self, username: str) -> bool: ...

    @abstractmethod
    def clear_peers(self) -> int: ...

    @abstractmethod
    def users(self) -> list[UserView]: ...

    @abstractmethod
    def create_user(
        self, admin: User, username: str, password: str, is_admin: bool
    ) -> UserView:
        """Create the Navidrome account and the matching Post-Vinyl user."""

    @abstractmethod
    def set_password(self, admin: User, user_id: int, password: str) -> None:
        """Change the password in Navidrome and the saved copy here."""

    @abstractmethod
    def delete_user(self, admin: User, user_id: int) -> None:
        """Delete the account; the user's request history stays, hidden."""


class AdminService(AdminServiceBase):
    def __init__(
        self,
        db: Database,
        navidrome: NavidromeClientBase,
        box: SecretBox,
        stop_user: Callable[[int], None] = lambda user_id: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._navidrome = navidrome
        self._box = box
        self._stop_user = stop_user
        self._clock = clock
        self._slskd = SlskdStore()
        self._users = UsersStore()

    def peers(self) -> list[SlskdPeer]:
        with self._db.transaction() as conn:
            return self._slskd.peers(conn)

    def clear_peer(self, username: str) -> bool:
        with self._db.transaction(write=True) as conn:
            known = self._slskd.get_peer(conn, username) is not None
            self._slskd.clear_peer(conn, username)
        return known

    def clear_peers(self) -> int:
        with self._db.transaction(write=True) as conn:
            return self._slskd.clear_peers(conn)

    def users(self) -> list[UserView]:
        with self._db.transaction() as conn:
            found = self._users.all(conn)
        return [user_view(u) for u in found]

    def create_user(
        self, admin: User, username: str, password: str, is_admin: bool
    ) -> UserView:
        session = self._session(admin)
        try:
            nd_id = self._navidrome.create_user(session, username, password, is_admin)
        except NavidromeDuplicate as e:
            raise UsernameTaken(f"{username} already exists in Navidrome") from e
        except NavidromeError as e:
            raise UserNotCreated(f"Navidrome can't create the user: {e}") from e
        with self._db.transaction(write=True) as conn:
            user = self._users.upsert_on_login(
                conn,
                username,
                self._box.encrypt(password),
                is_admin,
                int(self._clock()),
                nd_id,
            )
        return user_view(user)

    def set_password(self, admin: User, user_id: int, password: str) -> None:
        user = self._target(user_id)
        session = self._session(admin)
        own = None
        if user.id == admin.id:
            own = self._box.decrypt(admin.nd_password_enc)
        try:
            self._navidrome.set_password(
                session, user.nd_id, user.username, password, own
            )
        except NavidromeNotFound as e:
            raise UserNotFound(f"{user.username} isn't in Navidrome") from e
        except NavidromeError as e:
            raise UserNotCreated(f"Navidrome can't change it: {e}") from e
        with self._db.transaction(write=True) as conn:
            self._users.set_password(conn, user.id, self._box.encrypt(password))

    def delete_user(self, admin: User, user_id: int) -> None:
        if user_id == admin.id:
            raise CannotDeleteSelf("you can't delete your own account")
        user = self._target(user_id)
        session = self._session(admin)
        try:
            self._navidrome.delete_user(session, user.nd_id, user.username)
        except NavidromeNotFound:
            pass  # already removed in Navidrome: finish here
        except NavidromeError as e:
            raise UserNotCreated(f"Navidrome can't delete the user: {e}") from e
        self._stop_user(user.id)
        with self._db.transaction(write=True) as conn:
            self._users.mark_deleted(conn, user.id, int(self._clock()))

    def _target(self, user_id: int) -> User:
        with self._db.transaction() as conn:
            user = self._users.get(conn, user_id)
        if user is None or user.deleted_at is not None:
            raise UserNotFound(f"user {user_id}")
        return user

    def _session(self, admin: User) -> Session:
        try:
            return self._navidrome.login(
                admin.username, self._box.decrypt(admin.nd_password_enc)
            )
        except NavidromeAuthError as e:
            raise AdminLoginStale(
                "Navidrome refused your saved login; log in again"
            ) from e
        except NavidromeError as e:
            raise UserNotCreated(f"Navidrome can't be reached: {e}") from e
