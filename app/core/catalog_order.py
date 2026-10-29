"""Order of track search results (#98, user 2026-10-29).

MusicBrainz's own order is unstable for tied scores and puts e.g. "Feather
(N0ms Bootleg)" (score 100) above the studio "Feather" (77), so the catalog
service sorts every source's results with this key, best first:

1. an excluded version (bootleg, live, remix, … the matcher's list) in the
   title or disambiguation that the typed query doesn't contain goes last;
2. a recording with an Official release on a plain Album / Single / EP (no
   secondary type such as Compilation, Live, Mixtape/Street) comes first;
3. higher source score; a source with no scores (Deezer) keeps its own
   order instead (live 2026-10-29: sorting Deezer by id put an unrelated
   track first);
4. earlier first-release date;
5. source id, only so the order never changes between calls.

A source that lacks releases just ties on rule 2.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.matcher import version_hit, words

STUDIO_TYPES = frozenset({"Album", "Single", "EP"})

# (status, primary type, secondary types) of one release
ReleaseKind = tuple[str, str, tuple[str, ...]]


def has_studio_release(releases: Iterable[ReleaseKind]) -> bool:
    return any(
        status == "Official" and primary in STUDIO_TYPES and not secondary
        for status, primary, secondary in releases
    )


def track_key(
    query: str,
    title: str,
    disambiguation: str,
    releases: Iterable[ReleaseKind],
    score: int | None,
    position: int,
    first_release_date: str,
    source_id: str,
) -> tuple[bool, bool, int, bool, str, str]:
    """Sort key: smaller is better; ``position`` is the hit's place in the source's answer."""
    variant = version_hit(words(f"{title} {disambiguation}"), words(query)) is not None
    return (
        variant,
        not has_studio_release(releases),
        -score if score is not None else position,
        not first_release_date,  # unknown dates after known ones
        first_release_date,
        source_id,
    )
