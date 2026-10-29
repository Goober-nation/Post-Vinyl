"""In-memory RequestService that behaves like the real one (#96).

Same rules as ``services/requests.py``: allowed sources, songs and origins must
exist (fill ``songs`` and ``origins``), an origin must be the user's, Soulseek
queries are whitespace-collapsed and a repeat bumps the old row, transitions
go through ``core/states.py``, history is newest-used first with the same
page clamping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.core import states
from app.services.requests import (
    DEFAULT_PAGE_SIZE,
    REQUEST_SOURCES,
    Page,
    RequestNotFound,
    RequestServiceBase,
    SongNotFound,
    clamp_page,
)
from app.stores.requests import Request


class FakeRequestService(RequestServiceBase):
    def __init__(self) -> None:
        self.requests: dict[int, Request] = {}
        self.songs: set[int] = set()  # song ids that exist
        self.origins: dict[int, int] = {}  # origin id → owning user id
        self.now = 0  # ticks on every write, like the real clock

    def _tick(self) -> int:
        self.now += 1
        return self.now

    def create(
        self, user_id: int, song_id: int, source: str, origin_id: int | None = None
    ) -> Request:
        if source not in REQUEST_SOURCES:
            raise ValueError(f"unknown request source {source!r}")
        if song_id not in self.songs:
            raise SongNotFound(f"song {song_id}")
        if origin_id is not None and self.origins.get(origin_id) != user_id:
            raise RequestNotFound(f"origin {origin_id}")
        now = self._tick()
        req = Request(
            len(self.requests) + 1, user_id, song_id, source, origin_id, None,
            "requested", now, now,
        )  # fmt: skip
        self.requests[req.id] = req
        return req

    def record_slskd_query(self, user_id: int, query: str) -> Request:
        query = " ".join(query.split())
        if not query:
            raise ValueError("empty search")
        now = self._tick()
        for req in self.requests.values():
            if (
                req.user_id == user_id
                and req.source == "slskd"
                and (req.query or "").lower() == query.lower()
            ):
                req = replace(req, last_used_at=now)
                self.requests[req.id] = req
                return req
        req = Request(
            len(self.requests) + 1, user_id, None, "slskd", None, query,
            "requested", now, now,
        )  # fmt: skip
        self.requests[req.id] = req
        return req

    def get(self, user_id: int, request_id: int) -> Request:
        req = self.requests.get(request_id)
        if req is None or req.user_id != user_id:
            raise RequestNotFound(f"request {request_id}")
        return req

    def _move(
        self, user_id: int, request_id: int, transition: Callable[[str], str]
    ) -> Request:
        req = self.get(user_id, request_id)
        req = replace(req, track_state=transition(req.track_state))
        self.requests[request_id] = req
        return req

    def drop(self, user_id: int, request_id: int) -> Request:
        return self._move(user_id, request_id, states.drop)

    def re_request(self, user_id: int, request_id: int) -> Request:
        return self._move(user_id, request_id, states.re_request)

    def history(
        self, user_id: int, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page:
        page, page_size = clamp_page(page, page_size)
        mine = sorted(
            (r for r in self.requests.values() if r.user_id == user_id),
            key=lambda r: (r.last_used_at, r.id),
            reverse=True,
        )
        start = (page - 1) * page_size
        return Page(mine[start : start + page_size], len(mine), page, page_size)
