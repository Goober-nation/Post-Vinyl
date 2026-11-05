"""A user's Navidrome session from their saved login, one way for every service."""

from __future__ import annotations

import logging

from app.clients.navidrome import NavidromeAuthError, NavidromeClientBase, Session
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.users import User, UsersStore

log = logging.getLogger(__name__)


class NavidromeLogins:
    def __init__(
        self, db: Database, navidrome: NavidromeClientBase, box: SecretBox
    ) -> None:
        self._db = db
        self._nd = navidrome
        self._box = box
        self._users = UsersStore()

    def session_for(self, user: User) -> Session | None:
        """None until the user logs in again: a refused login is marked once."""
        if user.status != "active":
            return None
        try:
            return self._nd.login(
                user.username, self._box.decrypt(user.nd_password_enc)
            )
        except NavidromeAuthError:
            log.warning("Navidrome refused the saved login of %s", user.username)
            with self._db.transaction(write=True) as conn:
                self._users.mark_login_refused(conn, user.id)
            return None
