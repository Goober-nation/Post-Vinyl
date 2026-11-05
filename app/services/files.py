"""Files: verify, move into the library, link to Navidrome (#103).

Owns ``file_state`` (``tagged`` is the tagging service's). Decided with the
user, 2026-10-29:

- **Verify**: the file exists, opens as audio and, when the song's length is
  known, lasts within the matcher's tolerance of it. A file that fails keeps
  its error code in ``verification`` and stays ``downloaded``; nothing moves it.
- **Move** into ``library/<Artist>/<Album> (<Year>)/[Disc N/]<NN> <Title>.<ext>``
  built from the file's own tags once tagging wrote them (known true); an
  untagged file goes to ``library/<Artist>/<Title>.<ext>`` from the song's
  normalized names. **Never overwrite**: another file at the target gets
  " (2)", " (3)", … A replaced song's old copy (the row still carries its
  ``navidrome_id``) is looked up in Navidrome and deleted, only inside the
  library, before the new one moves in.
- **Index**: scan Navidrome, find the moved file by its path and store the
  ``navidrome_id``; Navidrome owns the path from then on. Navidrome's music
  folder may be the library or the music folder above it (user, 2026-11-05): a
  path is read against both, and with the wider root the downloads folder goes
  into ``.ndignore`` so they are never indexed.
- Navidrome is reached as the first Post-Vinyl admin, with the Navidrome
  password stored (encrypted) at their login.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import threading
import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC

from app.clients.navidrome import (
    Cover,
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeError,
    NavidromeNotFound,
    Session,
)
from app.clients.navidrome import Song as LibrarySong
from app.config import FilesSettings
from app.core import states
from app.core.matcher import DURATION_TOLERANCE_S
from app.services.encryption import DecryptError, SecretBox
from app.services.navidrome_login import NavidromeLogins
from app.stores.database import Database
from app.stores.files import FileRecord, FilesStore
from app.stores.pulls import PullsStore
from app.stores.songs import Song, SongsStore
from app.stores.users import UsersStore

log = logging.getLogger(__name__)

SCAN_WAIT_S = 120
SCAN_POLL_S = 2
# a file smaller than this share of bitrate × length is cut off (tags only add)
MIN_SIZE_RATIO = 0.8
MAX_SEGMENT = 120  # characters per folder/file name
_UNSAFE = re.compile(r"[\x00-\x1f/\\]")
ALBUM_SONGS = 200  # library songs read when asking which releases an album has
LAYOUT_SAMPLE = 20  # songs read to tell Navidrome's root from a few missing files
_COVERS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class LibraryFileNotFound(LookupError):
    """No such library song, or its file isn't inside the library."""


class LayoutMismatch(NavidromeError):
    """Navidrome's paths don't lead to our library: the mounts disagree."""


class NoAdmin(Exception):
    """No Post-Vinyl admin to reach Navidrome as (run setup first)."""


def segment(text: str, fallback: str) -> str:
    """One safe path segment: no slashes or control characters, no leading dot."""
    clean = " ".join(_UNSAFE.sub("_", text or "").split()).strip(". ")
    return (clean or fallback)[:MAX_SEGMENT].rstrip(". ") or fallback


def _first(tags: Any, key: str) -> str:
    value = tags.get(key) if tags else None
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _number(text: str) -> int | None:
    head = text.split("/")[0].strip()
    return int(head) if head.isdigit() else None


def layout(tags: Any, song: Song, ext: str, tagged: bool) -> Path:
    """Library-relative path for a file (see module doc)."""
    title = _first(tags, "title") if tagged else ""
    artist = (_first(tags, "albumartist") or _first(tags, "artist")) if tagged else ""
    album = _first(tags, "album") if tagged else ""
    title = title or song.query_title or song.title
    artist = artist or song.main_artist or song.artist_name
    name = segment(title, "Unknown Title")
    if not album:
        return Path(segment(artist, "Unknown Artist")) / f"{name}.{ext}"
    year = _first(tags, "date")[:4]
    folder = f"{album} ({year})" if year.isdigit() else album
    track = _number(_first(tags, "tracknumber"))
    disc_text = _first(tags, "discnumber")
    disc, discs = (
        _number(disc_text),
        _number(disc_text.split("/")[1]) if "/" in disc_text else None,
    )
    discs = discs or _number(_first(tags, "disctotal") or _first(tags, "totaldiscs"))
    out = Path(segment(artist, "Unknown Artist")) / segment(folder, "Unknown Album")
    if disc and discs and discs > 1:
        out /= f"Disc {disc}"
    stem = f"{track:02d} {name}" if track else name
    return out / f"{segment(stem, name)}.{ext}"


def free_path(target: Path) -> Path:
    """``target``, or "<stem> (2)<ext>", "(3)"… when something is there."""
    n = 2
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        n += 1
    return candidate


def move_file(src: Path, target: Path) -> None:
    """Move without ever showing a partial file at ``target``.

    Downloads and library may be separate mounts, where a rename fails. The copy
    then goes under a name Navidrome doesn't index, and the source goes last, so
    a crash or a full disk at any point leaves the source whole.
    """
    try:
        os.rename(src, target)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    partial = target.with_name(f"{target.name}.partial")
    try:
        with src.open("rb") as source, partial.open("wb") as copy:
            shutil.copyfileobj(source, copy)
            copy.flush()
            os.fsync(copy.fileno())
        shutil.copystat(src, partial)
        os.replace(partial, target)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    src.unlink()


def _leftover(path: Path) -> bool:
    """A cover or macOS file, worthless once its folder has no audio left."""
    return path.is_file() and (
        path.name == ".DS_Store"
        or path.name.startswith("._")
        or path.suffix.lower() in _COVERS
    )


def _numbered(path: Path, target: Path) -> bool:
    """``path`` is one of ``target``'s "(2)", "(3)"… names."""
    return (
        path.parent == target.parent
        and path.suffix == target.suffix
        and re.fullmatch(re.escape(target.stem) + r" \(\d+\)", path.stem) is not None
    )


class FilesServiceBase(ABC):
    @abstractmethod
    def verify(self, file_id: int) -> bool: ...

    @abstractmethod
    def move(self, file_id: int) -> bool: ...

    @abstractmethod
    def index(self, file_id: int, scan: bool = True) -> bool:
        """(Scan and) link; False while Navidrome doesn't list the file yet."""

    @abstractmethod
    def scan(self) -> None:
        """One Navidrome scan, waited for (the pipeline batches its moves)."""

    @abstractmethod
    def discard_unverifiable(self, file_id: int, older_than_s: int) -> bool:
        """Delete a download that failed verification long enough ago."""

    @abstractmethod
    def check_missing(self, file_id: int) -> bool:
        """Mark the file ``missing`` when it vanished; True when it did."""


class FilesService(FilesServiceBase):
    def __init__(
        self,
        db: Database,
        navidrome: NavidromeClientBase,
        box: SecretBox,
        settings: FilesSettings,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._nd = navidrome
        self._box = box
        self._logins = NavidromeLogins(db, navidrome, box)
        self._music = Path(settings.music_dir)
        self._downloads = self._music / settings.slskd_downloads_dir
        self._downloads_top = Path(settings.slskd_downloads_dir).parts[0]
        self._ignore_checked = False
        self._library = self._music / settings.library_dir
        self._clock = clock
        self._sleep = sleep
        self._files = FilesStore()
        self._songs = SongsStore()
        self._pulls = PullsStore()
        self._session: Session | None = None
        # moves, renames, deletes and the missing check one at a time: the check
        # must never see a file between its move and our record of it (live #114)
        self._placing = threading.Lock()

    def _now(self) -> int:
        return int(self._clock())

    def _get(self, file_id: int) -> tuple[FileRecord, Song]:
        with self._db.transaction() as conn:
            rec = self._files.get(conn, file_id)
            song = self._songs.get(conn, rec.song_id) if rec else None
        if rec is None or song is None:
            raise LookupError(f"file {file_id}")
        return rec, song

    def _move_state(self, rec: FileRecord, new: str) -> bool:
        with self._db.transaction(write=True) as conn:
            current = self._files.get(conn, rec.id)
            if current is None or current.file_state != rec.file_state:
                return False  # replaced or changed meanwhile
            self._files.set_state(conn, rec.id, new, self._now())
        return True

    def _source_path(self, rec: FileRecord) -> Path:
        """Before the move: inside slskd's downloads; after: inside the music dir."""
        if rec.file_state in ("moved", "indexed"):
            return self._music / rec.file_location
        return self._downloads / rec.file_location

    # ------------------------------------------------------------ verify

    def verify(self, file_id: int) -> bool:
        rec, song = self._get(file_id)
        path = self._source_path(rec)
        error = self._check(path, song)
        with self._db.transaction(write=True) as conn:
            self._files.set_verification(conn, rec.id, error or "success", self._now())
        if error:
            log.warning("file %s failed verification: %s (%s)", rec.id, error, path)
            return False
        return self._move_state(rec, states.file_verified(rec.file_state))

    def _check(self, path: Path, song: Song) -> str | None:
        if not path.is_file():
            return "not_found"
        try:
            audio = mutagen.File(path)
        except mutagen.MutagenError:
            audio = None
        if audio is None or not getattr(audio.info, "length", 0):
            return "not_audio"
        if (
            song.duration_s
            and abs(audio.info.length - song.duration_s) > DURATION_TOLERANCE_S
        ):
            return "duration"
        # the header's length can outlive a cut-off file (live #114: 4 KB read as
        # 306 s); FLAC's bitrate comes from the file size, so it can't tell
        bitrate = getattr(audio.info, "bitrate", 0) or 0
        if (
            not isinstance(audio, FLAC)
            and bitrate
            and path.stat().st_size < MIN_SIZE_RATIO * bitrate * audio.info.length / 8
        ):
            return "truncated"
        return None

    # ------------------------------------------------------------ move

    def move(self, file_id: int) -> bool:
        with self._placing:
            return self._move(file_id)

    def _move(self, file_id: int) -> bool:
        rec, song = self._get(file_id)
        if rec.file_state not in ("verified", "tagged"):
            return False
        src = self._source_path(rec)
        audio = mutagen.File(src, easy=True)
        tagged = rec.file_state == "tagged"
        ext = src.suffix.lstrip(".").lower() or "audio"
        rel = layout(audio.tags if audio else None, song, ext, tagged)
        if rec.navidrome_id:  # a replacement: the old copy goes first
            self._delete_old(rec)
            with self._db.transaction(write=True) as conn:
                self._pulls.song_moved(conn, rec.song_id, rec.navidrome_id, None)
        target = free_path(self._library / rel)
        if target != self._library / rel:
            log.warning("file %s: %s exists, saving as %s", rec.id, rel, target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        move_file(src, target)
        with self._db.transaction(write=True) as conn:
            self._files.set_location(
                conn, rec.id, str(target.relative_to(self._music)), self._now()
            )
            self._files.set_navidrome_id(conn, rec.id, None, self._now())
        new = states.file_moved(rec.file_state)
        if not self._move_state(rec, new):
            log.warning("file %s changed while moving; file is at %s", rec.id, target)
            return False
        log.info("file %s moved to %s", rec.id, target)
        return True

    def _delete_old(self, rec: FileRecord) -> None:
        try:
            old = self._call(lambda s: self._nd.get_song(s, rec.navidrome_id or ""))
        except NavidromeNotFound:
            return  # Navidrome no longer has it: nothing to find
        ours = self._on_disk(old)
        if ours is None:  # gone, or outside the library: never deleted
            log.warning("file %s: old copy %s not in the library", rec.id, old.path)
            return
        ours.unlink()
        log.info("file %s: deleted the replaced copy %s", rec.id, ours)

    # ------------------------------------------------------------ index

    def index(self, file_id: int, scan: bool = True) -> bool:
        rec, _ = self._get(file_id)
        if rec.file_state != "moved":
            return False
        if scan:
            self.scan()
        song = self._find_in_navidrome(self._music / rec.file_location)
        if song is None:
            log.info(
                "file %s: Navidrome doesn't list %s yet", rec.id, rec.file_location
            )
            return False
        with self._db.transaction(write=True) as conn:
            self._files.set_navidrome_id(conn, rec.id, song.id, self._now())
        return self._move_state(rec, states.file_indexed("moved"))

    def scan(self) -> None:
        self._keep_downloads_out()
        status = self._call(lambda s: self._nd.start_scan(s))
        waited = 0.0
        while status.scanning and waited < SCAN_WAIT_S:
            self._sleep(SCAN_POLL_S)
            waited += SCAN_POLL_S
            status = self._call(lambda s: self._nd.scan_status(s))
        if status.error:
            log.warning("Navidrome scan error: %s", status.error)
        self._keep_downloads_out()  # a first scan only now tells the root

    # ------------------------------------------------------------ cleanup

    def discard_unverifiable(self, file_id: int, older_than_s: int) -> bool:
        """A download that failed verification ``older_than_s`` ago is deleted from
        the downloads folder (user, 2026-10-29); its row becomes ``missing``, the
        verification code stays as the reason."""
        rec, _ = self._get(file_id)
        if rec.file_state != "downloaded" or rec.verification in (None, "success"):
            return False
        if self._now() - rec.updated_at < older_than_s:
            return False
        path = self._source_path(rec)
        if path.resolve().is_relative_to(self._downloads.resolve()) and path.is_file():
            path.unlink()
        if self._move_state(rec, states.file_missing("downloaded")):
            log.info("file %s: deleted the unverifiable download (%s)", rec.id,
                     rec.verification)  # fmt: skip
            return True
        return False

    # ------------------------------------------------------------ missing

    def check_missing(self, file_id: int) -> bool:
        with self._placing:
            return self._check_missing(file_id)

    def _check_missing(self, file_id: int) -> bool:
        """Before indexing: the file is gone from disk. Indexed: Navidrome no longer
        knows the id or reports the file missing (deleted outside Post-Vinyl)."""
        rec, _ = self._get(file_id)
        if rec.file_state in ("missing", "deleted"):
            return False
        if rec.file_state == "indexed":
            try:
                nd = self._call(lambda s: self._nd.get_song(s, rec.navidrome_id or ""))
                gone = nd.missing
            except NavidromeNotFound:
                gone = True
        else:
            gone = not self._source_path(rec).is_file()
        if not gone:
            return False
        if self._move_state(rec, states.file_missing(rec.file_state)):
            log.warning("file %s is missing (%s)", rec.id, rec.file_location)
            return True
        return False

    # ------------------------------------------------------------ library (#112)

    def library_page(
        self, query: str, start: int, end: int
    ) -> tuple[list[LibrarySong], int]:
        """The whole library as Navidrome indexes it (user, 2026-10-30)."""
        page: tuple[list[LibrarySong], int] = self._call(
            lambda s: self._nd.list_songs(s, query, start, end)
        )
        return page

    def release_counts(self, album: str) -> Counter[str]:
        """How many library songs each MusicBrainz release of this album has."""
        songs, _ = self.library_page(album, 0, ALBUM_SONGS)
        return Counter(s.release_id for s in songs if s.release_id and not s.missing)

    def records(self, navidrome_ids: list[str]) -> dict[str, FileRecord]:
        with self._db.transaction() as conn:
            return self._files.by_navidrome_ids(conn, navidrome_ids)

    def tag_failed(self, file_ids: list[int]) -> set[int]:
        with self._db.transaction() as conn:
            return self._files.last_tagging_failed(conn, file_ids)

    def library_song(self, navidrome_id: str) -> LibrarySong:
        try:
            song: LibrarySong = self._call(lambda s: self._nd.get_song(s, navidrome_id))
        except NavidromeNotFound as e:
            raise LibraryFileNotFound(f"no library song {navidrome_id}") from e
        return song

    def cover(self, navidrome_id: str, size: int) -> Cover | None:
        """The song's cover from Navidrome (it owns the art; nothing is kept)."""
        try:
            cover: Cover = self._call(
                lambda s: self._nd.cover_art(s, navidrome_id, size)
            )
        except NavidromeNotFound:
            return None
        return cover

    def library_path(self, song: LibrarySong) -> Path:
        """Our path for a library song; never anything outside the library."""
        ours = self._on_disk(song)
        if ours is None:
            raise LibraryFileNotFound(f"{song.path} isn't a file in the library")
        return ours

    def _on_disk(self, song: LibrarySong) -> Path | None:
        """The song's file, or None. Navidrome's root may be the library or the
        music folder; the root it reports goes first, so a same-named file under
        the other root is never taken for this one."""
        bases = (self._library, self._music)
        if Path(song.library_path) == self._music:
            bases = (self._music, self._library)
        root = self._library.resolve()
        for base in bases:
            ours = (base / song.path).resolve()
            if ours.is_relative_to(root) and ours.is_file():
                return ours
        return None

    def _find_in_navidrome(self, path: Path) -> LibrarySong | None:
        """The song at ``path``, whichever root Navidrome's paths start from."""
        for base in (self._library, self._music):
            song = self._song_at(str(path.relative_to(base)))
            if song is not None:
                return song
        return None

    def _song_at(self, rel: str) -> LibrarySong | None:
        song: LibrarySong | None = self._call(
            lambda s: self._nd.find_song_by_path(s, rel)
        )
        return song

    def _sample(self) -> list[LibrarySong]:
        songs: list[LibrarySong] = self._call(
            lambda s: self._nd.list_songs(s, "", 0, LAYOUT_SAMPLE)
        )[0]
        return songs

    def check_layout(self) -> None:
        """Raises LayoutMismatch when no sampled song leads to a file: the mounts
        disagree, which no single file's error would say. Rows Navidrome itself
        marks missing say nothing about the mounts, so they don't count."""
        try:
            songs = [s for s in self._sample() if not s.missing]
        except NoAdmin:
            return  # before setup there's nothing to check as
        if songs and not any(self._on_disk(s) for s in songs):
            raise LayoutMismatch(
                f"Navidrome's {songs[0].library_path} doesn't match {self._library}"
            )

    def _keep_downloads_out(self) -> None:
        """With Navidrome at the music folder, list the downloads in .ndignore so
        half-finished files aren't indexed. Keeps every line already there."""
        if self._ignore_checked:
            return
        songs = self._sample()
        if not songs:
            return  # nothing tells the root yet
        self._ignore_checked = True
        if Path(songs[0].library_path) != self._music:
            return
        line = f"/{self._downloads_top}/"
        ignore = self._music / ".ndignore"
        try:
            text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            if line not in text.splitlines():
                end = "" if not text or text.endswith("\n") else "\n"
                ignore.write_text(f"{text}{end}{line}\n", encoding="utf-8")
                log.info("added %s to %s", line, ignore)
        except OSError as e:
            log.warning("couldn't update %s: %s", ignore, e)

    def re_place(
        self, navidrome_id: str, title: str, artist: str
    ) -> LibrarySong | None:
        with self._placing:
            return self._re_place(navidrome_id, title, artist)

    def _re_place(
        self, navidrome_id: str, title: str, artist: str
    ) -> LibrarySong | None:
        """After a rename: move the file to where its tags put it (untagged:
        ``<artist>/<title>``), rescan, relink our record. Never overwrites."""
        song = self.library_song(navidrome_id)
        src = self.library_path(song)
        marks = self._read_marks(navidrome_id)
        audio = mutagen.File(src, easy=True)
        tags = audio.tags if audio else None
        names = Song(0, title, artist, title, artist, None, None, None, None, None)
        ext = src.suffix.lstrip(".").lower() or "audio"
        target = self._library / layout(tags, names, ext, bool(_first(tags, "title")))
        if _numbered(src, target.resolve()):  # already its free name: stays put
            target = src
        if target.resolve() != src:
            target = free_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            move_file(src, target)
            self._prune(src.parent)
            log.info("library %s renamed: %s -> %s", navidrome_id, song.path,
                     target.relative_to(self._library))  # fmt: skip
        self.scan()
        new = self._find_in_navidrome(target)
        if new is None:
            if marks:
                log.warning("library %s: stars not carried, %s isn't listed",
                            navidrome_id, target)  # fmt: skip
        elif new.id != navidrome_id:
            self._write_marks(new.id, marks)
        with self._db.transaction(write=True) as conn:
            rec = self._files.by_navidrome_ids(conn, [navidrome_id]).get(navidrome_id)
            if rec is not None:
                loc = str(target.relative_to(self._music))
                self._files.set_location(conn, rec.id, loc, self._now())
                self._files.set_navidrome_id(conn, rec.id, new.id if new else None,
                                             self._now())  # fmt: skip
            if new is not None and new.id != navidrome_id:
                self._pulls.song_moved(conn, rec.song_id if rec else None,
                                       navidrome_id, new.id)  # fmt: skip
        return new

    def delete_library_file(self, navidrome_id: str) -> None:
        with self._placing:
            self._delete_library_file(navidrome_id)

    def _delete_library_file(self, navidrome_id: str) -> None:
        """Remove the file (inside the library only), mark our record deleted,
        rescan (user, 2026-10-30). A later request downloads the song again."""
        song = self.library_song(navidrome_id)
        path = self.library_path(song)
        path.unlink()
        self._prune(path.parent)
        with self._db.transaction(write=True) as conn:
            rec = self._files.by_navidrome_ids(conn, [navidrome_id]).get(navidrome_id)
            if rec is not None and rec.file_state in ("moved", "indexed", "missing"):
                self._files.set_state(conn, rec.id, states.file_deleted(rec.file_state),
                                      self._now())  # fmt: skip
        log.info("library %s deleted: %s", navidrome_id, song.path)
        self.scan()

    def _user_sessions(self) -> list[tuple[str, Session]]:
        """A session per user we hold a working Navidrome login for."""
        with self._db.transaction() as conn:
            users = UsersStore().all(conn)
        sessions = []
        for user in users:
            try:
                session = self._logins.session_for(user)
            except (NavidromeError, DecryptError) as e:
                log.warning("no Navidrome session for %s: %s", user.username, e)
                continue
            if session is not None:
                sessions.append((user.username, session))
        return sessions

    def _read_marks(self, song_id: str) -> list[tuple[Session, bool, int]]:
        """Each user's star and rating: a retag can give the song a new id, and
        they stay behind on the old one (user, 2026-11-05)."""
        marks = []
        for username, session in self._user_sessions():
            try:
                starred, rating = self._nd.marks(session, song_id)
            except NavidromeError as e:
                log.warning("couldn't read %s's star on %s: %s", username, song_id, e)
                continue
            if starred or rating:
                marks.append((session, starred, rating))
        return marks

    def _write_marks(
        self, song_id: str, marks: list[tuple[Session, bool, int]]
    ) -> None:
        for session, starred, rating in marks:
            try:
                self._nd.set_marks(session, song_id, starred, rating)
            except NavidromeError as e:
                log.warning("couldn't carry %s's star to %s: %s",
                            session.username, song_id, e)  # fmt: skip

    def _prune(self, folder: Path) -> None:
        """Remove folders with nothing but leftovers (covers, macOS files), up to
        (not including) the library."""
        root = self._library.resolve()
        folder = folder.resolve()
        while folder != root and folder.is_relative_to(root):
            items = list(folder.iterdir())
            if not all(_leftover(item) for item in items):
                return
            for item in items:
                item.unlink()
            folder.rmdir()
            folder = folder.parent

    # ------------------------------------------------------------ Navidrome login

    def _call(self, fn: Callable[[Session], Any]) -> Any:
        """Run ``fn`` as the first admin; logs in again once if the session expired."""
        for retry in (False, True):
            if self._session is None or retry:
                self._session = self._login()
            try:
                return fn(self._session)
            except NavidromeAuthError:
                if retry:
                    raise
                log.info("Navidrome session expired; logging in again")
        raise NavidromeError("unreachable")

    def _login(self) -> Session:
        while True:
            with self._db.transaction() as conn:
                admin = UsersStore().first_admin(conn)
            if admin is None:
                raise NoAdmin("no admin has a working login")
            session = self._logins.session_for(admin)
            if session is not None:
                return session
