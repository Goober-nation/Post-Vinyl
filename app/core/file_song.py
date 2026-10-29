"""A song's names from a peer's file path, for files picked in the slskd tab (#108).

Search results carry no tags, so the path is all there is: ``03. Artist -
Title`` or ``Artist - Album - 03 - Title`` names both; otherwise the title is the file name and the artist the
folder above the album (``Artist/Album/03 Title``) or the ``Artist`` of an
``Artist - Album`` folder. Disc folders (``CD3``, ``Disc 2``) are skipped.
A guess: tagging only accepts a ListenBrainz match whose credit has this
artist and whose length fits, so a wrong guess stays untagged, not mistagged.
"""

from __future__ import annotations

import re

from app.core.matcher import split_path

UNKNOWN_ARTIST = "Unknown Artist"

# "03", "03.", "1-03", "03 -", "A1"; never a whole name ("1999")
_TRACK_NO_RE = re.compile(
    r"^\s*(?:[A-D]?\d{1,2}[-.])?\d{1,3}(?:\s*[-._)]\s*|\s+)(?=\S)"
)
_DISC_RE = re.compile(r"^(?:cd|disc|disk)\s*\d+$", re.IGNORECASE)
_NUM_RE = re.compile(r"[A-D]?\d{1,3}")
_SEP = " - "


def names_from_path(filename: str) -> tuple[str, str]:
    """``(title, artist)``; the artist is UNKNOWN_ARTIST when the path has none."""
    folders, stem, _ = split_path(filename)
    stem = _TRACK_NO_RE.sub("", stem, count=1).strip()
    parts = [p.strip() for p in stem.split(_SEP)]
    if len(parts) >= 2 and all(parts):
        # "Artist - Album - 02 - Title": the title follows the last bare number
        # that has something after it (a trailing "08A - 154" is key and BPM)
        nums = [i for i, p in enumerate(parts[:-1]) if _NUM_RE.fullmatch(p)]
        if not nums:
            return _SEP.join(parts[1:]), parts[0]
        stem = _SEP.join(parts[nums[-1] + 1 :])
        if nums[-1] > 0:
            return stem, parts[0]
    folders = [f for f in folders if not _DISC_RE.match(f.strip())]
    album_folder = folders[-1] if folders else ""
    if _SEP in album_folder:
        artist = album_folder.split(_SEP, 1)[0].strip()
    elif len(folders) >= 2:
        artist = folders[-2].strip()
    else:
        artist = ""
    return stem or filename, artist or UNKNOWN_ARTIST
