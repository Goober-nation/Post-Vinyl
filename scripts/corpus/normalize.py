"""Query names for slskd from catalogue names (#70, rules decided in #71).

Turns a song's MusicBrainz/ListenBrainz names into the names a slskd query is
built from. slskd results decide every choice (user, 2026-10-27); the numbers
are in ``agents_memory/rebuild/search-test-v2.md``.

Kept (each switchable, so the report can show what it changes):

- ``feat``           drop "(feat. X)", "[ft. X]", "(with X)" and a "feat. X" tail
                     from titles
- ``title_credits``  drop a credited artist written into the title with
                     "+", "x", "×", "&" or "," ("Stateside + Zara Larsson")
- ``the``            drop a leading "The" from the query artist

The query artist is the **main artist only** (first credit that isn't
featured): join words like "feat.", "with", "&" cut slskd results hardest.
The full credit is kept alongside for the matcher.

Everything else stays as the source writes it: original script (Высоцкий,
이은미), accents (Café), case, punctuation, "&", version suffixes. Soulseek
ignores case and punctuation and matches accents loosely; folding them for
comparing files is the matcher's job, not this module's.

Pure and stdlib-only; moves to ``app/core/normalize.py`` once settled.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

ALL_RULES = frozenset({"feat", "title_credits", "the"})

_FEAT_WORD = r"(?:feat\.?|ft\.?|featuring|with)"
# "(feat. X)" / "[ft. X]" / "(with X)" anywhere, or an unbracketed "feat. X"
# tail. Unbracketed "with" is too often part of the title to count.
_FEAT_BRACKET_RE = re.compile(rf"\s*[(\[]\s*{_FEAT_WORD}\s[^)\]]*[)\]]", re.IGNORECASE)
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
_FEAT_JOIN_RE = re.compile(rf"^\s*[(\[]?\s*{_FEAT_WORD}\s*$", re.IGNORECASE)
_TITLE_CREDIT_SEP = r"\s*(?:\+|×|,|&|\s[xX])\s+"
_THE_RE = re.compile(r"^the\s+(?=\S)", re.IGNORECASE)


@dataclass(frozen=True)
class Credit:
    """One artist in a credit; ``featured`` when a feat. phrase came before it."""

    name: str
    featured: bool = False


@dataclass(frozen=True)
class QueryNames:
    track: str
    artist: str  # main artist, for the query
    album: str
    credit: str  # full credit as the source writes it, for the matcher


def squash(text: str | None) -> str:
    return " ".join((text or "").split())


def strip_feat(title: str) -> str:
    return _FEAT_TAIL_RE.sub("", _FEAT_BRACKET_RE.sub("", title))


def strip_title_credits(title: str, names: Iterable[str]) -> str:
    """Drop "+ X" / "x X" / "& X" / ", X" tails where X is a credited artist."""
    prev = None
    while prev != title:
        prev = title
        for name in names:
            rx = rf"{_TITLE_CREDIT_SEP}{re.escape(name)}\s*$"
            title = re.sub(rx, "", title, flags=re.IGNORECASE)
    return title


def query_title(
    title: str | None, credits: Iterable[Credit] = (), rules: frozenset[str] = ALL_RULES
) -> str:
    out = squash(title)
    if "feat" in rules:
        out = strip_feat(out)
    if "title_credits" in rules:
        out = strip_title_credits(out, [c.name for c in credits])
    return squash(out)


def main_artist(credits: Iterable[Credit], rules: frozenset[str] = ALL_RULES) -> str:
    credits = list(credits)
    main = next((c for c in credits if not c.featured), credits[0] if credits else None)
    out = squash(main.name if main else "")
    if "the" in rules:
        out = _THE_RE.sub("", out)
    return out


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


def query_names(
    track: str | None,
    credits: Iterable[Credit],
    album: str | None,
    credit: str | None = None,
    rules: frozenset[str] = ALL_RULES,
) -> QueryNames:
    credits = list(credits)
    return QueryNames(
        track=query_title(track, credits, rules),
        artist=main_artist(credits, rules),
        album=squash(album),
        credit=squash(credit) or ", ".join(c.name for c in credits),
    )
