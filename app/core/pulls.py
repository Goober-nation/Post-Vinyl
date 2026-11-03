"""Pure rules for pulls: which songs may be pulled, and default playlist names."""

from __future__ import annotations

import time

from app.core.matcher import version_hit, words


def is_excluded(title: str) -> bool:
    """An excluded version: pulls pick for the user, so none get in."""
    return version_hit(words(title), []) is not None


def _date(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def recs_name(now: int) -> str:
    return f"recommendations-{_date(now)}"


def radio_name(artist: str, now: int) -> str:
    return f"radio-{artist}-{_date(now)}"
