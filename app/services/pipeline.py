"""File pipeline (#105): downloaded → verify → tag → move → index.

Called by the ``file_pipeline`` worker every ``pipeline_every_seconds``
(user, 2026-10-29). One tick:

1. verify every new download;
2. tag every verified file; a failed tagging is tried **once more** after
   ``retag_after_minutes``, and after a second failure the file moves untagged;
3. move every tagged file (and the twice-failed verified ones);
4. **one** Navidrome scan for everything moved, then link each file; a file
   Navidrome doesn't list yet is linked on a later tick.

Also hourly: files that vanished become ``missing``; and downloads that failed
verification are deleted after ``unverifiable_keep_hours``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import WorkersSettings
from app.services.files import FilesServiceBase
from app.services.tagging import TaggingServiceBase
from app.stores.database import Database
from app.stores.files import FileRecord, FilesStore

log = logging.getLogger(__name__)

LIVE_STATES = ("downloaded", "verified", "tagged", "moved", "indexed")


@dataclass
class TickResult:
    verified: int = 0
    tagged: int = 0
    moved: int = 0
    indexed: int = 0


class FilePipeline:
    def __init__(
        self,
        db: Database,
        files: FilesServiceBase,
        tagging: TaggingServiceBase,
        settings: WorkersSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._files = files
        self._tagging = tagging
        self._settings = settings
        self._clock = clock
        self._store = FilesStore()

    def _in(self, state: str) -> list[FileRecord]:
        with self._db.transaction() as conn:
            return self._store.in_state(conn, state)

    def _failed_tags(self, rec: FileRecord) -> list[int]:
        """When this file's taggings failed; earlier files of the song don't count."""
        with self._db.transaction() as conn:
            rows = self._store.tagging_for_file(conn, rec.id)
        return [r.tagged_at for r in rows
                if r.status == "failTag" and r.tagged_at >= rec.downloaded_at]  # fmt: skip

    def tick(self) -> TickResult:
        out = TickResult()
        for rec in self._in("downloaded"):
            if rec.verification is None:
                out.verified += self._files.verify(rec.id)
        retag_s = self._settings.retag_after_minutes * 60
        for rec in self._in("verified"):
            fails = self._failed_tags(rec)
            if not fails or (len(fails) == 1 and self._clock() - fails[0] >= retag_s):
                out.tagged += self._tagging.tag(rec.id)
            elif len(fails) >= 2:
                out.moved += self._files.move(rec.id)  # untagged, as decided
        for rec in self._in("tagged"):
            out.moved += self._files.move(rec.id)
        moved = self._in("moved")
        if moved:
            self._files.scan()
            for rec in moved:
                out.indexed += self._files.index(rec.id, scan=False)
        if any(vars(out).values()):
            log.info("file pipeline: %s", out)
        return out

    def check_missing(self) -> int:
        found = 0
        for state in LIVE_STATES:
            for rec in self._in(state):
                found += self._files.check_missing(rec.id)
        return found

    def discard_unverifiable(self) -> int:
        keep_s = self._settings.unverifiable_keep_hours * 3600
        return sum(
            self._files.discard_unverifiable(rec.id, keep_s)
            for rec in self._in("downloaded")
        )
