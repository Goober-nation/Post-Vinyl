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

from app.clients.navidrome import Song as LibrarySong
from app.services.encryption import SecretBox
from app.services.files import FilesService
from app.services.requests import DEFAULT_PAGE_SIZE, clamp_page
from app.services.tagging import Suggestion, TaggingService, strip_tags, write_tags
from app.stores.database import Database
from app.stores.files import FileRecord
from app.stores.users import User

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class LibraryPage:
    items: list[LibraryFile]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class Rename:
    """The core tag set an admin may change (user, 2026-10-30)."""

    title: str
    artist: str
    album_artist: str = ""
    album: str = ""
    year: str = ""
    track: int | None = None
    disc: int | None = None


def item(song: LibrarySong, rec: FileRecord | None) -> LibraryFile:
    return LibraryFile(
        song.id, song.title, song.artist, song.album, song.duration, song.suffix,
        song.missing, rec.id if rec else None, rec.file_state if rec else None,
    )  # fmt: skip


class LibraryServiceBase(ABC):
    @abstractmethod
    def page(
        self, query: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> LibraryPage: ...

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
    def delete(self, song_id: str) -> None: ...


class LibraryService(LibraryServiceBase):
    def __init__(
        self, db: Database, files: FilesService, tagging: TaggingService, box: SecretBox
    ) -> None:
        self._db = db
        self._files = files
        self._tagging = tagging
        self._box = box

    def _out(self, song: LibrarySong | None) -> LibraryFile | None:
        if song is None:  # Navidrome doesn't list the new path yet
            return None
        return item(song, self._files.records([song.id]).get(song.id))

    def page(
        self, query: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> LibraryPage:
        page, page_size = clamp_page(page, page_size)
        start = (page - 1) * page_size
        songs, total = self._files.library_page(" ".join(query.split()), start,
                                                start + page_size)  # fmt: skip
        recs = self._files.records([s.id for s in songs])
        return LibraryPage([item(s, recs.get(s.id)) for s in songs], total, page,
                           page_size)  # fmt: skip

    def suggest(self, admin: User, song_id: str, rename: Rename) -> Suggestion | None:
        title, artist = " ".join(rename.title.split()), " ".join(rename.artist.split())
        if not title or not artist:
            raise ValueError("a rename needs a title and an artist")
        song = self._files.library_song(song_id)
        self._files.library_path(song)  # refuse before any lookup
        token = self._box.decrypt(admin.lb_token_enc) if admin.lb_token_enc else None
        return self._tagging.suggest(admin.id, title, artist, rename.album,
                                     song.duration or None, token)  # fmt: skip

    def apply(
        self, admin: User, song_id: str, recording_id: str, release_id: str
    ) -> LibraryFile | None:
        path = self._files.library_path(self._files.library_song(song_id))
        tags, cover = self._tagging.tags_for(admin.id, recording_id, release_id)
        write_tags(path, tags, cover)
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

    def delete(self, song_id: str) -> None:
        self._files.delete_library_file(song_id)
