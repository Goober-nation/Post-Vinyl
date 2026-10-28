"""Stall rule (#94): noStart / tooSlow from progress samples."""

from __future__ import annotations

from app.core.stall import stall_outcome

KB = 1000


def test_no_samples_or_too_early():
    assert stall_outcome([], 100) is None
    assert stall_outcome([(0, 0)], 19.9) is None


def test_no_start_after_20s():
    assert stall_outcome([(0, 0), (10, 0)], 20) == "noStart"


def test_queue_time_not_counted():
    # samples start when slskd reports downloading, not when it was queued
    assert stall_outcome([(100, 0)], 110) is None


def test_too_slow_over_last_window():
    # 4 KB/s = 32 kbit/s
    samples = [(t, t * 4 * KB) for t in range(0, 41, 5)]
    assert stall_outcome(samples, 40) == "tooSlow"


def test_fast_enough():
    samples = [(t, t * 6 * KB) for t in range(0, 41, 5)]  # 48 kbit/s
    assert stall_outcome(samples, 40) is None


def test_only_last_window_counts():
    # fast early, then stuck for the last 20 s
    samples = [(0, 0), (10, 5_000 * KB), (20, 5_000 * KB), (40, 5_000 * KB)]
    assert stall_outcome(samples, 40) == "tooSlow"
    # stuck early, then fast
    samples = [(0, 0), (20, 1), (40, 1_000 * KB)]
    assert stall_outcome(samples, 40) is None


def test_boundary_exactly_40kbit():
    assert stall_outcome([(0, 0), (20, 100 * KB)], 20) is None  # 40 kbit/s exactly
