"""
HistoryCleaner — P6.9-7 background worker.

Every `download.history_clear_interval_minutes` (default 15, 0 disables),
and once at startup when enabled:

1. Fetch slskd's download history and permanently remove every terminal
   record (completed / failed / cancelled) via `DELETE ...?remove=true`,
   the same primitive the Transfers tab's "Delete finished" uses.
2. Best-effort the same for upload history when the download service
   exposes it — the endpoint exists on slskd 0.26.0 (live-verified), but a
   missing or failing upload surface must never block download cleanup.

Discovered live 2026-08-14: slskd's accumulated transfer history congested
the stack — searches and queue attempts degraded, and clearing the history
restored normal operation. This worker does that on a timer.

Deliberate semantics (user decisions 2026-08-14):

- **Local rows are kept.** Only slskd-side records are removed; musica's
  own bookkeeping (import tracking, retry state, the transfers UI's DB
  views) is untouched. Failed retry candidates in the UI remain visible
  even though slskd forgot the underlying record.
- **Failed removals are retried.** A refused or timed-out deletion leaves
  the slskd record in place and the pass just counts it as failed; the next
  cycle tries again.
- **A 'completed' transfer still awaiting its beets import is skipped.**
  Deleting slskd's record would make the next monitor poll stop reporting
  it, so the file would never be handed to beets. Mirrors the guard in
  DELETE /api/transfers?state=finished.
"""

import threading

from app.db.download_store import DownloadStore
from app.logging_config import get_logger

logger = get_logger(__name__)

FINISHED_STATES = {"completed", "failed", "cancelled"}

#: Minimum sleep between due-checks so a 0/disabled interval can't spin.
TICK_SECONDS = 60


class HistoryCleaner:
    """
    Periodically clears slskd's terminal transfer history.

    Runs as a daemon thread. clean_once() is synchronous and independently
    testable; start() / stop() manage the thread. The interval is read
    fresh every tick, so a hot-reloaded value takes effect without a
    restart.
    """

    def __init__(self, config, download_service, database=None, event_hub=None):
        self._config = config
        self._download_service = download_service
        self._store = DownloadStore(database) if database is not None else None
        self._event_hub = event_hub
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background worker thread."""
        if self._thread is not None:
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        logger.info("HistoryCleaner started (interval=%dm)", self._interval_minutes())

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self.request_stop()
        self.join(timeout=5)

    def request_stop(self) -> None:
        """Signal the thread to stop without waiting (non-blocking)."""
        self._stopped.set()

    def join(self, timeout: float | None = 5) -> None:
        """Wait for the thread to finish (call request_stop() first)."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("HistoryCleaner stopped")

    def run(self) -> None:
        """Main loop — cleans once at startup, then every interval.

        A startup run is intentional (same decision as LoveSync/TrashPurge):
        the user's stabilization fix is clearing history, so doing it on
        boot matches their manual recovery step.
        """
        while not self._stopped.is_set():
            if self._interval_minutes() > 0:
                try:
                    self.clean_once()
                except Exception:
                    logger.exception("Unhandled error in HistoryCleaner.clean_once")
            self._stopped.wait(TICK_SECONDS)

    def clean_once(self) -> dict:
        """One history-clear pass (sync, independently testable).

        Returns a summary dict; never raises — every per-record failure is
        counted, and a hard slskd failure degrades to a warning.
        """
        result = {
            "deleted_downloads": 0,
            "failed_downloads": 0,
            "deleted_uploads": 0,
            "failed_uploads": 0,
            "skipped": 0,
        }

        try:
            transfers = self._download_service.get_status()
        except Exception as e:  # noqa: BLE001 — slskd backends vary
            logger.warning("HistoryCleaner: cannot reach slskd: %s", e)
            return result

        for transfer in transfers:
            if transfer.state not in FINISHED_STATES:
                continue
            # A completed transfer whose file hasn't been handed to beets
            # yet must not have its slskd record removed — the monitor
            # would stop reporting it and the import would never run.
            if (
                transfer.state == "completed"
                and self._store is not None
                and self._store.import_pending(transfer.transfer_id)
            ):
                result["skipped"] += 1
                continue
            try:
                ok = self._download_service.delete_transfer(transfer.transfer_id)
            except Exception as e:  # noqa: BLE001 — delete impls vary
                logger.warning(
                    "HistoryCleaner: delete failed for %s: %s", transfer.transfer_id, e
                )
                ok = False
            if ok:
                result["deleted_downloads"] += 1
            else:
                result["failed_downloads"] += 1

        self._clean_uploads(result)

        logger.info(
            "HistoryCleaner: %d download(s) cleared, %d failed, %d skipped; "
            "%d upload(s) cleared, %d failed",
            result["deleted_downloads"],
            result["failed_downloads"],
            result["skipped"],
            result["deleted_uploads"],
            result["failed_uploads"],
        )
        if self._event_hub is not None:
            self._event_hub.publish("system.history_cleaned", result)
        return result

    def _clean_uploads(self, result: dict) -> None:
        """Best-effort pass over slskd's upload history, if supported.

        Never raises and never blocks download cleanup: an upload surface
        that is missing or failing just leaves uploads untouched.
        """
        get_uploads = getattr(self._download_service, "get_upload_status", None)
        delete_upload = getattr(self._download_service, "delete_upload_transfer", None)
        if get_uploads is None or delete_upload is None:
            return
        try:
            uploads = get_uploads()
        except Exception as e:  # noqa: BLE001 — upload backends vary
            logger.warning("HistoryCleaner: cannot fetch uploads: %s", e)
            return
        for transfer in uploads:
            if transfer.state not in FINISHED_STATES:
                continue
            try:
                ok = delete_upload(transfer.transfer_id, transfer.username)
            except Exception as e:  # noqa: BLE001 — delete impls vary
                logger.warning(
                    "HistoryCleaner: upload delete failed for %s: %s",
                    transfer.transfer_id,
                    e,
                )
                ok = False
            if ok:
                result["deleted_uploads"] += 1
            else:
                result["failed_uploads"] += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _interval_minutes(self) -> int:
        """Read the (hot-reloadable) clear interval; 0 disables."""
        return max(
            0,
            int(
                getattr(
                    getattr(self._config, "download", None),
                    "history_clear_interval_minutes",
                    15,
                )
            ),
        )
