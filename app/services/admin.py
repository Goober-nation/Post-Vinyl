"""Admin views (#112): bad slskd peers and clearing them, the user list.

Navidrome owns the accounts; this only shows what Post-Vinyl keeps per user,
never a secret. Admin-only, enforced by the routes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stores.database import Database
from app.stores.slskd import SlskdPeer, SlskdStore
from app.stores.users import UsersStore


@dataclass(frozen=True)
class UserView:
    id: int
    username: str
    is_admin: bool
    status: str  # active | needs_reauth
    has_listenbrainz_token: bool
    created_at: int


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


class AdminService(AdminServiceBase):
    def __init__(self, db: Database) -> None:
        self._db = db
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
        return [
            UserView(
                u.id,
                u.username,
                bool(u.is_admin),
                u.status,
                u.lb_token_enc is not None,
                u.created_at,
            )
            for u in found
        ]
