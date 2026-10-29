"""Which release a song is tagged as (#104, user 2026-10-29).

The earliest **official plain album** (release group type Album, no secondary
type such as Compilation or Live); only when there is none, the earliest
official plain EP, then Single. Feather → "Modal Soul" (2005), not the later
compilations or reissues. With none of those, the earliest official release
of any kind, so a song that only came out on a soundtrack is still tagged.
Unknown dates go last; ties keep MusicBrainz's order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

TIERS = ("Album", "EP", "Single")


class Release(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def date(self) -> str: ...
    @property
    def primary_type(self) -> str: ...
    @property
    def secondary_types(self) -> tuple[str, ...]: ...


R = TypeVar("R", bound=Release)


def _earliest(releases: Sequence[R]) -> R | None:
    dated = sorted(releases, key=lambda r: (not r.date, r.date))
    return dated[0] if dated else None


def choose_release(releases: Sequence[R]) -> R | None:
    official = [r for r in releases if r.status == "Official"]
    for tier in TIERS:
        plain = [
            r for r in official if r.primary_type == tier and not r.secondary_types
        ]
        if plain:
            return _earliest(plain)
    return _earliest(official)
