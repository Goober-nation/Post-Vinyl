"""A background thread that calls one function every few seconds (#105).

Errors are logged and the loop goes on, so one bad tick never stops a worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class Periodic:
    def __init__(self, name: str, tick: Callable[[], object], every_s: float) -> None:
        self.name = name
        self._tick = tick
        self._every = every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # any error: the worker must keep running
                log.exception("%s tick failed", self.name)
            self._stop.wait(self._every)
