"""
SearchRateLimiter — bounds how often SlskdSearch is allowed to broadcast a
NEW query onto the Soulseek network.

Why this exists (diagnosed 2026-08-13; see live-artifacts/hang-probe-1 and
tests/live/tools/probe_layers.py): a single Soulseek search fans out
network-wide, and every peer holding a match dials in to deliver its
response. Measured directly against slskd's own /proc/net/tcp connection
count (not just the host's view of them, which is inflated further by
Docker Desktop's userspace port forwarder): one search opened ~2,800
concurrent peer connections inside the slskd container, peaking within
about 30 seconds and reaped back down to near-idle within roughly a minute
of the last search. That is normal, healthy Soulseek behavior — slskd's own
15s peer inactivity timeout does its job.

The failure was never one search; it was firing searches back to back with
nothing pacing them. musica's own search log showed the literal query text
'Alright' searched ten times inside a single live-suite run. Each spike
needs about a minute to drain on slskd's side (longer once Docker Desktop's
forwarder is factored in) — ten of them with no gap between never had the
chance, and the superimposed spikes are what pinned the host's connection
count at several thousand for the whole run.

This is a sliding-window limiter: no more than `max_searches` new slskd
searches may start within any `window_seconds`. `acquire()` blocks rather
than failing immediately, because both call sites that create new searches
(the manual search route, RecPuller) can tolerate a wait — RecPuller
already paces itself between tracks, and a manual search is already a
multi-second round trip end to end. A bounded wait is enforced via
`timeout`; past that, `SearchRateLimitedError` is raised so a caller that
genuinely cannot wait (or a caller that wants to surface backpressure to a
human) has a way out.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.exceptions import SearchRateLimitedError


class SearchRateLimiter:
    """Sliding-window limiter on how often `acquire()` may succeed.

    Thread-safe: `acquire()` is called from both the HTTP worker threadpool
    (manual searches) and RecPuller's background thread, often concurrently.
    """

    def __init__(self, max_searches: int, window_seconds: float) -> None:
        if max_searches < 1:
            raise ValueError("max_searches must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_searches
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _seconds_until_slot(self, now: float) -> float:
        """0.0 if a slot is free right now, else how long until the oldest
        timestamp ages out of the window. Caller must hold `_lock`."""
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) < self._max:
            return 0.0
        return self._timestamps[0] - cutoff

    def acquire(self, timeout: float | None = None) -> None:
        """Block until a slot in the window is free, then take it.

        Sleeps outside the lock between checks — slots free purely by time
        elapsing, nothing proactively signals it, so this is a polling wait
        rather than a condition variable. Polling in <=1s increments keeps
        the wait responsive without meaningfully spinning the CPU: this
        gates network calls that already take seconds, not a hot path.

        Raises:
            SearchRateLimitedError: `timeout` elapsed before a slot freed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._seconds_until_slot(now)
                if wait <= 0:
                    self._timestamps.append(now)
                    return
            if deadline is not None and time.monotonic() + wait > deadline:
                raise SearchRateLimitedError(self._max, self._window)
            time.sleep(min(wait, 1.0))

    def snapshot(self) -> dict:
        """Current window occupancy — for logging/diagnostics only."""
        with self._lock:
            now = time.monotonic()
            self._seconds_until_slot(now)  # prunes expired entries as a side effect
            return {
                "used": len(self._timestamps),
                "max": self._max,
                "window_seconds": self._window,
            }
