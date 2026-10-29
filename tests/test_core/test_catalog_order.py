"""Track search result order (#98)."""

from __future__ import annotations

from app.core.catalog_order import ReleaseKind, has_studio_release, track_key

STUDIO: ReleaseKind = ("Official", "Album", ())
COMPILATION: ReleaseKind = ("Official", "Album", ("Compilation",))
MIXTAPE: ReleaseKind = ("Bootleg", "Other", ("Mixtape/Street",))


def order(query: str, hits: list[tuple]) -> list[str]:
    """hits: (id, title, disambiguation, releases, score, first_release_date)"""
    return [
        h[0]
        for h in sorted(
            hits,
            key=lambda h: track_key(
                query, h[1], h[2], h[3], h[4], hits.index(h), h[5], h[0]
            ),
        )
    ]


def test_feather_live_case() -> None:
    """The three real MusicBrainz hits for Nujabes – Feather (2026-10-29)."""
    hits = [
        ("n0ms", "Feather (N0ms Bootleg)", "", [COMPILATION], 100, "2015"),
        ("mixtape", "Feather", "", [MIXTAPE], 82, "2006"),
        ("studio", "Feather", "", [MIXTAPE, COMPILATION, STUDIO], 77, "2005-11-11"),
    ]
    assert order("Feather", hits) == ["studio", "mixtape", "n0ms"]


def test_version_word_in_query_is_not_penalised() -> None:
    hits = [
        ("plain", "Song", "", [STUDIO], 100, ""),
        ("live", "Song", "live", [STUDIO], 90, ""),
    ]
    assert order("Song", hits) == ["plain", "live"]
    assert order("Song live", hits)[0] == "plain"  # both allowed, studio score wins
    assert order("Song (Live)", [hits[1], ("x", "Song", "", [], 99, "")]) == [
        "live",
        "x",
    ]


def test_remaster_suffix_is_the_same_song() -> None:
    hits = [
        ("rem", "Song - 2011 Remaster", "", [STUDIO], 90, ""),
        ("p", "Song", "", [], 100, ""),
    ]
    assert order("Song", hits) == ["rem", "p"]  # studio release beats score


def test_score_then_date_then_id() -> None:
    hits = [
        ("b", "Song", "", [], 90, "2001"),
        ("a", "Song", "", [], 90, "2001"),
        ("c", "Song", "", [], 90, ""),
        ("d", "Song", "", [], 95, "2010"),
        ("e", "Song", "", [], 90, "1999"),
    ]
    assert order("Song", hits) == ["d", "e", "a", "b", "c"]


def test_source_without_scores_keeps_its_own_order() -> None:
    """Deezer gives no scores: its relevance order stands (live 2026-10-29)."""
    hits = [
        ("9", "Feather", "", [], None, ""),
        ("1", "Les Echos De Nuages", "", [], None, ""),
        ("5", "Feather (Remix)", "", [], None, ""),
    ]
    assert order("Feather", hits) == ["9", "1", "5"]


def test_studio_release_needs_official_plain_type() -> None:
    assert has_studio_release([("Official", "Single", ())])
    assert has_studio_release([("Official", "EP", ())])
    assert not has_studio_release([("Bootleg", "Album", ())])
    assert not has_studio_release([("Official", "Album", ("Live",))])
    assert not has_studio_release([("Official", "Other", ())])
    assert not has_studio_release([])
