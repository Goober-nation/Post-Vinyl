"""The downloads tab (#111): the queue with live progress, attempts, next peer.

Rows come from our bookkeeping (``downloads`` + the request and song); the
progress of a running attempt is read live from slskd by its transfer id
(slskd owns transfers, nothing is copied). A user sees their own downloads;
an admin may ask for everyone's. Next peer is the request's owner's or an
admin's (user, 2026-10-30).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.clients.slskd import SlskdClientBase, SlskdError
from app.services.requests import DEFAULT_PAGE_SIZE, clamp_page
from app.services.slskd_download import SlskdDownloader
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.downloads import DownloadsStore, QueueRow
from app.stores.slskd import SlskdAttempt, SlskdStore

log = logging.getLogger(__name__)

STATES = frozenset({"queued", "downloading", "completed", "failed", "cancelled"})


class DownloadNotFound(LookupError):
    """No such download for this user."""


class AdminOnly(PermissionError):
    """Only an admin may do this."""


class NotRunning(Exception):
    """The download has no running attempt to skip."""


@dataclass(frozen=True)
class Progress:
    """slskd's live view of the running attempt's transfer."""

    state: str
    bytes_transferred: int
    size: int
    average_speed: int  # bytes/s
    place_in_queue: int | None


@dataclass(frozen=True)
class AttemptView:
    id: int
    rank: int
    username: str
    filename: str  # the peer's path
    outcome: str | None  # None while running
    started_at: int
    ended_at: int | None


@dataclass(frozen=True)
class QueueItem:
    id: int
    request_id: int
    username: str  # who asked for it
    source: str
    state: str  # queued | downloading | completed | failed | cancelled
    track_state: str
    title: str | None
    artist: str | None
    created_at: int
    completed_at: int | None
    attempts: int
    current: AttemptView | None  # the running attempt
    progress: Progress | None


@dataclass(frozen=True)
class QueuePage:
    items: list[QueueItem]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class DownloadDetail:
    item: QueueItem
    attempts: list[AttemptView]


class DownloadsServiceBase(ABC):
    @abstractmethod
    def page(
        self, user_id: int, is_admin: bool, *, everyone: bool = False,
        state: str | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    ) -> QueuePage: ...  # fmt: skip

    @abstractmethod
    def get(self, user_id: int, is_admin: bool, download_id: int) -> DownloadDetail: ...

    @abstractmethod
    def next_peer(
        self, user_id: int, is_admin: bool, download_id: int
    ) -> DownloadDetail:
        """Skip the running attempt; the peer isn't blamed."""


def view(a: SlskdAttempt) -> AttemptView:
    return AttemptView(a.id, a.rank, a.username, a.filename, a.outcome,
                       a.started_at, a.ended_at)  # fmt: skip


class DownloadsService(DownloadsServiceBase):
    def __init__(
        self,
        db: Database,
        client: SlskdClientBase,
        downloader: SlskdDownloader,
        sources: SourcePreferences | None = None,  # None: slskd always on (tests)
    ) -> None:
        self._sources = sources
        self._db = db
        self._client = client
        self._downloader = downloader
        self._downloads = DownloadsStore()
        self._slskd = SlskdStore()

    def _progress(self, a: SlskdAttempt, ask_place: bool = False) -> Progress | None:
        """``ask_place``: ask the peer for the queue position of a remotely
        queued transfer (a Soulseek message: the detail view only, not the
        polled list, which shows what slskd stored from the last ask)."""
        if a.transfer_id is None:
            return None  # being queued right now
        try:
            t = self._client.get_download(a.username, a.transfer_id)
            place = t.place_in_queue
            if ask_place and t.state == "Queued, Remotely":
                place = self._client.place_in_queue(a.username, a.transfer_id)
        except SlskdError as e:  # gone at slskd or slskd down: show the row anyway
            log.info("transfer %s unreadable: %s", a.transfer_id, e)
            return None
        return Progress(t.state, t.bytes_transferred, t.size, t.average_speed, place)

    def _item(
        self, row: QueueRow, attempts: list[SlskdAttempt], ask_place: bool = False
    ) -> QueueItem:
        running = next((a for a in reversed(attempts) if a.outcome is None), None)
        return QueueItem(
            row.id, row.request_id, row.username, row.source, row.state,
            row.track_state, row.title, row.artist, row.created_at, row.completed_at,
            len(attempts), view(running) if running else None,
            self._progress(running, ask_place) if running else None,
        )  # fmt: skip

    def page(
        self, user_id: int, is_admin: bool, *, everyone: bool = False,
        state: str | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    ) -> QueuePage:  # fmt: skip
        if everyone and not is_admin:
            raise AdminOnly("only an admin sees everyone's downloads")
        if state is not None and state not in STATES:
            raise ValueError(
                f"unknown download state {state!r}; possible: {sorted(STATES)}"
            )
        page, page_size = clamp_page(page, page_size)
        owner = None if everyone else user_id
        with self._db.transaction() as conn:
            rows = self._downloads.queue_page(
                conn, owner, state, page_size, (page - 1) * page_size
            )
            total = self._downloads.queue_count(conn, owner, state)
            attempts = {
                r.id: self._slskd.attempts_for_download(conn, r.id) for r in rows
            }
        items = [self._item(r, attempts[r.id]) for r in rows]
        return QueuePage(items, total, page, page_size)

    def _owned(self, user_id: int, is_admin: bool, download_id: int) -> QueueRow:
        with self._db.transaction() as conn:
            row = self._downloads.queue_row(conn, download_id)
        if row is None or (row.user_id != user_id and not is_admin):
            raise DownloadNotFound(f"download {download_id}")
        return row

    def get(self, user_id: int, is_admin: bool, download_id: int) -> DownloadDetail:
        row = self._owned(user_id, is_admin, download_id)
        with self._db.transaction() as conn:
            attempts = self._slskd.attempts_for_download(conn, row.id)
        return DownloadDetail(
            self._item(row, attempts, ask_place=True), [view(a) for a in attempts]
        )

    def next_peer(
        self, user_id: int, is_admin: bool, download_id: int
    ) -> DownloadDetail:
        row = self._owned(user_id, is_admin, download_id)
        if self._sources is not None:  # the next peer is new work at slskd
            self._sources.require(row.user_id, "download", row.source)
        if row.source != "slskd" or not self._downloader.next_peer(row.id):
            raise NotRunning(f"download {download_id} has no running attempt")
        return self.get(user_id, is_admin, download_id)
