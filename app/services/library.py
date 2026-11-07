"""File management (#112, user 2026-10-30): the whole library, rename, delete.

The list is what Navidrome indexes; songs Post-Vinyl downloaded show its record
too. A **rename** is two steps: the edited names are looked up again
(``TaggingService.suggest``: ListenBrainz with the admin's token, else a
MusicBrainz search, each only while it's on) and, when a release matches, its
full tag set is suggested; the admin then **applies** it (rebuilt on the
server from its ids, so no tag data is taken from the client) or, when nothing
matched, **strips** every tag and the file moves by the new names. Renames and
deletes are admin-only; this service is reached only through admin routes for
them. Owns nothing: tags are the tagging service's, files the files service's.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.clients.navidrome import Cover
from app.clients.navidrome import Song as LibrarySong
from app.core.file_song import UNKNOWN_ARTIST, names_from_path
from app.core.matcher import parse_title, same_song
from app.services.files import FilesService
from app.services.requests import DEFAULT_PAGE_SIZE, clamp_page
from app.services.tagging import (
    Suggestion,
    TagFailed,
    TaggingService,
    strip_tags,
    write_tags,
)
from app.stores.database import Database
from app.stores.files import FileRecord
from app.stores.users import User

log = logging.getLogger(__name__)

OWNED_HITS = 50  # library songs compared per title


@dataclass(frozen=True)
class LibraryFile:
    id: str  # Navidrome's song id
    title: str
    artist: str
    album: str
    duration: float
    suffix: str
    missing: bool  # gone from disk, still in Navidrome's index
    file_id: int | None  # Post-Vinyl's record, when it downloaded the song
    file_state: str | None
    has_cover: bool = False  # Navidrome has art for it: /cover answers an image
    tag_failed: bool = False  # it moved in untagged: its last tagging failed
    bitrate: int = 0  # kbps; 0 when unknown


@dataclass(frozen=True)
class LibraryPage:
    items: list[LibraryFile]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class Rename:
    """The names a rename looks up; the tags come from what they match."""

    title: str
    artist: str
    album: str = ""


def item(song: LibrarySong, rec: FileRecord | None, failed: set[int]) -> LibraryFile:
    return LibraryFile(
        song.id, song.title, song.artist, song.album, song.duration, song.suffix,
        song.missing, rec.id if rec else None, rec.file_state if rec else None,
        song.has_cover, rec is not None and rec.id in failed, song.bitrate,
    )  # fmt: skip


class LibraryServiceBase(ABC):
    @abstractmethod
    def page(
        self, query: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> LibraryPage: ...

    @abstractmethod
    def owned(self, songs: list[tuple[str, str]]) -> list[bool]:
        """For each (title, artist): is it in the library already?"""

    @abstractmethod
    def suggest(self, admin: User, song_id: str, rename: Rename) -> Suggestion | None:
        """The tag set the new names point at, or None when nothing matched."""

    @abstractmethod
    def apply(
        self, admin: User, song_id: str, recording_id: str, release_id: str
    ) -> LibraryFile | None:
        """Write the suggested tag set, then move the file where it puts it."""

    @abstractmethod
    def strip(
        self, admin: User, song_id: str, title: str, artist: str
    ) -> LibraryFile | None:
        """No match: every tag off, the file moves to ``<artist>/<title>``."""

    @abstractmethod
    def retag(self, admin: User, song_id: str) -> LibraryFile | None:
        """Try tagging again from the file's name; on a match the tags are rewritten.
        TagFailed when nothing matches, and the file stays as it is."""

    @abstractmethod
    def delete(self, song_id: str) -> None: ...

    @abstractmethod
    def cover(self, song_id: str, size: int) -> Cover | None:
        """The song's cover from Navidrome, or None when it has none (#126)."""


class LibraryService(LibraryServiceBase):
    def __init__(
        self, db: Database, files: FilesService, tagging: TaggingService
    ) -> None:
        self._db = db
        self._files = files
        self._tagging = tagging

    def _out(self, song: LibrarySong | None) -> LibraryFile | None:
        if song is None:  # Navidrome doesn't list the new path yet
            return None
        rec = self._files.records([song.id]).get(song.id)
        return item(song, rec, self._files.tag_failed([rec.id] if rec else []))

    def owned(self, songs: list[tuple[str, str]]) -> list[bool]:
        return [self.find(title, artist) is not None for title, artist in songs]

    def find(self, title: str, artist: str) -> str | None:
        """The library's copy of a song by name, whoever got it there. The search
        takes the base title: every word must hit, and the file may word its
        qualifiers differently."""
        # Navidrome's search finds nothing with a "*" in it (live 2026-11-07: Y***'D OUT)
        query = " ".join(parse_title(title).text.replace("*", " ").split())
        hits, _ = self._files.library_page(query, 0, OWNED_HITS)
        return next(
            (
                s.id
                for s in hits
                if not s.missing and same_song(title, artist, s.title, s.artist)
            ),
            None,
        )

    def page(
        self, query: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> LibraryPage:
        page, page_size = clamp_page(page, page_size)
        start = (page - 1) * page_size
        songs, total = self._files.library_page(" ".join(query.split()), start,
                                                start + page_size)  # fmt: skip
        recs = self._files.records([s.id for s in songs])
        failed = self._files.tag_failed([r.id for r in recs.values()])
        return LibraryPage([item(s, recs.get(s.id), failed) for s in songs], total,
                           page, page_size)  # fmt: skip

    def suggest(self, admin: User, song_id: str, rename: Rename) -> Suggestion | None:
        title, artist = " ".join(rename.title.split()), " ".join(rename.artist.split())
        if not title or not artist:
            raise ValueError("a rename needs a title and an artist")
        song = self._files.library_song(song_id)
        self._files.library_path(song)  # refuse before any lookup
        return self._tagging.suggest(admin.id, title, artist, rename.album,
                                     song.duration or None)  # fmt: skip

    def apply(
        self, admin: User, song_id: str, recording_id: str, release_id: str
    ) -> LibraryFile | None:
        path = self._files.library_path(self._files.library_song(song_id))
        tags, cover = self._tagging.tags_for(admin.id, recording_id, release_id)
        write_tags(path, tags, cover)
        rec = self._files.records([song_id]).get(song_id)
        if rec:
            written = {**tags, "cover": cover is not None}
            self._tagging.record_retag(rec.id, recording_id, release_id, written)
        log.info("library %s retagged by %s as %s – %s", song_id, admin.username,
                 tags.get("artist"), tags.get("title"))  # fmt: skip
        return self._out(self._files.re_place(song_id, str(tags.get("title", "")),
                                              str(tags.get("artist", ""))))  # fmt: skip

    def strip(
        self, admin: User, song_id: str, title: str, artist: str
    ) -> LibraryFile | None:
        title, artist = " ".join(title.split()), " ".join(artist.split())
        if not title or not artist:
            raise ValueError("moving untagged needs a title and an artist")
        path = self._files.library_path(self._files.library_song(song_id))
        strip_tags(path)
        log.info("library %s stripped by %s, moving as %s – %s", song_id,
                 admin.username, artist, title)  # fmt: skip
        return self._out(self._files.re_place(song_id, title, artist))

    def retag(self, admin: User, song_id: str) -> LibraryFile | None:
        song = self._files.library_song(song_id)
        self._files.library_path(song)  # refuse before any lookup
        title, artist = names_from_path(song.path)
        if artist == UNKNOWN_ARTIST:
            artist = song.artist
        found = self.suggest(admin, song_id, Rename(title, artist))
        if found is None and song.title and song.artist:
            # a bare "feather nujabes" names no artist: the current names may do
            found = self.suggest(admin, song_id, Rename(song.title, song.artist))
        if found is None:
            raise TagFailed(f"nothing matched {artist} – {title}")
        return self.apply(admin, song_id, found.recording_id, found.release_id)

    def delete(self, song_id: str) -> None:
        self._files.delete_library_file(song_id)

    def cover(self, song_id: str, size: int) -> Cover | None:
        return self._files.cover(song_id, size)
