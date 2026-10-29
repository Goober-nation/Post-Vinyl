"""slskd cleanup (#105): clear what the app is done with at slskd and on disk.

Part of the slskd backend; called by the ``cleanup`` worker (user, 2026-10-29):

- **handed-off transfers**: a completed attempt whose file has left the
  downloads folder (moved into the library) is forgotten at slskd
  (``cancel_download(forget=True)``: the record goes, no file is touched);
- **failed attempts**: their transfers are forgotten too, and their own
  ``post-vinyl/<download>/<attempt>`` folder (anything slskd saved) is deleted;
- **empty folders** under ``post-vinyl/`` are removed.

Only transfers of the app's own attempts are touched, and only folders under
``post-vinyl/`` inside slskd's downloads folder.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.clients.slskd import SlskdClientBase, SlskdError
from app.config import FilesSettings
from app.services.slskd_download import DESTINATION_ROOT, destination, local_name
from app.stores.database import Database
from app.stores.slskd import SlskdStore

log = logging.getLogger(__name__)


class SlskdCleanup:
    def __init__(
        self, db: Database, client: SlskdClientBase, settings: FilesSettings
    ) -> None:
        self._db = db
        self._client = client
        self._root = Path(settings.music_dir) / settings.slskd_downloads_dir
        self._ours = self._root / DESTINATION_ROOT
        self._store = SlskdStore()

    def tick(self) -> int:
        """One round; returns how many transfers were forgotten."""
        known = {t.id for t in self._client.list_downloads()}
        with self._db.transaction() as conn:
            ended = self._store.ended_attempts(conn)
        forgotten = 0
        for a in ended:
            folder = self._root / destination(a.download_id, a.id)
            if a.outcome == "completed" and (folder / local_name(a.filename)).exists():
                continue  # the file is still waiting for the pipeline
            if a.outcome != "completed" and folder.is_dir():
                shutil.rmtree(folder)
                log.info("deleted the leftovers of failed attempt %s", a.id)
            if a.transfer_id and a.transfer_id in known:
                try:
                    self._client.cancel_download(a.username, a.transfer_id, forget=True)
                    forgotten += 1
                except SlskdError as e:
                    log.info("forgetting transfer %s failed: %s", a.transfer_id, e)
        self._remove_empty()
        return forgotten

    def _remove_empty(self) -> None:
        if not self._ours.is_dir():
            return
        for download in self._ours.iterdir():
            if not download.is_dir():
                continue
            for attempt in download.iterdir():
                if attempt.is_dir() and not any(attempt.iterdir()):
                    attempt.rmdir()
            if not any(download.iterdir()):
                download.rmdir()
