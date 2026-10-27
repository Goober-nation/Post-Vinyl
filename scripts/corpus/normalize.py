"""Name normalization prototype for the normalization test (#70).

Turns any source's track, artist and album names into the same compare-key
``Song(track, artists, album)``, so the same song from Deezer, MusicBrainz,
ListenBrainz (or later a file path) comes out identical. ``artists`` is a
sorted tuple of single artist names, because sources split credits
differently ("A & B" in one credit string vs. two contributors).

Every rule can be switched off (``rules=``) so the report (#70) can measure
what each one contributes. Rules (decided with the user 2026-10-26):

- ``feat``     drop featured credits: "(feat. X)" in titles, featured artists
- ``version``  drop same-song suffixes: Remastered, Deluxe, years, Album Version
- ``amp``      "&" → "and"
- ``the``      drop a leading "The" from artist names
- ``punct``    brackets and punctuation → spaces (words inside kept),
               apostrophes removed ("Don't" → "dont")
- ``accents``  strip accents from Latin letters only (Cyrillic й stays й)
- ``case``     case-fold

No transliteration: Кино and Kino stay different. Pure and stdlib-only; it
moves to ``app/core/normalize.py`` once the rules are settled (#71).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

ALL_RULES = frozenset({"feat", "version", "amp", "the", "punct", "accents", "case"})

_FEAT_WORD = r"(?:feat\.?|ft\.?|featuring|with)"
# "(feat. X)" / "[ft. X]" anywhere, or an unbracketed "feat. X" tail. "with"
# only counts inside brackets: unbracketed it's too often part of the title.
_FEAT_BRACKET_RE = re.compile(rf"\s*[(\[]\s*{_FEAT_WORD}\s[^)\]]*[)\]]", re.IGNORECASE)
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
_FEAT_JOIN_RE = re.compile(rf"^\s*[(\[]?\s*{_FEAT_WORD}\s*$", re.IGNORECASE)

# One trailing "(…)", "[…]" or " - …" segment that marks the same song.
_VERSION_WORDS = (
    r"(?:\d{4}\s*)?(?:-\s*)?(?:digital(?:ly)?\s+)?"
    r"(?:re-?master(?:ed)?(?:\s+version)?|deluxe(?:\s+(?:edition|version))?|"
    r"expanded(?:\s+edition)?|bonus\s+track|album\s+version|single\s+version)"
    r"(?:\s*\d{4})?|\d{4}"
)
_VERSION_RE = re.compile(
    rf"\s*(?:[(\[]\s*(?:{_VERSION_WORDS})\s*[)\]]|\s-\s*(?:{_VERSION_WORDS}))\s*$",
    re.IGNORECASE,
)
_APOSTROPHES_RE = re.compile(r"['’ʼ`´]")
_NON_WORD_RE = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class Song:
    track: str
    artists: tuple[str, ...]
    album: str


@dataclass(frozen=True)
class Credit:
    """One artist in a credit, with the phrase that came before it."""

    name: str
    featured: bool = False


def strip_feat(title: str) -> str:
    return _FEAT_TAIL_RE.sub("", _FEAT_BRACKET_RE.sub("", title))


def strip_version(text: str) -> str:
    prev = None
    while prev != text:
        prev, text = text, _VERSION_RE.sub("", text)
    return text


def strip_latin_accents(text: str) -> str:
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)
        if len(base) > 1 and unicodedata.name(base[0], "").startswith("LATIN"):
            out.append("".join(c for c in base if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


def norm_text(text: str | None, rules: frozenset[str] = ALL_RULES) -> str:
    """Rules that apply to every field (punctuation, accents, case)."""
    text = unicodedata.normalize("NFC", text or "")
    if "amp" in rules:
        text = text.replace("&", " and ")
    if "punct" in rules:
        text = _NON_WORD_RE.sub(" ", _APOSTROPHES_RE.sub("", text))
    if "accents" in rules:
        text = strip_latin_accents(text)
    if "case" in rules:
        text = text.casefold()
    return " ".join(text.split())


def norm_title(text: str | None, rules: frozenset[str] = ALL_RULES) -> str:
    """Track or album title."""
    text = text or ""
    if "feat" in rules:
        text = strip_feat(text)
    if "version" in rules:
        text = strip_version(text)
    return norm_text(text, rules)


def norm_artist(name: str, rules: frozenset[str] = ALL_RULES) -> str:
    out = norm_text(name, rules)
    if "the" in rules and out[:4].casefold() == "the " and len(out) > 4:
        out = out[4:]
    return out


def norm_artists(
    credits: Iterable[Credit], rules: frozenset[str] = ALL_RULES
) -> tuple[str, ...]:
    names = {
        norm_artist(c.name, rules)
        for c in credits
        if not (c.featured and "feat" in rules)
    }
    return tuple(sorted(n for n in names if n))


def credits_from_joined(parts: Iterable[tuple[str, str]]) -> list[Credit]:
    """Credits from (name, join phrase after it) pairs, MusicBrainz-style.

    An artist is featured when the join phrase before it is a feat. phrase;
    once one is, every later artist in the credit is too ("A feat. B & C").
    """
    out, featured = [], False
    for name, join in parts:
        out.append(Credit(name, featured))
        featured = featured or bool(_FEAT_JOIN_RE.match(join))
    return out


def normalize(
    track: str | None,
    credits: Iterable[Credit],
    album: str | None,
    rules: frozenset[str] = ALL_RULES,
) -> Song:
    return Song(
        norm_title(track, rules), norm_artists(credits, rules), norm_title(album, rules)
    )
