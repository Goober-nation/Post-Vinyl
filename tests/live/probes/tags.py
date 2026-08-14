"""
`TagProbe` — what is actually written in the file, read with mutagen.

Never asks beets what it thinks it wrote. beets' library database and the
bytes on disk are two different claims, and the whole reason S8 exists is
that they can disagree: a row can say `albumartist = Nujabes` while the file
says `Nujabes ft. Cise Starr, Akin`, and it is the file that Navidrome
indexes and the user sees.

The grading logic (`grade_tags`) is a pure function over a `TrackTags`, so
it is unit-tested without a stack, without mutagen, and without a live tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.live.probes.contract import TagProbe, TrackTags
from tests.live.probes.naming import (
    artist_key,
    artist_matches,
    has_feat,
    strip_feat,
    text_key,
)

#: Extensions the probes treat as audio. Shared with `FsProbe`.
AUDIO_EXTS = frozenset(
    {
        ".flac",
        ".mp3",
        ".m4a",
        ".mp4",
        ".aac",
        ".alac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aiff",
        ".aif",
        ".wma",
        ".ape",
        ".wv",
    }
)


class TagReadError(RuntimeError):
    """mutagen could not parse the file. Usually means truncated or not audio."""


# Candidate tag keys per logical field, in priority order. One flat table
# covers Vorbis comments (FLAC/Ogg, lowercase keys), ID3 (frame ids) and
# MP4 atoms, because all three tag objects are dict-like and simply miss the
# keys that don't belong to them.
_KEY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "albumartist": ("albumartist", "album artist", "ALBUMARTIST", "TPE2", "aART"),
    "artist": ("artist", "ARTIST", "TPE1", "\xa9ART"),
    "album": ("album", "ALBUM", "TALB", "\xa9alb"),
    "title": ("title", "TITLE", "TIT2", "\xa9nam"),
    "track": ("tracknumber", "track", "TRACKNUMBER", "TRCK", "trkn"),
    "mb_trackid": (
        "musicbrainz_trackid",
        "MUSICBRAINZ_TRACKID",
        "UFID:http://musicbrainz.org",
        "TXXX:MusicBrainz Track Id",
        "----:com.apple.iTunes:MusicBrainz Track Id",
    ),
    "mb_albumid": (
        "musicbrainz_albumid",
        "MUSICBRAINZ_ALBUMID",
        "TXXX:MusicBrainz Album Id",
        "----:com.apple.iTunes:MusicBrainz Album Id",
    ),
}


def _coerce(value: Any) -> str | None:
    """Flatten whatever a mutagen tag object hands back into a string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return _coerce(value[0])
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip() or None
    # ID3's UFID frame carries the MusicBrainz recording id as raw bytes on
    # `.data`; str() on it would give the repr, not the id.
    data = getattr(value, "data", None)
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace").strip() or None
    text = getattr(value, "text", None)
    if text is not None:
        return _coerce(text)
    return str(value).strip() or None


def _first(tags: Any, field: str) -> str | None:
    if tags is None:
        return None
    for key in _KEY_CANDIDATES[field]:
        try:
            value = tags.get(key)
        except (TypeError, KeyError, ValueError):
            value = None
        coerced = _coerce(value)
        if coerced:
            return coerced
    return None


def _as_track_number(raw: str | None) -> int | None:
    """`"20"`, `"20/22"` and MP4's `(20, 22)` all mean track 20."""
    if not raw:
        return None
    head = raw.replace("(", "").replace(")", "").split("/")[0].split(",")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


class LiveTagProbe(TagProbe):
    """Reads tags off the real files on the host filesystem."""

    def read(self, path: Path) -> TrackTags:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        # Imported lazily so the ordinary (non-live) suite can import this
        # module — and unit-test `grade_tags` — without mutagen installed.
        import mutagen

        try:
            audio = mutagen.File(str(path))
        except Exception as exc:  # mutagen raises a zoo of per-format errors
            raise TagReadError(f"{path}: {exc}") from exc
        if audio is None:
            raise TagReadError(f"{path}: not a recognised audio file")

        tags = audio.tags
        info = getattr(audio, "info", None)
        mime = getattr(audio, "mime", None) or []
        fmt = mime[0].split("/")[-1] if mime else path.suffix.lstrip(".").lower()

        return TrackTags(
            path=path,
            albumartist=_first(tags, "albumartist"),
            artist=_first(tags, "artist"),
            album=_first(tags, "album"),
            title=_first(tags, "title"),
            track=_as_track_number(_first(tags, "track")),
            mb_trackid=_first(tags, "mb_trackid"),
            mb_albumid=_first(tags, "mb_albumid"),
            duration_s=round(getattr(info, "length", 0.0) or 0.0, 3) or None,
            bitrate=getattr(info, "bitrate", None),
            format=fmt,
        )

    def grade(self, path: Path, track: Any) -> tuple[bool, str]:
        path = Path(path)
        if not path.exists():
            return False, f"no file at {path} — nothing was written there"
        try:
            tags = self.read(path)
        except TagReadError as exc:
            return False, (
                f"{path.name} could not be read as audio ({exc}) — the file is "
                f"most likely truncated or is not audio at all"
            )
        return grade_tags(tags, track)


def grade_tags(tags: TrackTags, track: Any) -> tuple[bool, str]:
    """S8: do these tags match the corpus expectation?

    Pure: takes a `TrackTags` and a `tests.live.corpus.Track`, touches no
    disk. The returned reason is quoted verbatim in the report, so it names
    both what was found and what was wanted, in that order, in one sentence
    a person can act on.

    What is graded, and why:

    - **albumartist** — must be the canonical artist and must carry **no**
      featuring clause. This is the field the folder name is built from, so
      a wrong value here is also a wrong path on disk.
    - **title** — must match exactly (modulo case, accents and punctuation).
      Nothing is stripped: "Feather [Explicit]" is a *wrong* title, not a
      cosmetic variant, and the corpus picked that track specifically to
      catch it.
    - **album** — same comparison, and skipped entirely when the corpus
      marks the album as ambiguous (`expect_album is None`).
    - **artist** — only has to *contain* the expected artist; a featuring
      clause is legitimate here, and this is the field it belongs in.
    - **MusicBrainz recording id** — reported, never required. An unmatched
      (`asis`) import legitimately has none.
    """
    problems: list[str] = []
    notes: list[str] = []

    expected_albumartist = track.expect_albumartist
    actual_albumartist = tags.albumartist

    if not actual_albumartist:
        problems.append(
            f"albumartist is empty but should be '{expected_albumartist}' — with no "
            f"albumartist the file cannot be filed under an artist folder at all"
        )
    elif has_feat(actual_albumartist):
        base = strip_feat(actual_albumartist)
        problems.append(
            f"albumartist is '{actual_albumartist}' but should be "
            f"'{expected_albumartist}' — a featuring clause must never reach "
            f"albumartist, it creates a second folder for the same artist "
            f"(here '{actual_albumartist}' alongside '{base}')"
        )
    elif artist_key(actual_albumartist) != artist_key(expected_albumartist):
        problems.append(
            f"albumartist is '{actual_albumartist}' but should be "
            f"'{expected_albumartist}'"
        )
    elif actual_albumartist != expected_albumartist:
        notes.append(
            f"albumartist '{actual_albumartist}' differs from "
            f"'{expected_albumartist}' only by case or punctuation"
        )

    if not tags.title:
        problems.append(f"title is empty but should be '{track.title}'")
    elif text_key(tags.title) != text_key(track.title):
        problems.append(f"title is '{tags.title}' but should be '{track.title}'")

    if track.expect_album is None:
        notes.append(
            f"album not graded — the corpus marks this track's album as "
            f"ambiguous (file says '{tags.album or ''}')"
        )
    elif not tags.album:
        problems.append(f"album is empty but should be '{track.expect_album}'")
    elif text_key(tags.album) != text_key(track.expect_album):
        problems.append(
            f"album is '{tags.album}' but should be '{track.expect_album}'"
        )

    if not artist_matches(tags.artist, expected_albumartist):
        problems.append(
            f"artist is '{tags.artist or ''}' but should credit "
            f"'{expected_albumartist}'"
        )

    if tags.mb_trackid:
        notes.append(f"MusicBrainz recording {tags.mb_trackid}")
    else:
        notes.append(
            "no MusicBrainz recording id — imported as-is, which is allowed "
            "but means nothing cross-checked the metadata"
        )

    if tags.track is None:
        notes.append("no track number")

    tail = f" ({'; '.join(notes)})" if notes else ""
    if problems:
        return False, f"{tags.path.name}: " + "; ".join(problems) + tail
    return True, (
        f"{tags.path.name}: tags match the corpus — albumartist="
        f"'{tags.albumartist}', album='{tags.album}', title='{tags.title}', "
        f"track={tags.track}, {tags.format}{tail}"
    )
