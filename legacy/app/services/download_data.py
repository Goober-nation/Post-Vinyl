"""
DownloadDataService — service-layer wrapper around DownloadStore.

Routes should go through this instead of instantiating DownloadStore
directly, so download-related business rules/validation stay in one
place as more DB writers (LoveSync, TrashPurge, playlists) get added.

Queueing is two-phase: rows are written (queue_rows) before slskd is asked
to queue anything, then settled (settle_queued) with slskd's answer. A row
therefore always exists for a transfer slskd could have started.
"""

from app.db.database import Database
from app.db.download_store import DownloadStore


class DownloadDataService:
    """Wraps DownloadStore for use by routes."""

    def __init__(self, database: Database):
        self._store = DownloadStore(database)

    def queue_rows(
        self,
        search_id: str,
        username: str,
        files: list,
        is_rec: bool,
    ) -> list[tuple[str, int]]:
        """Write one 'queued' download row per file, before slskd is asked.

        Args:
            search_id: Search ID that originated these files (must be real)
            username: Peer username
            files: Objects/dicts with .filename/.size (or ["filename"]/["size"])
            is_rec: Whether this download is a recommendation download

        Returns:
            (filename, download id) pairs for settle_queued / abandon_queued
        """
        rows = []
        for f in files:
            filename = f.filename if hasattr(f, "filename") else f["filename"]
            size = f.size if hasattr(f, "size") else f.get("size", 0)
            download_id = self._store.insert_pending(
                search_id, username, filename, size, is_rec
            )
            rows.append((filename, download_id))
        return rows

    def settle_queued(
        self, rows: list[tuple[str, int]], failures: list[dict]
    ) -> int:
        """Record slskd's answer for rows from queue_rows.

        Files named in `failures` (QueueResult.failures, a list of
        {"filename": ...} dicts) become 'failed'; the rest are marked as
        accepted by slskd. Returns how many were accepted.
        """
        failed_filenames = {fail["filename"] for fail in failures}
        accepted = 0
        for filename, download_id in rows:
            if filename in failed_filenames:
                self._store.set_state(download_id, "failed")
            else:
                self._store.mark_attempt_accepted(download_id)
                accepted += 1
        return accepted

    def abandon_queued(self, rows: list[tuple[str, int]]) -> None:
        """slskd could not be reached for these rows: mark them failed."""
        for _filename, download_id in rows:
            self._store.set_state(download_id, "failed")

    def delete_finished(self, transfer_ids: list[str]) -> None:
        """Remove local rows for the given (already slskd-side-handled) transfer IDs."""
        row_ids = []
        for transfer_id in transfer_ids:
            row = self._store.get_by_slskd_id(transfer_id)
            if row is not None:
                row_ids.append(row["id"])
        if row_ids:
            self._store.delete(row_ids)
