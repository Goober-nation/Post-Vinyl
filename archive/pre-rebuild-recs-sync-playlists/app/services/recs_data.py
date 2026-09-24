"""
RecsDataService — service-layer wrapper around RecsStore/DownloadStore for
recommendation routes.

Routes should go through this instead of instantiating RecsStore/DownloadStore
directly, so recommendation-related business rules/validation stay in one
place as more DB writers (LoveSync, TrashPurge, playlist rotation) get added.
"""

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.recs_store import RecsStore
from app.logging_config import get_logger
from app.services.interfaces.download import DownloadService

logger = get_logger(__name__)


class RecsDataService:
    """Wraps RecsStore/DownloadStore for use by recommendation routes."""

    def __init__(self, database: Database):
        self._recs = RecsStore(database)
        self._downloads = DownloadStore(database)

    def status_counts(self) -> dict[str, int]:
        return self._recs.count_recs_by_status()

    def list_recs(
        self, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        items = self._recs.list_recs(status=status, limit=limit, offset=offset)
        if status:
            total = self._recs.count_recs_by_status().get(status, 0)
        else:
            total = self._recs.count_recs()
        return items, total

    def cancel_all_queued(self, download_service: DownloadService) -> dict:
        """
        Cancel every recommendation currently in 'queued' status.

        Best-effort: also cancels the underlying slskd transfer for each queued
        recommendation (matched via search_id, falling back to a live username+
        filename lookup for rows not yet adopted by DownloadMonitor), then flips
        the recommendation rows to 'cancelled' regardless of transfer-cancel
        outcome so they stop showing up as queued.
        """
        queued_recs = self._recs.get_recs_by_status("queued")
        search_ids = [r["search_id"] for r in queued_recs if r["search_id"]]
        downloads = self._downloads.get_downloads_by_search_ids(search_ids)

        live_by_key: dict[tuple[str, str], str] = {}
        try:
            for t in download_service.get_status():
                live_by_key[(t.username, t.filename)] = t.transfer_id
        except Exception:
            logger.warning(
                "cancel-queued: failed to fetch live transfer status", exc_info=True
            )

        cancelled_transfers = 0
        failed_transfers = 0
        for d in downloads:
            if d["state"] in ("completed", "cancelled", "failed"):
                continue
            transfer_id = d["slskd_id"] or live_by_key.get(
                (d["username"], d["filename"])
            )
            if not transfer_id:
                continue
            try:
                if download_service.cancel(transfer_id):
                    cancelled_transfers += 1
                else:
                    failed_transfers += 1
            except Exception as e:  # noqa: BLE001 — cancel() impls can raise various errors
                logger.warning(
                    "cancel-queued: failed to cancel transfer %s: %s", transfer_id, e
                )
                failed_transfers += 1

        cancelled_recs = self._recs.bulk_update_status("queued", "cancelled")

        logger.info(
            "cancel-queued: %d recs cancelled, %d transfers cancelled, %d transfer cancels failed",
            cancelled_recs,
            cancelled_transfers,
            failed_transfers,
        )

        return {
            "cancelled_recs": cancelled_recs,
            "cancelled_transfers": cancelled_transfers,
            "failed_transfers": failed_transfers,
        }
