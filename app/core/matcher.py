"""Is this file the requested song (#90; rules in ``rebuild/design-notes-compilation.md`` §3).

A file is **correct** for a track when:

1. it's an audio file (extension read from the filename: slskd's ``extension``
   field is often empty) and not locked;
2. no excluded version word is in its filename or parent folder ("live" also
   one folder up) unless the song itself carries it;
3. the title's main part is an in-order phrase in the filename; bracketed
   title words may appear anywhere;
4. the main artist is an in-order phrase in the path; names of ≤2 characters
   must be their own path segment;
5. its length is within ±6 s of the song's (the recording the user picked;
   the slack covers silence at the tail, user 2026-10-29). A file without a length is
   ``no_length``, never correct (user, 2026-10-27); a song without a length
   skips the check.

Both sides are folded the same way first (``norm``): case, accents on Latin
letters only, punctuation, "&" as "and", "$" as "s". Moved here from
``scripts/corpus/label.py``, which imports it, so the benchmark and the app
share one matcher.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

AUDIO_EXTS = {
    "flac", "mp3", "m4a", "aac", "ogg", "opus", "wav", "aiff", "aif",
    "alac", "ape", "wv", "wma",
}  # fmt: skip

# Matched as whole words/phrases against the folded filename + parent folder.
EXCLUDED_VERSIONS = (
    "cover", "covers", "covered", "live", "acoustic", "acapella", "acappella",
    "a cappella", "a capella", "mix", "mixes", "mixed", "remix", "remixes",
    "remixed", "rmx", "vip mix", "extended", "solo", "reference", "home session",
    "home sessions", "soundtrack", "ost", "ambient", "edit", "radio edit",
    "karaoke", "nightcore", "sped up", "slowed", "8d audio", "8d", "mashup",
    "bootleg", "tribute", "made famous by", "in the style of", "instrumental",
    "demo", "loop", "flip", "backing track",
    # user, 2026-10-30 (#114 misses + spoken word; broad single words left out)
    "drumless", "bbc session", "bbc sessions", "peel session", "peel sessions",
    "radio session", "radio one session", "radio 1 session", "podcast",
    "audiobook", "voiceover", "voice over",
)  # fmt: skip
# The normal version, though it says "mix".
SAME_SONG_PHRASES = ("original mix",)

# Version words in other languages, read as their English form on both sides.
VERSION_SYNONYMS = {
    "ремикс": "remix", "ремиксы": "remixes", "кавер": "cover", "караоке": "karaoke",
    "акустика": "acoustic", "акустическая": "acoustic", "инструментал": "instrumental",
    "минусовка": "instrumental", "лайв": "live", "концерт": "live", "демо": "demo",
}  # fmt: skip

DURATION_TOLERANCE_S = 6
ALBUM_COMPLETENESS = 0.8

# Suffixes that don't change which song it is: remaster/deluxe/year.
_SAME_SONG_RE = re.compile(
    r"\s*[\(\[\-–]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|deluxe(?: edition)?|"
    r"expanded(?: edition)?|bonus track|single version|album version|"
    r"collectors edition)(?:\s+\d{4})?\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
_APOS_RE = re.compile(r"['’ʼ`´]")
_ACRONYM_RE = re.compile(r"\b(?:\w\.){1,}\w\b\.?")
# Peers write "$uicideboy$" as "suicideboys".
_SYMBOL_MAP = str.maketrans({"$": "s", "@": "a", "€": "e", "£": "l"})
_PAREN_RE = re.compile(r"[(\[{][^)\]}]*[)\]}]")
# A concert date in a folder name: 1998-04-06, 2016.07.02.
_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}([-.])\d{2}\1\d{2}(?!\d)")
# "live" glued to the next word or a year: Livebox, LiveAt, Live1999 (never
# liver/lively/olive).
_JOINED_LIVE_RE = re.compile(r"^live(?:box|at|in|from|on|set|show|tour|\d{2,4})$")


def norm(text: str) -> str:
    """Compare key: case-folded, accents off Latin letters only, punctuation
    and whitespace collapsed, "&" as "and", "$" as "s", dotted acronyms as one
    word (G.O.M.D → gomd). Non-Latin letters stay whole: NFKD on everything
    would turn Cyrillic й into и and split Hangul."""
    out = []
    for ch in unicodedata.normalize("NFC", text or "").casefold():
        base = unicodedata.normalize("NFD", ch)
        if len(base) > 1 and unicodedata.name(base[0], "").startswith("LATIN"):
            ch = "".join(c for c in base if not unicodedata.combining(c))
        out.append(ch)
    text = _APOS_RE.sub("", "".join(out)).replace("&", " and ")
    text = text.translate(_SYMBOL_MAP)
    text = _ACRONYM_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def norm_title(text: str) -> str:
    prev = None
    while prev != text:
        prev, text = text, _SAME_SONG_RE.sub("", text)
    return norm(text)


def split_path(filename: str) -> tuple[list[str], str, str]:
    """Soulseek paths use backslashes; return (folders, stem, extension)."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else ""
    stem, dot, ext = base.rpartition(".")
    return parts[:-1], stem if dot else base, ext.lower() if dot else ""


def is_audio(filename: str) -> bool:
    return split_path(filename)[2] in AUDIO_EXTS


def words(text: str) -> list[str]:
    return [VERSION_SYNONYMS.get(w, w) for w in norm(text).split()]


def has_phrase(haystack: list[str], phrase: str) -> bool:
    needle = phrase.split()
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _contains_all(haystack: list[str], needle: list[str]) -> bool:
    pool = set(haystack)
    return bool(needle) and all(w in pool for w in needle)


def version_hit(local: list[str], own: list[str]) -> str | None:
    text = " ".join(local)
    for phrase in SAME_SONG_PHRASES:
        text = f" {text} ".replace(f" {phrase} ", " ")
    local = text.split()
    for phrase in EXCLUDED_VERSIONS:
        if has_phrase(local, phrase) and not has_phrase(own, phrase):
            return phrase
    if "live" not in own and any(_JOINED_LIVE_RE.match(w) for w in local):
        return "live"
    return None


def excluded_word(filename: str, title: str, album: str = "") -> str | None:
    """The first excluded version word in the file's name or parent folder
    that the song itself doesn't carry. Other words higher up were mostly
    category folders (measured 2026-10-27), but "live" counts in any folder,
    and so does a folder with a full date ("1998-04-06 - Pacific Coliseum"):
    concert recordings sit under those (user, 2026-10-28, live run on #90)."""
    folders, stem, _ = split_path(filename)
    local = words(stem) + (words(folders[-1]) if folders else [])
    own = words(title) + words(album)
    if hit := version_hit(local, own):
        return hit
    if "live" in own:
        return None
    if any(version_hit(words(f), own) == "live" for f in folders):
        return "live"
    return "live" if any(_DATE_RE.search(f) for f in folders) else None


def title_matches(stem: str, title: str) -> bool:
    """The title's main part as an in-order phrase in the filename; words in
    its brackets anywhere ("Old Town Road (Remix)" matches "Old Town Road
    (feat. Billy Ray Cyrus) [Remix]"). Loose words would let "South City
    Midnight Lady" pass for "Midnight City"."""
    main = words(norm_title(_PAREN_RE.sub(" ", title)))
    extra = words(norm_title(" ".join(_PAREN_RE.findall(title))))
    got = words(stem)
    if not main:  # title is all brackets
        return _contains_all(got, extra)
    return has_phrase(got, " ".join(main)) and (not extra or _contains_all(got, extra))


def artist_matches(folders: list[str], stem: str, artist: str) -> bool:
    """The artist as an in-order phrase in the path. A name of ≤2 characters
    ("xx", "X", "U2") must be its own segment, or "The xx" matches a date like
    "1975 09 xx Memphis"."""
    want = words(artist)
    if not want:
        return False
    segments = folders + [stem]
    if len("".join(want)) > 2:
        return has_phrase(words(" ".join(segments)), " ".join(want))
    for seg in segments:
        for part in re.split(r"\s+[-–—]\s+|[_\\/]|^\d+[.)]?\s+", seg):
            if norm(part) == " ".join(want):
                return True
        if norm(seg) == " ".join(want):
            return True
    return False


@dataclass(frozen=True)
class Song:
    title: str
    artist: str  # main artist
    album: str = ""
    length_s: float | None = None


def label_file(
    filename: str, length_s: float | None, song: Song, *, locked: bool = False
) -> str:
    """``correct``, or why not: ``locked``, ``not_audio``, ``excluded:<word>``,
    ``no_title``, ``no_artist``, ``no_length``, ``duration``."""
    folders, stem, ext = split_path(filename)
    if locked:
        return "locked"
    if ext not in AUDIO_EXTS:
        return "not_audio"
    if word := excluded_word(filename, song.title, song.album):
        return f"excluded:{word}"
    if not title_matches(stem, song.title):
        return "no_title"
    if not artist_matches(folders, stem, song.artist):
        return "no_artist"
    if song.length_s:
        if length_s is None:
            return "no_length"
        if abs(int(length_s) - int(song.length_s)) > DURATION_TOLERANCE_S:
            return "duration"
    return "correct"


def is_match(
    filename: str, length_s: float | None, song: Song, *, locked: bool = False
) -> bool:
    return label_file(filename, length_s, song, locked=locked) == "correct"


def album_folder_ok(
    folder: str,
    filenames: Iterable[str],
    album: str,
    artist: str,
    track_count: int | None,
) -> bool:
    """One folder holds the album: its path has the album and artist words
    (artist skipped for "Various Artists"), no excluded word in its own name,
    and ≥80% of the album's track count in audio files."""
    got = words(folder)
    own = words(album)
    if not _contains_all(got, own):
        return False
    if artist.casefold() != "various artists" and not _contains_all(got, words(artist)):
        return False
    if version_hit(words(folder.replace("\\", "/").rsplit("/", 1)[-1]), own):
        return False
    audio = sum(1 for f in filenames if is_audio(f))
    return audio >= max(1, round(ALBUM_COMPLETENESS * (track_count or 1)))
