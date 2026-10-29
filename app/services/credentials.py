"""A user's own service credentials (#112): write-only, never read back.

Only the ListenBrainz token for now (Yandex, Last.fm later). A token is checked
with ListenBrainz before it's stored (user, 2026-10-30): an invalid one is
refused and the old one kept; ListenBrainz unreachable refuses too. Checking
is a request to ListenBrainz, so it needs ListenBrainz on for the user.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.clients.listenbrainz import ListenBrainzClientBase, ListenBrainzError
from app.services.encryption import SecretBox
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.users import UsersStore

log = logging.getLogger(__name__)


class CredentialCheckUnavailable(Exception):
    """The service that checks the credential can't be reached; nothing stored."""


@dataclass(frozen=True)
class CredentialsView:
    listenbrainz: bool  # a token is stored
    listenbrainz_user: str | None = None  # set right after a successful check


class CredentialsServiceBase(ABC):
    @abstractmethod
    def view(self, user_id: int) -> CredentialsView: ...

    @abstractmethod
    def set_listenbrainz(self, user_id: int, token: str | None) -> CredentialsView:
        """Check and store the token; ``None`` removes it."""


class CredentialsService(CredentialsServiceBase):
    def __init__(
        self,
        db: Database,
        listenbrainz: ListenBrainzClientBase,
        box: SecretBox,
        sources: SourcePreferences,
    ) -> None:
        self._db = db
        self._lb = listenbrainz
        self._box = box
        self._sources = sources
        self._users = UsersStore()

    def view(self, user_id: int) -> CredentialsView:
        with self._db.transaction() as conn:
            user = self._users.get(conn, user_id)
        return CredentialsView(listenbrainz=bool(user and user.lb_token_enc))

    def set_listenbrainz(self, user_id: int, token: str | None) -> CredentialsView:
        if token is None:
            with self._db.transaction(write=True) as conn:
                self._users.set_lb_token(conn, user_id, None)
            return CredentialsView(listenbrainz=False)
        token = token.strip()
        if not token:
            raise ValueError("empty token")
        self._sources.require(user_id, "catalog", "listenbrainz")
        try:
            check = self._lb.validate_token(token)
        except ListenBrainzError as e:
            log.info("ListenBrainz token check failed: %s", e)
            raise CredentialCheckUnavailable(
                "ListenBrainz can't check the token now"
            ) from e
        if not check.valid:
            raise ValueError("ListenBrainz says this token is invalid")
        with self._db.transaction(write=True) as conn:
            self._users.set_lb_token(conn, user_id, self._box.encrypt(token))
        return CredentialsView(listenbrainz=True, listenbrainz_user=check.user_name)
