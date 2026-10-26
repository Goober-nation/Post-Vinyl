"""
Unit tests for SearchRateLimiter in isolation from SlskdSearch.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.exceptions import SearchRateLimitedError
from app.services.search_limiter import SearchRateLimiter


class TestConstruction:
    def test_rejects_non_positive_max_searches(self):
        with pytest.raises(ValueError):
            SearchRateLimiter(max_searches=0, window_seconds=60)

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError):
            SearchRateLimiter(max_searches=1, window_seconds=0)


class TestAcquire:
    def test_first_n_acquires_succeed_immediately(self):
        limiter = SearchRateLimiter(max_searches=3, window_seconds=60)
        for _ in range(3):
            limiter.acquire(timeout=0)  # must not raise

    def test_n_plus_one_within_window_times_out(self):
        limiter = SearchRateLimiter(max_searches=2, window_seconds=60)
        limiter.acquire(timeout=0)
        limiter.acquire(timeout=0)
        with pytest.raises(SearchRateLimitedError):
            limiter.acquire(timeout=0.1)

    def test_error_carries_the_configured_limit_and_window(self):
        limiter = SearchRateLimiter(max_searches=2, window_seconds=60)
        limiter.acquire(timeout=0)
        limiter.acquire(timeout=0)
        with pytest.raises(SearchRateLimitedError) as exc_info:
            limiter.acquire(timeout=0)
        assert exc_info.value.details == {"max_searches": 2, "window_seconds": 60}

    def test_slot_frees_after_window_elapses(self):
        limiter = SearchRateLimiter(max_searches=1, window_seconds=0.2)
        limiter.acquire(timeout=0)
        with pytest.raises(SearchRateLimitedError):
            limiter.acquire(timeout=0)
        limiter.acquire(timeout=1.0)  # blocks ~0.2s then succeeds

    def test_blocking_acquire_actually_waits_rather_than_racing(self):
        """A slot should not appear to free up before the window has really
        elapsed — guards against an off-by-one in the cutoff math."""
        limiter = SearchRateLimiter(max_searches=1, window_seconds=0.3)
        limiter.acquire(timeout=0)
        start = time.monotonic()
        limiter.acquire(timeout=2.0)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25  # allow a little scheduling slack

    def test_unlimited_wait_when_timeout_is_none(self):
        limiter = SearchRateLimiter(max_searches=1, window_seconds=0.1)
        limiter.acquire(timeout=0)
        limiter.acquire(timeout=None)  # must not raise, just waits it out


class TestConcurrency:
    def test_concurrent_acquires_never_exceed_the_limit(self):
        """The regression this exists to prevent: two threads racing
        check-then-append must not both succeed past the cap."""
        limiter = SearchRateLimiter(max_searches=5, window_seconds=60)
        succeeded = []
        failed = []
        lock = threading.Lock()

        def worker():
            try:
                limiter.acquire(timeout=0.5)
                with lock:
                    succeeded.append(1)
            except SearchRateLimitedError:
                with lock:
                    failed.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(succeeded) == 5
        assert len(failed) == 15


class TestSnapshot:
    def test_reports_empty_window_initially(self):
        limiter = SearchRateLimiter(max_searches=4, window_seconds=60)
        snap = limiter.snapshot()
        assert snap == {"used": 0, "max": 4, "window_seconds": 60}

    def test_reports_usage_after_acquires(self):
        limiter = SearchRateLimiter(max_searches=4, window_seconds=60)
        limiter.acquire(timeout=0)
        limiter.acquire(timeout=0)
        assert limiter.snapshot()["used"] == 2

    def test_prunes_expired_entries(self):
        limiter = SearchRateLimiter(max_searches=4, window_seconds=0.1)
        limiter.acquire(timeout=0)
        time.sleep(0.15)
        assert limiter.snapshot()["used"] == 0
