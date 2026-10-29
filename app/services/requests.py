"""Requests: what a user asked for, whatever started it (#96).

Every create is a new row, even for a song the user already asked for (user,
2026-10-29); search and download deal with doubles. Owns ``track_state``
``requested`` / ``dropped`` / ``reRequested``; each change goes through
``core/states.py`` and is written only if no other service moved the state
meanwhile. A user only sees and changes their own requests.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.core import states
from app.stores.database import Database
from app.stores.requests import Request, RequestsStore
from app.stores.songs import SongsStore

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100  # user, 2026-10-29
MAX_PAGE = 1_000_000  # keeps the SQL offset inside SQLite's integer range
# where a request can come from besides the Soulseek tab (record_slskd_query)
REQUEST_SOURCES = frozenset({"catalog", "rec", "yandex"})


class RequestNotFound(LookupError):
    """No such request for this user."""


class SongNotFound(LookupError):
    """No such song."""


class StateChanged(Exception):
    """Another service changed the request's state meanwhile; reload and retry."""


@dataclass(frozen=True)
class Page:
    items: list[Request]
    total: int
    page: int
    page_size: int


def clamp_page(page: int, page_size: int) -> tuple[int, int]:
    return min(max(page, 1), MAX_PAGE), min(max(page_size, 1), MAX_PAGE_SIZE)


class RequestServiceBase(ABC):
    @abstractmethod
    def create(
        self, user_id: int, song_id: int, source: str, origin_id: int | None = None
    ) -> Request:
        """A request for a song (catalog pick, rec, playlist, …): ``requested``."""

    @abstractmethod
    def record_slskd_query(self, user_id: int, query: str) -> Request:
        """A typed Soulseek-tab search; a repeat (any case) bumps the old row."""

    @abstractmethod
    def get(self, user_id: int, request_id: int) -> Request: ...

    @abstractmethod
    def drop(self, user_id: int, request_id: int) -> Request: ...

    @abstractmethod
    def re_request(self, user_id: int, request_id: int) -> Request: ...

    @abstractmethod
    def history(
        self, user_id: int, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page:
        """Most recently used first; ``page`` starts at 1."""


class RequestService(RequestServiceBase):
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._requests = RequestsStore()
        self._songs = SongsStore()

    def create(
        self, user_id: int, song_id: int, source: str, origin_id: int | None = None
    ) -> Request:
        if source not in REQUEST_SOURCES:
            raise ValueError(f"unknown request source {source!r}")
        with self._db.transaction(write=True) as conn:
            if self._songs.get(conn, song_id) is None:
                raise SongNotFound(f"song {song_id}")
            # someone else's origin is reported like a missing one
            if origin_id is not None and (
                self._requests.origin_user(conn, origin_id) != user_id
            ):
                raise RequestNotFound(f"origin {origin_id}")
            return self._requests.create(
                conn,
                user_id,
                source,
                int(self._clock()),
                song_id=song_id,
                origin_id=origin_id,
            )

    def record_slskd_query(self, user_id: int, query: str) -> Request:
        query = " ".join(query.split())
        if not query:
            raise ValueError("empty search")
        with self._db.transaction(write=True) as conn:
            return self._requests.record_slskd_query(
                conn, user_id, query, int(self._clock())
            )

    def get(self, user_id: int, request_id: int) -> Request:
        with self._db.transaction() as conn:
            req = self._requests.get(conn, request_id)
        if req is None or req.user_id != user_id:
            raise RequestNotFound(f"request {request_id}")
        return req

    def _move(
        self, user_id: int, request_id: int, transition: Callable[[str], str]
    ) -> Request:
        with self._db.transaction(write=True) as conn:
            req = self._requests.get(conn, request_id)
            if req is None or req.user_id != user_id:
                raise RequestNotFound(f"request {request_id}")
            new = transition(req.track_state)  # raises IllegalTransition
            if not self._requests.set_track_state(
                conn, request_id, new, expected=req.track_state
            ):
                raise StateChanged(f"request {request_id} changed meanwhile")
            moved = self._requests.get(conn, request_id)
        assert moved is not None
        return moved

    def drop(self, user_id: int, request_id: int) -> Request:
        return self._move(user_id, request_id, states.drop)

    def re_request(self, user_id: int, request_id: int) -> Request:
        return self._move(user_id, request_id, states.re_request)

    def history(
        self, user_id: int, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page:
        page, page_size = clamp_page(page, page_size)
        with self._db.transaction() as conn:
            items = self._requests.history(conn, user_id, page, page_size)
            total = self._requests.count(conn, user_id)
        return Page(items, total, page, page_size)
