"""Attempt outcomes while a transfer runs (#94, decided 2026-10-26).

- ``noStart``: no byte arrived 20 s after slskd first reported the transfer as
  downloading (time spent queued doesn't count).
- ``tooSlow``: average speed over the last 20 s below 40 kbit/s (≈ 5 KB/s).

The caller keeps progress samples ``(seconds, bytes_transferred)`` taken since
the transfer started downloading; the first sample is that moment.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

STALL_WINDOW_S = 20.0
MIN_SPEED_BITS_PER_S = 40_000

Outcome = Literal["noStart", "tooSlow"]


def stall_outcome(samples: Sequence[tuple[float, int]], now: float) -> Outcome | None:
    """``None`` while the transfer may go on."""
    if not samples:
        return None
    started_at = samples[0][0]
    if now - started_at < STALL_WINDOW_S:
        return None
    latest = samples[-1][1]
    if latest == 0:
        return "noStart"
    # bytes at the window's start: the last sample at or before it
    window_start = now - STALL_WINDOW_S
    before = [b for t, b in samples if t <= window_start]
    start_bytes = before[-1] if before else samples[0][1]
    bits_per_s = (latest - start_bytes) * 8 / STALL_WINDOW_S
    return "tooSlow" if bits_per_s < MIN_SPEED_BITS_PER_S else None
