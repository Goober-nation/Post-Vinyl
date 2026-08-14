"""
Pure name-matching logic — the part of the probes with no I/O.

This module decides three things the whole placement/tag grade rests on:

1. When are two artist folders *the same artist*? (`artist_key`)
2. Which spelling of that artist is the canonical one? (`canonical_artist`)
3. When do two free-text fields — a title, an album — mean the same thing?
   (`text_key`)

It is separated from the probes precisely because it is the part that can
fail silently. A matcher that returns "no variants found" for a tree that
has three spellings of Tyler, The Creator does not raise; it just makes
every S9 assertion pass while checking nothing. So it lives here, takes
plain strings, and has real unit tests in `tests/test_live_probes.py`.

The strict-canonical placement spec, restated as rules:

- One folder per artist. Two folders whose names differ only by **case**,
  only by **punctuation**, only by **accent**, or only by a trailing
  **ft./feat. clause** are the same artist, and their existence side by side
  is a defect.
- A featuring clause must never appear in an albumartist, and therefore
  never in an artist folder name — so a folder named
  `Tyler, The Creator ft. Rex Orange County` is a defect *on its own*, with
  no second folder needed to prove it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

#: A trailing featuring clause, with or without brackets around it.
#:
#: `with` is deliberately NOT in this list. It reads as a featuring marker
#: in "Nick Cave with the Bad Seeds" and as part of the name in plenty of
#: others, and a matcher that merges two genuinely different artists is a
#: worse failure than one that misses a variant: the first invents defects
#: that will be chased, the second only fails to find one.
_FEAT_WORDS = r"feat|feats|featuring|ft|w/"

_FEAT_RE = re.compile(
    # optional opening bracket, the marker word, an optional dot, then
    # whitespace and the rest of the string.
    rf"\s*[\(\[\{{]?\s*\b(?:{_FEAT_WORDS})\b\.?\s+.*$",
    re.IGNORECASE,
)

#: Bracketed noise that peers weld into titles: [Explicit], (Official Audio).
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def strip_feat(name: str) -> str:
    """Remove a trailing featuring clause.

    >>> strip_feat("Tyler, The Creator ft. Rex Orange County")
    'Tyler, The Creator'
    >>> strip_feat("SoulChef (feat. Nieve)")
    'SoulChef'
    """
    return _FEAT_RE.sub("", name or "").strip().rstrip("([{-,&").strip()


def has_feat(name: str) -> bool:
    """True when `name` carries a featuring clause."""
    return strip_feat(name) != (name or "").strip()


def _fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form of a string.

    `&` becomes `and` before punctuation is dropped, otherwise
    "Joey Valence & Brae" and "Joey Valence and Brae" fold to different
    keys — they differ only by punctuation, which is exactly the class of
    difference this is supposed to erase.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = without_accents.casefold().replace("&", " and ")
    stripped = _NON_ALNUM_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).strip()


def text_key(text: str) -> str:
    """Comparison key for free text (titles, albums).

    Featuring clauses are preserved: "Write This Down (feat. Nieve)" is the
    track's real title, and silently equating it with "Write This Down"
    would hide a genuinely wrong tag.
    """
    return _fold(text)


def artist_key(name: str) -> str:
    """Comparison key for an artist name — the identity used for grouping.

    >>> artist_key("Tyler, The Creator") == artist_key("Tyler, the Creator")
    True
    >>> artist_key("Björk") == artist_key("Bjork")
    True
    >>> artist_key("jev.") == artist_key("jev")
    True
    """
    return _fold(strip_feat(name))


def title_key_loose(text: str) -> str:
    """`text_key` with bracketed noise removed — for *finding* files, never
    for grading them.

    Used by `FsProbe.find_by_title`, where the job is to locate every copy
    of a track no matter how a peer mangled the filename. Grading uses the
    strict key, because "Feather [Explicit]" as a title tag is a defect.
    """
    return _fold(_BRACKETED_RE.sub(" ", text or ""))


def canonical_artist(names: Iterable[str]) -> str:
    """Pick the canonical spelling from a set of variant folder names.

    Preference order, applied in turn:

    1. names with no featuring clause (a feat. clause is never canonical),
    2. the longest name (the fuller spelling is usually the real one),
    3. lexicographic order, purely so the answer is deterministic.

    With ("Tyler, the Creator", "Tyler, The Creator") that lands on
    "Tyler, The Creator" — the same-length tie breaks on ASCII order, where
    the capital wins.
    """
    candidates = sorted(set(names))
    if not candidates:
        return ""
    clean = [n for n in candidates if not has_feat(n)]
    pool = clean or [strip_feat(n) for n in candidates]
    return min(pool, key=lambda n: (-len(n), n))


def group_artist_variants(names: Iterable[str]) -> dict[str, list[str]]:
    """Group folder names by artist and return only the groups that are defects.

    A group is a defect when either:

    - it holds more than one spelling — the tree has split one artist across
      several folders; or
    - it holds exactly one spelling and that spelling carries a featuring
      clause — a folder that should never have been created under that name.

    Returns `{canonical_name: [actual folder names, sorted]}`. An empty dict
    means the artist folders are clean.
    """
    groups: dict[str, list[str]] = {}
    for name in names:
        if not name:
            continue
        groups.setdefault(artist_key(name), []).append(name)

    defects: dict[str, list[str]] = {}
    for variants in groups.values():
        distinct = sorted(set(variants))
        if len(distinct) == 1 and not has_feat(distinct[0]):
            continue
        defects[canonical_artist(distinct)] = distinct
    return defects


def merge_variant_groups(
    groups: Iterable[dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Combine per-tree variant reports into one.

    Grouping is done **per tree** and merged afterwards on purpose. Doing it
    globally would report `Searches/Tyler, The Creator` and
    `Discovery/Tyler, the Creator` as a split artist when they are two
    separate trees each holding a single, legitimate folder.
    """
    merged: dict[str, list[str]] = {}
    for group in groups:
        for canonical, variants in group.items():
            merged.setdefault(canonical, [])
            for variant in variants:
                if variant not in merged[canonical]:
                    merged[canonical].append(variant)
    return {k: sorted(v) for k, v in merged.items()}


def artist_matches(actual: str | None, expected: str) -> bool:
    """Does an artist *credit* cover the expected album artist?

    The artist tag legitimately carries the featuring clause the albumartist
    must not ("BADBADNOTGOOD feat. Samuel T. Herring"), so this is a
    containment test on the folded forms rather than equality.
    """
    if not actual:
        return False
    actual_key = _fold(actual)
    expected_key = artist_key(expected)
    if not expected_key:
        return False
    return expected_key == actual_key or f" {expected_key} " in f" {actual_key} "
