"""Search queries for a song or album (#91; ``rebuild/design-notes-compilation.md`` §4–5).

Track requests run **L0 → TA → T** (user, 2026-10-28, from search test v2,
#77); the next query runs only when the previous one found no helpful peer:

- ``L0``: longest title word + longest artist word, with a bracket qualifier
  ("remix", "live") in between when the title has one
- ``TA``: full title + main artist
- ``T``:  full title

Album requests use ``LW`` (longest album word + longest artist word); when the
album name is unusable (self-titled, generic, digits or one character) the
full album name + artist is used instead.

Names come in already normalized (``core/normalize.py``). Word picking keeps
each word's own script and accents: a Latin word needs 3+ letters, a word in
another script 2+. Ported from ``scripts/corpus/plan.py``.
"""

from __future__ import annotations

import re
import unicodedata

# Ported from legacy/app/services/query_builder.py via scripts/corpus/plan.py.
STOP_WORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "to", "at", "by", "is", "or", "for", "with",
    "vs", "and", "feat", "ft", "featuring", "presents", "intro", "produced", "prod",
    "mixed", "w", "remix", "rmx", "edit", "edition", "version", "original", "extended",
    "mix", "radio", "instrumental", "inst", "acoustic", "acoust", "live", "demo",
    "explicit", "clean", "bonus", "alternate", "alt", "cover", "single", "album", "ep",
    "lp", "deluxe", "special", "limited", "compilation", "comp", "collection",
    "anthology", "greatest", "hits", "best", "release", "reissue", "remastered",
    "remaster", "anniversary", "complete", "ultimate", "essential", "definitive",
    "part", "pt", "vol", "volume", "disc", "disk", "cd", "vinyl", "digital", "side",
    "chapter", "act", "scene", "ost", "soundtrack", "score", "theme", "opening",
    "ending", "background", "bgm", "insert", "character", "image", "track", "unknown",
    "various", "artist", "music", "song", "audio", "file", "type", "beat", "beats",
    "bpm", "kbps", "hz", "khz", "mp3", "flac", "m4a", "wav", "ogg", "aac", "wma",
    "lossless", "hires", "hd", "master", "stereo", "mono",
})  # fmt: skip
# Version qualifiers that can go into L0.
QUALIFIERS = (
    "instrumental", "acoustic", "remastered", "nightcore", "extended", "remaster",
    "acapella", "karaoke", "bootleg", "tribute", "slowed", "mashup", "remix", "radio",
    "cover", "demo", "edit", "live", "inst", "loop", "flip", "rmx",
)  # fmt: skip
# Album names that say nothing about which album (§5), beyond STOP_WORDS.
GENERIC_ALBUM_WORDS = frozenset({
    "hits", "best", "singles", "collection", "unplugged", "live", "greatest",
    "essentials", "gold", "platinum", "number", "ones", "love", "songs",
})  # fmt: skip

_FEAT_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\b", re.IGNORECASE)
_BRACKET_RE = re.compile(r"[(\[]([^)\]]*)[)\]]")
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def is_latin(word: str) -> bool:
    return all(unicodedata.name(c, "").startswith("LATIN") or c.isdigit() for c in word)


def select_words(text: str) -> list[str]:
    """Query words of a field, longest first, in the field's own script.

    The feat. clause and bracket contents are left out; stop words, version
    qualifiers and digit-only words are dropped.
    """
    text = _BRACKET_RE.sub(" ", _FEAT_RE.split(text or "", maxsplit=1)[0])
    out: list[str] = []
    for word in _WORD_RE.findall(unicodedata.normalize("NFC", text)):
        low = word.casefold()
        if low in STOP_WORDS or word.isdigit() or low in out:
            continue
        if len(word) < (3 if is_latin(word) else 2):
            continue
        out.append(low)
    return sorted(out, key=len, reverse=True)


def bracket_qualifier(title: str) -> str | None:
    """First version qualifier inside the title's brackets ("remix")."""
    for group in _BRACKET_RE.findall(title or ""):
        found = {w.casefold() for w in _WORD_RE.findall(group)}
        for q in QUALIFIERS:
            if q in found:
                return q
    return None


def _join(*parts: str) -> str:
    return " ".join(dict.fromkeys(w for p in parts for w in p.split()))


def track_queries(title: str, artist: str) -> list[tuple[str, str]]:
    """``(shape, query)`` in the order to try, duplicates dropped."""
    tw, aw = select_words(title), select_words(artist)
    out: list[tuple[str, str]] = []

    def add(shape: str, query: str) -> None:
        query = " ".join(query.split())
        if query and query.casefold() not in {q.casefold() for _, q in out}:
            out.append((shape, query))

    if tw:
        add("L0", _join(tw[0], bracket_qualifier(title) or "", *aw[:1]))
    elif aw:
        add("L0", aw[0])  # no usable title word: the artist word alone
    add("TA", f"{title} {artist}")
    add("T", title)
    return out


def _album_words(album: str) -> list[str]:
    return [w for w in select_words(album) if w not in GENERIC_ALBUM_WORDS]


def usable_album(album: str, artist: str) -> bool:
    """False for self-titled (longest album word is an artist word), generic
    ("Greatest Hits", "Unplugged") and digit/one-character names ("7", "21")."""
    aw = _album_words(album)
    return bool(aw) and aw[0] not in select_words(artist)


def album_query(album: str, artist: str) -> tuple[str, str]:
    """``LW``, or ``BA`` (full album + artist) when the album is unusable."""
    if usable_album(album, artist):
        return "LW", _join(_album_words(album)[0], *select_words(artist)[:1])
    if album.casefold() == artist.casefold():
        return "BA", album
    return "BA", " ".join(f"{album} {artist}".split())
