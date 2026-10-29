"""Generic download source (#99): "get this song" for a request.

Core and the dispatch service know nothing about how a source finds or
fetches files (architecture.md, "Swappable sources"). A source owns the
request's ``track_state`` from ``searching`` to its end state and writes its
``downloads`` rows; the outside world only reads those.

Contract every source keeps:
- ``start`` returns at once; the work runs on the source's own thread.
- The source claims the request with its first state change
  (``requested``/``reRequested`` → ``searching``, written with ``expected=``)
  and returns whether it did; if that write misses, another dispatch got there
  first and it does nothing.
- Cancelling is dropping (user, 2026-10-29): the requests service moves the
  request to ``dropped`` and the source notices before its next step.
  ``cancel`` only stops what the source runs outside the request's state (a
  transfer, #102); a no-op when it runs nothing for it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.stores.requests import Request
from app.stores.songs import Song


class DownloadSource(ABC):
    source: str  # the name in the config's sources.download list

    @abstractmethod
    def start(self, request: Request, song: Song) -> bool:
        """Whether this call claimed the request."""

    @abstractmethod
    def cancel(self, request_id: int) -> None: ...
