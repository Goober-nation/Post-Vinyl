"""
LoveSync — P6.7-5 (formerly P5-3) background worker.

Every `sync.interval_hours` (default 12h), and once at startup:

1. Fetch Navidrome starred songs.
2. Set rating to 5 for unrated favorites (a star in Navidrome means "love").
3. Send +1 feedback to ListenBrainz for every starred song whose sync_state
   row says feedback is still outstanding, and record it in sync_state.

There is deliberately no unstar handling (user decision 2026-08-13): the
worker only ever adds loves; sync_state rows persist.

`lb_synced` is the retry flag: when ListenBrainz is unreachable or disabled,
the row is recorded with lb_synced=0 and the feedback is re-attempted on the
next cycle. A song without an MBID has nothing to send and is recorded as
synced immediately so it is never retried.
"""

import threading

from app.db.database import Database
from app.db.sync_store import LOVE, SyncStore
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError,
    ListenBrainzFeedbackError,
)
from app.logging_config import get_logger

logger = get_logger(__name__)


class LoveSync:
    """
    Pushes Navidrome stars to ListenBrainz as loves.

    Runs as a daemon thread. sync_once() is synchronous and independently
    testable; start() / stop() manage the thread.
    """

    def __init__(
        self,
        config,
        library_service,
        feedback_service,
        database: Database,
        sync_store: SyncStore | None = None,
    ) -> None:
        self._config = config
        self._library = library_service
        self._feedback = feedback_service
        self._sync_store = sync_store or SyncStore(database)
        self._stopped = threading.Event()
        self._run_lock = threading.Lock()
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
        logger.info("LoveSync started (interval=%dh)", self._interval_hours())

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
        logger.info("LoveSync stopped")

    def run(self) -> None:
        """Main loop — syncs once at startup, then every interval.

        A startup run is intentional (user decision 2026-08-13): the sync is
        idempotent — sync_state guards ListenBrainz feedback, and setting a
        rating on an already-rated song is a no-op — so there is no reason
        to wait out the interval before the first pass.
        """
        while not self._stopped.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Unhandled error in LoveSync.sync_once")
            self._stopped.wait(self._interval_hours() * 3600)

    def run_once(self) -> dict:
        """Run one pass without overlapping a periodic or manual pass."""
        with self._run_lock:
            return self.sync_once()

    def sync_once(self) -> dict:
        """One sync pass (sync, independently testable).

        Returns a summary dict.
        """
        starred = self._library.get_starred()
        logger.info("LoveSync: %d starred song(s)", len(starred))

        rated = 0
        synced = 0
        failed = 0
        for song in starred:
            if (song.rating or 0) == 0:
                try:
                    if self._library.set_rating(song.song_id, 5):
                        rated += 1
                except Exception as e:  # noqa: BLE001 — library backends vary
                    logger.error(
                        "LoveSync: set_rating failed for %s: %s", song.song_id, e
                    )

            if self._sync_store.needs_feedback(song.song_id, LOVE):
                if self._send_love(song):
                    synced += 1
                else:
                    failed += 1

        logger.info(
            "LoveSync: %d rated, %d love(s) synced, %d pending",
            rated,
            synced,
            failed,
        )
        return {
            "starred": len(starred),
            "rated": rated,
            "synced": synced,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _interval_hours(self) -> int:
        """Read the (hot-reloadable) sync interval."""
        return max(
            1, int(getattr(getattr(self._config, "sync", None), "interval_hours", 12))
        )

    def _send_love(self, song) -> bool:
        """Send +1 for one starred song and record the sync_state row.

        Returns True when the feedback is settled for this cycle: delivered,
        or nothing to deliver (no MBID). False when it remains outstanding
        and should be retried next cycle.
        """
        if not song.mbid:
            self._sync_store.record(song.song_id, LOVE, None, lb_synced=1)
            return True
        try:
            ok = self._feedback.send_feedback(song.mbid, 1)
        except ListenBrainzDisabledError:
            # Not an error — LB is off. Record with lb_synced=0 so the
            # feedback is delivered whenever it is turned back on.
            self._sync_store.record(song.song_id, LOVE, song.mbid, lb_synced=0)
            return False
        except (ListenBrainzConnectionError, ListenBrainzFeedbackError) as e:
            logger.error(
                "LoveSync: feedback failed for %s (%s): %s", song.song_id, song.title, e
            )
            self._sync_store.record(song.song_id, LOVE, song.mbid, lb_synced=0)
            return False
        except Exception as e:  # noqa: BLE001 — feedback impls vary
            logger.error(
                "LoveSync: unexpected feedback error for %s: %s", song.song_id, e
            )
            self._sync_store.record(song.song_id, LOVE, song.mbid, lb_synced=0)
            return False

        if ok:
            self._sync_store.record(song.song_id, LOVE, song.mbid, lb_synced=1)
            logger.info("LoveSync: +1 for %s - %s", song.artist, song.title)
            return True
        self._sync_store.record(song.song_id, LOVE, song.mbid, lb_synced=0)
        return False
