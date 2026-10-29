"""Download monitor worker (#102): calls the downloader's ``poll`` every few
seconds, so running attempts move on (queue wait, stall rule, completion)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

POLL_EVERY_S = 2.0


class DownloadMonitor:
    def __init__(self, poll: Callable[[], None], every_s: float = POLL_EVERY_S) -> None:
        self._poll = poll
        self._every = every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="download-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:  # any error: the monitor must keep running
                log.exception("download monitor tick failed")
            self._stop.wait(self._every)
