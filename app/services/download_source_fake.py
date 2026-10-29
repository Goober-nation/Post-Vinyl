"""In-memory DownloadSource for tests of code built on download sources.

Keeps the contract of ``download_source.py`` against a real database: it
claims the request with the guarded ``searching`` write (so a second start
does nothing), and ``finish`` / ``fail`` play out the end of the job the way a
real source would write it.
"""

from __future__ import annotations

import time

from app.core import states
from app.services.download_source import DownloadSource
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.requests import Request, RequestsStore
from app.stores.songs import Song


class FakeDownloadSource(DownloadSource):
    def __init__(self, db: Database, source: str = "fake") -> None:
        self.source = source
        self._db = db
        self.started: list[int] = []  # request ids this source claimed
        self.cancelled: list[int] = []

    def start(self, request: Request, song: Song) -> None:
        store = RequestsStore()
        with self._db.transaction(write=True) as conn:
            new = states.start_search(request.track_state)
            if store.set_track_state(
                conn, request.id, new, expected=request.track_state
            ):
                self.started.append(request.id)

    def cancel(self, request_id: int) -> None:
        if request_id in self.started:
            self.cancelled.append(request_id)

    def finish(self, request_id: int, file_location: str) -> None:
        """searching → found → midDownload → downloaded, with a completed row."""
        self._walk(request_id, file_location)

    def fail(self, request_id: int) -> None:
        """searching → notFound."""
        with self._db.transaction(write=True) as conn:
            RequestsStore().set_track_state(
                conn, request_id, states.search_done("searching", found=False)
            )

    def _walk(self, request_id: int, file_location: str) -> None:
        now = int(time.time())
        with self._db.transaction(write=True) as conn:
            reqs, downloads = RequestsStore(), DownloadsStore()
            state = states.search_done("searching", found=True)
            row = downloads.create(conn, request_id, self.source, now)
            state = states.start_download(state)
            downloads.complete(conn, row.id, file_location, now)
            reqs.set_track_state(conn, request_id, states.download_done(state))
