"""
DownloadDataService — service-layer wrapper around DownloadStore.

Routes should go through this instead of instantiating DownloadStore
directly, so download-related business rules/validation stay in one
place as more DB writers (LoveSync, TrashPurge, playlists) get added.
"""

from app.db.database import Database
from app.db.download_store import DownloadStore


class DownloadDataService:
    """Wraps DownloadStore for use by routes."""

    def __init__(self, database: Database):
        self._store = DownloadStore(database)

    def record_queued_files(
        self,
        search_id: str,
        username: str,
        files: list,
        failures: list[dict],
        is_rec: bool,
    ) -> int:
        """
        Persist pending download rows for files that were successfully
        enqueued (i.e. not present in the queue result's failures list).

        Args:
            search_id: Search ID that originated these files
            username: Peer username
            files: Objects/dicts with .filename/.size (or ["filename"]/["size"])
            failures: QueueResult.failures — list of {"filename": ...} dicts
            is_rec: Whether this download is a recommendation download

        Returns:
            Number of rows persisted
        """
        failed_filenames = {fail["filename"] for fail in failures}
        count = 0
        for f in files:
            filename = f.filename if hasattr(f, "filename") else f["filename"]
            size = f.size if hasattr(f, "size") else f.get("size", 0)
            if filename in failed_filenames:
                continue
            self._store.insert_pending(search_id, username, filename, size, is_rec)
            count += 1
        return count

    def delete_finished(self, transfer_ids: list[str]) -> None:
        """Remove local rows for the given (already slskd-side-handled) transfer IDs."""
        if transfer_ids:
            self._store.delete_transfers(transfer_ids)
