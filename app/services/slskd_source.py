"""The slskd download source (#101; download step in #102).

``start`` claims the request (``searching``) and queues it; one worker thread
takes requests in order and runs the search (searches run one at a time
anyway, user 2026-10-29), then hands a ``found`` one to the downloader, whose
attempts the download monitor follows. Cancelling is dropping: the search sees
the request moved before its next query and stops; the monitor cancels a
running transfer of a dropped request.

A search that crashes releases the request back to ``requested`` and queues
it again once; a second crash ends it ``notFound`` with the error logged
(user, 2026-10-29). Requests left ``searching`` by an app restart are picked
up by ``DownloadDispatch.resume``.
"""

from __future__ import annotations

import logging
import queue
import threading

from app.core import states
from app.services.download_source import DownloadSource
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_search import SlskdSearcher
from app.stores.database import Database
from app.stores.requests import Request, RequestsStore
from app.stores.songs import Song

log = logging.getLogger(__name__)

MAX_CRASHES = 2  # per request, counted in memory


class SlskdSource(DownloadSource):
    source = "slskd"

    def __init__(
        self,
        db: Database,
        searcher: SlskdSearcher,
        downloader: SlskdDownloader | None = None,
    ) -> None:
        self._db = db
        self._searcher = searcher
        self._downloader = downloader  # None: search only (tests)
        self._requests = RequestsStore()
        self._queue: queue.Queue[tuple[Request, Song] | None] = queue.Queue()
        self._crashes: dict[int, int] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def start(self, request: Request, song: Song) -> bool:
        new = states.start_search(request.track_state)
        with self._db.transaction(write=True) as conn:
            claimed = self._requests.set_track_state(
                conn, request.id, new, expected=request.track_state
            )
        if not claimed:
            return False
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="slskd-search", daemon=True
                )
                self._worker.start()
        self._queue.put((request, song))
        return True

    def cancel(self, request_id: int) -> None:
        """Nothing to stop yet: a dropped search stops on its own (#102 adds transfers)."""

    def stop(self) -> None:
        """Finish the queued requests (re-queued ones too), then end the worker."""
        if self._worker is not None:
            self._queue.join()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join()

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            request, song = item
            try:
                found = self._searcher.search(request, song)
                log.info("request %s search done: found=%s", request.id, found)
                if found and self._downloader is not None:
                    self._downloader.start(request.id)
            except Exception:  # any error: one bad request must not end the worker
                log.exception("request %s search failed", request.id)
                self._crashed(request, song)
            finally:
                self._queue.task_done()

    def _crashed(self, request: Request, song: Song) -> None:
        self._crashes[request.id] = self._crashes.get(request.id, 0) + 1
        give_up = self._crashes[request.id] >= MAX_CRASHES
        new = (
            states.search_done("searching", found=False)
            if give_up
            else states.release("searching")
        )
        with self._db.transaction(write=True) as conn:
            moved = self._requests.set_track_state(
                conn, request.id, new, expected="searching"
            )
            fresh = self._requests.get(conn, request.id)
        if not moved or fresh is None:
            return  # dropped meanwhile
        if give_up:
            log.error(
                "request %s ended notFound after %d crashes", request.id, MAX_CRASHES
            )
            return
        self.start(fresh, song)
