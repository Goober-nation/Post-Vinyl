"""Pausing the queue (#111, user 2026-10-30): an admin stops new work.

While paused, no new search starts (the slskd gate waits), dispatch leaves
requests waiting in ``requested`` and the slskd tab refuses picks. Running
downloads, and their move to the next candidate, carry on. Resume lets the
redispatch tick start what waited. The flag is stored, so a restart keeps it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from app.stores.database import Database
from app.stores.setup import SetupStore

KEY = "queue_paused"


class QueuePaused(Exception):
    """New work is refused while the queue is paused."""


class QueuePauseBase(ABC):
    @abstractmethod
    def paused(self) -> bool: ...

    @abstractmethod
    def set_paused(self, paused: bool) -> None: ...


class QueuePause(QueuePauseBase):
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._store = SetupStore()

    def paused(self) -> bool:
        with self._db.transaction() as conn:
            return self._store.get(conn, KEY) == "1"

    def set_paused(self, paused: bool) -> None:
        with self._db.transaction(write=True) as conn:
            self._store.set(conn, KEY, "1" if paused else "0", int(self._clock()))
