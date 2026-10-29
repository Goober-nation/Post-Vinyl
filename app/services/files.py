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
  folder is the library only, so downloads are never indexed.
- Navidrome is reached as the first Post-Vinyl admin, with the Navidrome
  password stored (encrypted) at their login.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mutagen

from app.clients.navidrome import (
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeError,
    NavidromeNotFound,
    Session,
)
from app.config import FilesSettings
from app.core import states
from app.core.matcher import DURATION_TOLERANCE_S
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.files import FileRecord, FilesStore
from app.stores.songs import Song, SongsStore
from app.stores.users import UsersStore

log = logging.getLogger(__name__)

SCAN_WAIT_S = 120
SCAN_POLL_S = 2
MAX_SEGMENT = 120  # characters per folder/file name
_UNSAFE = re.compile(r"[\x00-\x1f/\\]")


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
        self._music = Path(settings.music_dir)
        self._downloads = self._music / settings.slskd_downloads_dir
        self._library = self._music / settings.library_dir
        self._clock = clock
        self._sleep = sleep
        self._files = FilesStore()
        self._songs = SongsStore()
        self._session: Session | None = None

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
        return None

    # ------------------------------------------------------------ move

    def move(self, file_id: int) -> bool:
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
        target = free_path(self._library / rel)
        if target != self._library / rel:
            log.warning("file %s: %s exists, saving as %s", rec.id, rel, target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, target)
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
        path = (Path(old.library_path) / old.path).resolve()
        # Navidrome's library path is its own mount; map it onto ours by the
        # path inside the library, and never delete outside the library
        ours = (self._library / old.path).resolve()
        if not ours.is_relative_to(self._library.resolve()):
            log.warning(
                "file %s: old copy %s is outside the library; kept", rec.id, path
            )
            return
        if ours.is_file():
            ours.unlink()
            log.info("file %s: deleted the replaced copy %s", rec.id, ours)

    # ------------------------------------------------------------ index

    def index(self, file_id: int, scan: bool = True) -> bool:
        rec, _ = self._get(file_id)
        if rec.file_state != "moved":
            return False
        library_rel = str(Path(rec.file_location).relative_to(Path(self._library.name)))
        if scan:
            self.scan()
        song = self._call(lambda s: self._nd.find_song_by_path(s, library_rel))
        if song is None:
            log.info("file %s: Navidrome doesn't list %s yet", rec.id, library_rel)
            return False
        with self._db.transaction(write=True) as conn:
            self._files.set_navidrome_id(conn, rec.id, song.id, self._now())
        return self._move_state(rec, states.file_indexed("moved"))

    def scan(self) -> None:
        status = self._call(lambda s: self._nd.start_scan(s))
        waited = 0.0
        while status.scanning and waited < SCAN_WAIT_S:
            self._sleep(SCAN_POLL_S)
            waited += SCAN_POLL_S
            status = self._call(lambda s: self._nd.scan_status(s))
        if status.error:
            log.warning("Navidrome scan error: %s", status.error)

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
        raise NavidromeError("unreachable")

    def _login(self) -> Session:
        with self._db.transaction() as conn:
            admin = UsersStore().first_admin(conn)
        if admin is None:
            raise NoAdmin("no Post-Vinyl admin yet; run the setup first")
        return self._nd.login(admin.username, self._box.decrypt(admin.nd_password_enc))
