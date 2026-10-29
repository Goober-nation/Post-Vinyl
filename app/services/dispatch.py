"""Download dispatch (#99): hand a request to the user's download source.

The source is the user's first enabled one in ``sources.download`` order
(config + their preferences, ``services/sources.py``). With none enabled the
request waits in its state, nothing is written (user, 2026-10-29). Falling
back to the next source after a ``notFound`` is not built.

``status`` reads what the source wrote (the request's ``track_state`` and its
newest ``downloads`` row); dispatch never changes either.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.core import states
from app.services.download_source import DownloadSource
from app.services.requests import RequestNotFound
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.requests import RequestsStore
from app.stores.songs import SongsStore

log = logging.getLogger(__name__)

STARTABLE = frozenset({"requested", "reRequested"})


class NotStartable(Exception):
    """The request isn't waiting to be fetched (wrong state, or no song yet)."""


@dataclass(frozen=True)
class DownloadStatus:
    track_state: str
    source: str | None  # the source of the newest downloads row
    download_state: str | None  # queued | downloading | completed | failed | cancelled
    file_location: str | None


class DownloadDispatchBase(ABC):
    @abstractmethod
    def dispatch(self, user_id: int, request_id: int) -> str | None:
        """The source the request went to; None when no source is enabled."""

    @abstractmethod
    def cancel(self, user_id: int, request_id: int) -> None:
        """Ask every source to stop work on the request (after it was dropped)."""

    @abstractmethod
    def status(self, user_id: int, request_id: int) -> DownloadStatus: ...

    @abstractmethod
    def redispatch_waiting(self) -> int:
        """Start requests still ``requested``/``reRequested`` (e.g. no download
        source was on); returns how many went to a source."""

    @abstractmethod
    def resume(self) -> int:
        """At startup: re-dispatch requests a previous run left ``searching``."""


class DownloadDispatch(DownloadDispatchBase):
    def __init__(
        self,
        db: Database,
        sources: SourcePreferences,
        download_sources: list[DownloadSource],
        paused: Callable[[], bool] = lambda: False,
    ) -> None:
        self._db = db
        self._paused = paused
        self._prefs = sources
        self._sources = {s.source: s for s in download_sources}
        self._requests = RequestsStore()
        self._songs = SongsStore()
        self._downloads = DownloadsStore()

    def dispatch(self, user_id: int, request_id: int) -> str | None:
        with self._db.transaction() as conn:
            req = self._requests.get(conn, request_id)
            if req is None or req.user_id != user_id:
                raise RequestNotFound(f"request {request_id}")
            song = self._songs.get(conn, req.song_id) if req.song_id else None
        if req.track_state not in STARTABLE:
            raise NotStartable(f"request {request_id} is {req.track_state}")
        if song is None:
            raise NotStartable(f"request {request_id} has no song")
        if self._paused():  # an admin paused the queue (#111): wait, redispatch resumes
            log.debug("request %s waits: the queue is paused", request_id)
            return None
        names = self._prefs.for_user(user_id, "download")
        source = next((self._sources[n] for n in names if n in self._sources), None)
        if source is None:
            log.info("request %s waits: no download source enabled", request_id)
            return None
        if not source.start(req, song):
            raise NotStartable(f"request {request_id} was started meanwhile")
        return source.source

    def redispatch_waiting(self) -> int:
        with self._db.transaction() as conn:
            waiting = [*self._requests.in_state(conn, "requested"),
                       *self._requests.in_state(conn, "reRequested")]  # fmt: skip
        started = 0
        for req in waiting:
            if req.song_id is None:
                continue  # a typed Soulseek search: started by hand, not here
            try:
                started += self.dispatch(req.user_id, req.id) is not None
            except NotStartable:
                pass  # moved meanwhile
        return started

    def resume(self) -> int:
        """At startup: requests left ``searching`` by the last run go back to
        ``requested`` and are dispatched again; returns how many."""
        with self._db.transaction(write=True) as conn:
            stuck = self._requests.in_state(conn, "searching")
            for req in stuck:
                self._requests.set_track_state(
                    conn, req.id, states.release(req.track_state), expected="searching"
                )
        for req in stuck:
            try:
                self.dispatch(req.user_id, req.id)
            except NotStartable as e:
                log.info("request %s not resumed: %s", req.id, e)
        return len(stuck)

    def _owned(self, user_id: int, request_id: int) -> None:
        with self._db.transaction() as conn:
            req = self._requests.get(conn, request_id)
        if req is None or req.user_id != user_id:
            raise RequestNotFound(f"request {request_id}")

    def cancel(self, user_id: int, request_id: int) -> None:
        self._owned(user_id, request_id)
        for source in self._sources.values():
            source.cancel(request_id)

    def status(self, user_id: int, request_id: int) -> DownloadStatus:
        with self._db.transaction() as conn:
            req = self._requests.get(conn, request_id)
            if req is None or req.user_id != user_id:
                raise RequestNotFound(f"request {request_id}")
            rows = self._downloads.for_request(conn, request_id)
        last = rows[-1] if rows else None
        return DownloadStatus(
            req.track_state,
            last.source if last else None,
            last.state if last else None,
            last.file_location if last else None,
        )
