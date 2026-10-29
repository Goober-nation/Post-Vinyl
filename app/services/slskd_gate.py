"""When an slskd search may run, and when its empty result can be trusted (#100).

Part of the slskd backend. Decided with the user, 2026-10-29:

- **One search at a time**, at most ``searches_per_minute`` started in any
  60 s (4: live 2026-10-27, faster searching got the account throttled).
- **Ready** = logged in + a distributed-network parent. Not ready → wait with
  no limit (checked every 5 s, a log line each minute); a request is never
  marked ``notFound`` because slskd was down. Restarting slskd is a watchdog's
  job, not this one.
- **Parent lost during a search** (the search's ``still_ready`` says no, or
  slskd isn't ready right after) → the result is dropped and the same query
  runs again.
- **Empty results are trusted only when confirmed**: before a request ends
  ``notFound``, ``empty_trusted(since)`` must be true. It is when some search
  returned results after ``since``; otherwise the throttle check search runs
  (``throttle_check_query``, healthy at ``throttle_check_min_responses``). A
  failed check pauses all searching for ``throttle_pause_minutes`` and returns
  False: the caller runs its queries again.

State lives in memory (one app, one slskd); a restart starts fresh.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.clients.slskd import SlskdClientBase, SlskdError
from app.config import SlskdSettings

log = logging.getLogger(__name__)

R = TypeVar("R")

READY_CHECK_EVERY_S = 5
RATE_WINDOW_S = 60

# runs one query; calls still_ready() while it polls and raises ParentLost on False
Search = Callable[[str, Callable[[], bool]], R]


class ParentLost(Exception):
    """Raised by a search that saw slskd lose readiness while it ran."""


@dataclass(frozen=True)
class Ran(Generic[R]):
    result: R
    started_at: float  # the gate's clock; pass to empty_trusted() as ``since``


class SlskdGate:
    def __init__(
        self,
        client: SlskdClientBase,
        settings: SlskdSettings,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()  # one search at a time
        self._starts: deque[float] = deque()
        self._paused_until = 0.0
        self._last_healthy = -math.inf  # when a search last came back with results

    def ready(self) -> bool:
        try:
            state = self._client.app_state()
        except SlskdError as e:
            log.debug("slskd state unavailable: %s", e)
            return False
        return state.logged_in and state.has_parent

    def search(
        self, query: str, run: Search[R], responses: Callable[[R], int]
    ) -> Ran[R]:
        """Run ``query`` once slskd is ready and the rate cap allows it."""
        with self._lock:
            return self._run(query, run, responses, healthy_at=1)

    def empty_trusted(
        self, since: float, run: Search[R], responses: Callable[[R], int]
    ) -> bool:
        """Whether empty results from ``since`` on can be believed (see module doc)."""
        with self._lock:
            if self._last_healthy >= since:
                return True
            s = self._settings
            check = self._run(
                s.throttle_check_query,
                run,
                responses,
                healthy_at=s.throttle_check_min_responses,
            )
            if responses(check.result) >= s.throttle_check_min_responses:
                return True
            self._paused_until = self._clock() + s.throttle_pause_minutes * 60
            log.warning(
                "slskd looks throttled: check search %r got %d responses (< %d); "
                "pausing searches for %d min",
                s.throttle_check_query,
                responses(check.result),
                s.throttle_check_min_responses,
                s.throttle_pause_minutes,
            )
            return False

    # ------------------------------------------------------------ internals

    def _run(
        self, query: str, run: Search[R], responses: Callable[[R], int], healthy_at: int
    ) -> Ran[R]:
        while True:
            self._wait_turn()
            self._wait_ready()
            started = self._clock()
            self._starts.append(started)
            try:
                result = run(query, self.ready)
            except ParentLost:
                log.info(
                    "slskd lost readiness during search %r; running it again", query
                )
                continue
            if not self.ready():
                log.info("slskd not ready after search %r; running it again", query)
                continue
            if responses(result) >= healthy_at:
                self._last_healthy = self._clock()
            return Ran(result, started)

    def _wait_turn(self) -> None:
        while (wait := self._paused_until - self._clock()) > 0:
            self._sleep(wait)
        cap = self._settings.searches_per_minute
        while True:
            now = self._clock()
            while self._starts and now - self._starts[0] >= RATE_WINDOW_S:
                self._starts.popleft()
            if len(self._starts) < cap:
                return
            self._sleep(RATE_WINDOW_S - (now - self._starts[0]))

    def _wait_ready(self) -> None:
        waited = 0
        while not self.ready():
            if waited % 60 == 0:
                log.warning(
                    "slskd not ready (logged out or no parent); waiting %d s", waited
                )
            self._sleep(READY_CHECK_EVERY_S)
            waited += READY_CHECK_EVERY_S
