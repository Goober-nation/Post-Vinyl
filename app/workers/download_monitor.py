"""
DownloadMonitor — Background worker that polls slskd for transfer state
and drives the download lifecycle: tracking, retry, file-move, SSE events.
"""

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.db.database import Database
from app.db.download_store import ALLOWED_EXTENSIONS, DownloadStore
from app.db.file_store import FileStore
from app.db.peer_store import PeerStore
from app.exceptions import SlskdConnectionError
from app.logging_config import get_logger
from app.sse import EventHub

# slskd sub-states that mean "our end lost/never had a solid connection" —
# exactly what a wifi/VPN reconnect produces across every in-flight transfer
# at once. Not the peer's fault, so these must never count toward
# bad_peer_threshold/peer blocking (found live 2026-08-14: with the default
# threshold of 1, a single reconnect blip permanently blocked every
# currently-transferring peer for peer_ban_days and queueing looked "stopped
# dead" afterward). Only a reason outside this set — a peer explicitly
# rejecting/erroring the transfer — reflects on the peer.
_CONNECTIVITY_FAIL_REASONS = {"timedout", "aborted", "cancelled"}

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetryOutcome:
    """Result of one DownloadRetrier.retry() call."""

    queued: bool
    peer: str | None = None
    # Why nothing was queued; empty when queued.
    reason: str = ""
    # True when the retry budget (download.max_retries_per_track) is spent.
    exhausted: bool = False


class DownloadRetrier:
    """The one retry implementation: DB-backed, shared by the monitor's
    automatic failure/orphan path and the UI's Retry button.

    A retry updates the download's own row (see DownloadStore.move_to_peer)
    and picks an alternative peer from the search the row came from. Every
    peer the row has ever been queued from is skipped, and the budget is
    `max_retries_per_track` retries after the original attempt.
    """

    def __init__(self, config, download_service, database: Database) -> None:
        self._config = config
        self._download_service = download_service
        self._store = DownloadStore(database)
        self._peers = PeerStore(database)

    def retry(
        self,
        download_id: int,
        slskd_id: str,
        *,
        bypass_manual_gate: bool = False,
    ) -> RetryOutcome:
        """Re-queue a failed download from an alternative peer.

        `download_id` is our row (kept through every retry); `slskd_id` is the
        slskd transfer that just failed or went missing, removed from slskd
        once the retry is queued. `bypass_manual_gate` lets the UI's Retry
        button retry a manual download even when auto_retry_manual_soulseek
        is off; the automatic path leaves it False.

        Raises SlskdConnectionError when the search cannot be re-read from
        slskd; every other failure is reported in the returned outcome.
        """
        original = self._store.get_with_request(download_id)
        if original is None:
            return RetryOutcome(False, reason="Download row no longer exists")
        username = original["username"]
        filename = original["filename"]
        is_manual = original["source"] != "rec"
        if (
            is_manual
            and not bypass_manual_gate
            and not getattr(self._config.download, "auto_retry_manual_soulseek", False)
        ):
            # #57: a rec's auto-retry is unaffected — this only gates manual
            # (user-searched) downloads on the automatic path. The UI's own
            # Retry button bypasses this flag.
            return RetryOutcome(False, reason="Auto-retry disabled for manual downloads")

        retry_count = self._store.retry_count(download_id)
        if retry_count >= self._config.download.max_retries_per_track:
            return RetryOutcome(False, reason="Max retries exceeded", exhausted=True)

        # Re-read the search this track came from. slskd retains completed
        # searches, so this works after a restart without musica keeping a
        # copy — and it re-reads an existing search rather than starting a
        # new one, so retry still picks from the same candidate pool.
        search_id = original["search_id"]
        responses = self._download_service.fetch_search_responses(search_id)
        if not responses:
            return RetryOutcome(False, reason="No search responses available for retry")

        # Every peer this download was ever queued from is skipped, not just
        # the one that just failed: a chain must never go A -> B -> A.
        peer, candidate = self._pick_alternative_peer(
            responses, self._store.tried_peers(download_id)
        )
        if not (peer and candidate):
            return RetryOutcome(False, reason="No viable alternative peer")

        self._store.move_to_peer(
            download_id, peer, candidate["filename"], candidate.get("size", 0)
        )
        try:
            result = self._download_service.queue(
                peer, [candidate], search_id=search_id
            )
        except Exception as e:  # noqa: BLE001 — queue() can raise various impl-specific errors
            self._store.set_state(download_id, "failed")
            return RetryOutcome(False, reason=str(e))

        if result.enqueued_count <= 0:
            self._store.set_state(download_id, "failed")
            return RetryOutcome(False, reason="Queue returned 0 enqueued")

        self._store.mark_attempt_accepted(download_id)
        self.abandon_at_slskd(slskd_id, username, filename)
        logger.info("Retry queued: %s from %s", filename, peer)
        return RetryOutcome(True, peer=peer)

    def abandon_at_slskd(self, transfer_id: str, username: str, filename: str) -> None:
        """Remove a transfer slskd should stop working on.

        Called once a retry is queued, for the superseded transfer. musica
        gives up on a peer while slskd keeps working on it, and a 'failed'
        state is often transient, so the original can complete after the same
        track was re-queued elsewhere and both copies land. Live-confirmed
        2026-08-11: a Comfort Zone pull of 5 tracks downloaded 2 of them
        twice, from the exact peer pairs this would have separated
        (whitelamp+N+3 and M3H9X+bob-bob-bob-123).

        Also called for a stalled transfer that no retry replaced, so it
        cannot finish behind a failed row.

        Best-effort: a failure here only risks the duplicate we already
        have today, so it must never break the retry that just succeeded.
        """
        try:
            removed = self._download_service.delete_transfer(transfer_id)
        except Exception:
            logger.warning(
                "Could not remove abandoned transfer %s from slskd; it may keep "
                "running or complete as a duplicate",
                transfer_id,
                exc_info=True,
            )
            return
        if removed:
            logger.info("Removed abandoned transfer from slskd: %s / %s", username, filename)
        else:
            logger.warning(
                "slskd refused to remove abandoned transfer %s / %s; it may keep "
                "running or complete as a duplicate",
                username,
                filename,
            )

    def _pick_alternative_peer(
        self,
        responses: list[dict],
        skip_usernames: set[str],
    ) -> tuple[str | None, dict | None]:
        """Pick an alternative peer from search responses.

        `skip_usernames` is every peer already tried for this download.
        """
        ban_seconds = self._config.download.peer_ban_days * 86400
        for response in responses:
            peer = response.get("username", "")
            if not peer or peer in skip_usernames:
                continue
            if self._peers.is_blocked(peer, ban_seconds):
                continue
            if self._peers.failure_count(peer) >= self._config.download.bad_peer_threshold:
                continue
            if not response.get("hasFreeUploadSlot"):
                continue

            # Find first audio file
            for f in response.get("files", []):
                fname = f.get("filename", "")
                ext = Path(fname).suffix.lower()
                if ext in ALLOWED_EXTENSIONS:
                    return (peer, {"filename": fname, "size": f.get("size", 0)})

        return (None, None)


class DownloadMonitor:
    """
    Polls slskd for transfer state and drives the download lifecycle.

    Runs as a daemon thread.  poll_once() is synchronous and independently
    testable; start() / stop() manage the thread.
    """

    def __init__(
        self,
        config,
        download_service,
        library_service,
        database: Database,
        event_hub: EventHub,
        interval: int | None = None,
    ) -> None:
        self._config = config
        self._download_service = download_service
        self._library_service = library_service
        self._store = DownloadStore(database)
        self._peers = PeerStore(database)
        self._files = FileStore(database)
        self._retrier = DownloadRetrier(config, download_service, database)
        self._event_hub = event_hub
        # slskd transfer_id -> wall-clock time its speed first dropped below
        # download.min_speed_kbps (cleared when it recovers). Drives the
        # slow-transfer half of the stall rule. In-memory, like the other
        # poll-timers: a restart gives slskd a fresh window.
        self._slow_since: dict[str, float] = {}
        self.interval = interval or config.download.check_interval
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        # slskd transfer_id -> consecutive polls slskd hasn't reported it.
        # Drives orphan reconciliation; in-memory so a musica restart
        # re-grants the grace period rather than judging slskd on its first
        # poll.
        self._missing_polls: dict[str, int] = {}
        # slskd transfer_id -> wall-clock time its source file was first not
        # found on disk. Drives _handle_missing_source's timeout. In-memory
        # rather than the row's completed_at: slskd keeps reporting a
        # finished transfer as "completed" on every poll, so a stored
        # timestamp would never age past the timeout.
        self._missing_source_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background poller thread."""
        if self._thread is not None:
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        logger.info("DownloadMonitor started (interval=%ds)", self.interval)

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
        logger.info("DownloadMonitor stopped")

    def run(self) -> None:
        """Main loop — polls until stopped."""
        while not self._stopped.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Unhandled error in poll_once")
            self._stopped.wait(self.interval)

    def poll_once(self) -> dict:
        """
        One monitoring cycle (sync, independently testable).

        Returns a summary dict.
        """
        logger.debug("DownloadMonitor: poll cycle start")
        moved_files: list[str] = []
        retried: list[str] = []
        scan_triggered = False
        transfers_seen = 0

        # 0. Housekeeping — deliberately before the slskd fetch, which
        # returns early when slskd is unreachable. slskd being down is
        # precisely when pending rows go unadopted, so the reaper must
        # still run on those cycles.
        self._reap_stale_pending()

        # 1. Fetch transfers from slskd
        try:
            transfers = self._download_service.get_status()
        except SlskdConnectionError as e:
            logger.warning("DownloadMonitor: cannot reach slskd: %s", e)
            return {"error": str(e)}

        transfers_seen = len(transfers)
        now = time.time()

        for t in transfers:
            # Only transfers this app started are tracked. A transfer with no
            # row by slskd_id is adopted into the one queued row waiting for
            # that peer and file; if none matches, it was not ours: ignore it.
            row = self._store.get_by_slskd_id(t.transfer_id)
            is_new = False
            if row is None:
                row = self._store.adopt(t.username, t.filename, t.transfer_id)
                if row is None:
                    continue
                is_new = True
            download_id = row["id"]
            prev_state = row["state"]
            # Stalled-transfer rule: a queued transfer that never starts, or a
            # downloading one that crawls below min speed for too long, is
            # failed here and goes through the same retry path as a reported
            # failure. slskd still lists it as live, so it is removed from
            # slskd below if no retry supersedes it.
            stall_msg = self._stall_reason(row, t, now)
            state = t.state
            fail_reason = t.fail_reason
            if stall_msg is not None:
                state = "failed"
                fail_reason = "stalled"
            if prev_state != state:
                self._store.set_state(download_id, state)

            # --- SSE: transfer.started (new OR pending-adopted OR queued→downloading) ---
            if state in ("queued", "downloading") and (
                is_new or prev_state in (None, "queued")
            ):
                self._event_hub.publish(
                    "transfer.started",
                    {
                        "transfer_id": t.transfer_id,
                        "username": t.username,
                        "filename": t.filename,
                        "size": t.size,
                    },
                )

            # --- SSE: transfer.progress (on transition into downloading) ---
            if state == "downloading" and (is_new or prev_state != "downloading"):
                self._event_hub.publish(
                    "transfer.progress",
                    {
                        "transfer_id": t.transfer_id,
                        "progress": int(t.progress),
                        "speed": int(t.speed) if t.speed is not None else None,
                    },
                )

            # --- Failure handling (on transition into failed) ---
            if state == "failed" and prev_state != "failed":
                will_retry = False
                error_msg = ""

                # Increment peer failure count and block the peer if it has
                # crossed the threshold — but blocking a peer is independent
                # of giving up on the track: always fall through and try an
                # alternative peer (excluding blocked ones) unless the retry
                # budget is exhausted or no alternative exists. Skip this
                # entirely for connectivity-flavored failures (timedout/
                # aborted/cancelled) — those reflect our own connection
                # dropping, not the peer doing anything wrong.
                blocked_msg = ""
                if fail_reason not in _CONNECTIVITY_FAIL_REASONS:
                    threshold = self._config.download.bad_peer_threshold
                    count, newly_blocked = self._peers.record_failure(
                        t.username, threshold
                    )
                    if newly_blocked:
                        logger.warning(
                            "Peer %s blocked after %d failures",
                            t.username,
                            count,
                        )
                    if count >= threshold:
                        blocked_msg = f"Peer blocked after {count} failures"

                will_retry, error_msg = self._attempt_retry(download_id, t.transfer_id)
                if stall_msg is not None:
                    if not will_retry:
                        # No retry superseded the stalled transfer; remove it so
                        # slskd does not keep it running behind a failed row.
                        self._retrier.abandon_at_slskd(t.transfer_id, t.username, t.filename)
                    error_msg = f"{stall_msg}; {error_msg}" if error_msg else stall_msg
                if will_retry:
                    retried.append(t.transfer_id)

                if blocked_msg and not will_retry:
                    error_msg = (
                        f"{blocked_msg}; {error_msg}" if error_msg else blocked_msg
                    )

                self._event_hub.publish(
                    "transfer.failed",
                    {
                        "transfer_id": t.transfer_id,
                        "error": error_msg or "unknown",
                        "will_retry": will_retry,
                    },
                )

            # --- File move (on completed) ---
            if t.state == "completed":
                should_emit = prev_state != "completed"
                target_dir = ""
                # The completed transition above created this file's
                # file_management row (trigger). It is still 'pending' until
                # the file is placed or given up on; no pending row means
                # there is nothing left to do for it.
                pending = self._files.pending_for(row["username"], row["filename"])
                if pending is not None:
                    moved = self._import_via_move(t, pending["id"])
                    if moved:
                        moved_files.append(str(moved))
                        self._files.mark_moved(
                            pending["id"],
                            moved.relative_to(self._config.paths.library_path).as_posix(),
                        )
                        target_dir = str(moved.parent)
                        should_emit = True

                if should_emit:
                    self._event_hub.publish(
                        "transfer.completed",
                        {
                            "transfer_id": t.transfer_id,
                            "filename": t.filename,
                            "target_dir": target_dir or "",
                        },
                    )

        # Forget slow-speed clocks for transfers no longer downloading.
        downloading_ids = {t.transfer_id for t in transfers if t.state == "downloading"}
        self._slow_since = {
            tid: since for tid, since in self._slow_since.items() if tid in downloading_ids
        }

        # 3b. Reconcile rows slskd has stopped reporting. Runs after the
        # loop above so anything adopted during *this* cycle already counts
        # as reported.
        retried.extend(self._reconcile_orphans({t.transfer_id for t in transfers}))

        # 4. Trigger library scan if files were moved
        if moved_files:
            try:
                self._library_service.trigger_scan()
                scan_triggered = True
                logger.info(
                    "Library scan triggered after moving %d files",
                    len(moved_files),
                )
            except Exception:
                logger.warning("Failed to trigger library scan", exc_info=True)

        return {
            "transfers_seen": transfers_seen,
            "moved": moved_files,
            "scan_triggered": scan_triggered,
            "retried": retried,
        }

    # ------------------------------------------------------------------
    # Stalled transfers
    # ------------------------------------------------------------------

    def _stall_reason(self, row, transfer, now: float) -> str | None:
        """Why this live slskd transfer counts as stalled, or None.

        A queued transfer is stalled when its latest attempt is older than
        download.start_timeout_secs. A downloading transfer is stalled when
        its speed has stayed under download.min_speed_kbps (kilobits per
        second) for start_timeout_secs. min_speed_kbps = 0 disables the speed
        half. Other states are never stalled.
        """
        timeout = self._config.download.start_timeout_secs
        if transfer.state == "queued":
            attempted = self._store.latest_attempt_at(row["id"])
            if attempted is not None and now - attempted >= timeout:
                return f"Not started within {timeout}s"
            return None

        if transfer.state != "downloading":
            return None
        min_kbps = self._config.download.min_speed_kbps
        if min_kbps <= 0:
            return None
        floor_bytes_per_sec = min_kbps * 125  # 1 kbps = 125 bytes/s
        if (transfer.speed or 0) >= floor_bytes_per_sec:
            self._slow_since.pop(transfer.transfer_id, None)
            return None
        since = self._slow_since.setdefault(transfer.transfer_id, now)
        if now - since >= timeout:
            return f"Speed stayed under {min_kbps} kbps for {timeout}s"
        return None

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    def _attempt_retry(self, download_id: int, transfer_id: str) -> tuple[bool, str]:
        """Automatic retry (failure and orphan paths): delegates to the shared
        DownloadRetrier, with the manual-download gate applied.

        Returns (will_retry, error_message).
        """
        try:
            outcome = self._retrier.retry(download_id, transfer_id)
        except SlskdConnectionError as e:
            return (False, str(e))
        if outcome.queued:
            return (True, "")
        return (False, outcome.reason)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def _reconcile_orphans(self, reported_ids: set[str]) -> list[str]:
        """Fail + retry downloads slskd has stopped reporting.

        poll_once only ever walks the transfers slskd *reports*, so a row
        slskd forgets — slskd restarted, its transfer list was cleared, the
        transfer was removed out-of-band — would otherwise sit in
        'downloading' forever: a phantom row in the UI, no retry (retry
        fires on a reported 'failed' state that never comes), and, if it's
        a manual download, a permanent block on rec queueing via
        has_active_manual_downloads().

        Note this is NOT "on restart, drop everything". Restarting musica
        alone leaves slskd transferring untouched, and those rows resync
        normally on the next poll — cancelling them would destroy healthy
        transfers. The trigger is slskd's own view, not our uptime.

        A row must be missing for `download.orphan_grace_polls` consecutive
        polls before it counts, so a single truncated or blipped status
        response doesn't kill live transfers. The counter is in-memory on
        purpose: after a musica restart it starts empty, which gives slskd
        a few polls of grace to report everything before we judge it.

        Returns the transfer ids that were successfully re-queued.
        """
        grace = getattr(
            getattr(self._config, "download", None), "orphan_grace_polls", 2
        )
        # Keyed on slskd's transfer id (the one slskd reports), never on our
        # own download id: that is what reported_ids contains.
        live = {row["slskd_id"]: row for row in self._store.adopted_active()}

        # Drop counters for rows that came back or are no longer live.
        self._missing_polls = {
            tid: misses
            for tid, misses in self._missing_polls.items()
            if tid in live and tid not in reported_ids
        }

        orphans = []
        for slskd_id, row in live.items():
            if slskd_id in reported_ids:
                continue
            misses = self._missing_polls.get(slskd_id, 0) + 1
            self._missing_polls[slskd_id] = misses
            if misses >= int(grace):
                orphans.append((slskd_id, row))

        retried: list[str] = []
        for slskd_id, row in orphans:
            self._missing_polls.pop(slskd_id, None)
            self._store.set_state(row["id"], "failed")
            logger.warning(
                "Download orphaned — slskd stopped reporting it after %d polls: "
                "%s / %s",
                grace,
                row["username"],
                row["filename"],
            )
            will_retry, error_msg = self._attempt_retry(row["id"], slskd_id)
            if will_retry:
                retried.append(slskd_id)
            self._event_hub.publish(
                "transfer.failed",
                {
                    "transfer_id": slskd_id,
                    "error": (
                        "slskd stopped reporting this transfer"
                        + (f"; {error_msg}" if error_msg else "")
                    ),
                    "will_retry": will_retry,
                },
            )
        return retried

    def _reap_stale_pending(self) -> None:
        """Fail queue-time pending rows slskd never adopted (P6.5-5 fix).

        Without this a manual download slskd silently drops holds
        RecPuller's queue-priority gate open forever — every subsequent rec
        pull aborts, and no endpoint can clear the row. See
        DownloadStore.fail_stale_unadopted for why 'failed' is the right
        resting state.
        """
        timeout = getattr(
            getattr(self._config, "download", None), "pending_timeout_minutes", 5
        )
        try:
            stale = self._store.fail_stale_unadopted(int(timeout) * 60)
        except Exception:
            logger.warning("Failed to reap stale pending downloads", exc_info=True)
            return
        for row in stale:
            logger.warning(
                "Pending download never adopted by slskd after %s min, "
                "marking failed: %s / %s",
                timeout,
                row["username"],
                row["filename"],
            )
            # Never adopted, so there is no slskd id: announce our own id.
            self._event_hub.publish(
                "transfer.failed",
                {
                    "transfer_id": str(row["id"]),
                    "error": (
                        f"slskd never picked up this download within {timeout} minutes"
                    ),
                    "will_retry": False,
                },
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _import_via_move(self, transfer, file_id: int) -> Path | None:
        """
        Move a completed download's file into the flat landing directory,
        no tagging, no metadata parsing, no profile selection — least
        friction, always works (replaces the old beets-driven import;
        see AGENTS.md / issues #2, #55, #58 for why beets was removed).

        `file_id` is the file_management row waiting for this file. Returns
        the destination path on success, or None if the source file couldn't
        be located or the move failed — in every None case the source file
        is left untouched and the poll loop keeps going.
        """
        source = self._resolve_source_path(transfer)
        if source is None:
            self._handle_missing_source(transfer, file_id)
            return None
        # Self-healed: a source that was missing on an earlier poll turned
        # up before the timeout — drop the tracking entry so a later,
        # unrelated miss on this same transfer starts its own clock.
        self._missing_source_since.pop(transfer.transfer_id, None)

        dest_dir = self._config.paths.library_path / "new-system-tests"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = self._unique_destination(dest_dir, source.name)
            shutil.move(str(source), str(dest))
        except OSError as e:
            logger.warning(
                "Failed to move completed download %s -> %s: %s",
                source,
                dest_dir,
                e,
            )
            return None

        return dest

    @staticmethod
    def _unique_destination(dest_dir: Path, filename: str) -> Path:
        """Append a numeric suffix on collision rather than overwrite/fail."""
        candidate = dest_dir / filename
        if not candidate.exists():
            return candidate
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        n = 1
        while True:
            candidate = dest_dir / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def _handle_missing_source(self, transfer, file_id: int) -> None:
        """A transfer reports 'completed' but its file is nowhere on disk.

        Usually transient — slskd can report completion a moment before the
        file is visible on the mount — so this only acts once
        `missing_source_timeout_minutes` has passed since the source was
        *first* seen missing, with no source ever found in between; every
        poll before that is a silent no-op, left to retry next cycle exactly
        as before. Tracked in `_missing_source_since` (per slskd transfer)
        rather than a stored timestamp: slskd keeps reporting a finished
        transfer as "completed" on every poll, so a stored timestamp would
        never age past the timeout.

        Past the timeout, retrying is pointless: nothing about the file
        reappearing is a function of time anymore.

        Marks the file_management row 'missing' (terminal). That is what
        stops the poll-every-cycle retry: the row is no longer 'pending', so
        pending_for() returns nothing for it, even though slskd keeps
        reporting the transfer as "completed".
        """
        transfer_id = transfer.transfer_id
        now = time.time()
        first_seen = self._missing_source_since.setdefault(transfer_id, now)
        timeout_minutes = getattr(
            getattr(self._config, "download", None),
            "missing_source_timeout_minutes",
            5,
        )
        if now - first_seen < timeout_minutes * 60:
            return
        self._missing_source_since.pop(transfer_id, None)
        self._files.mark_missing(file_id)
        logger.warning(
            "Giving up on %s / %s: transfer completed but no source file "
            "was found on disk within %d minute(s); not retrying further",
            transfer.username,
            transfer.filename,
            timeout_minutes,
        )

    def _resolve_source_path(self, transfer) -> Path | None:
        """
        Locate a completed transfer's file on disk under slskd's download
        directory. Tries the exact reported path first (matches slskd's own
        directory structure when it's preserved locally); falls back to a
        basename search only when that fails, since two downloads from
        different peers sharing a basename can otherwise match the wrong
        file — the defect the old `_move_file()` had unconditionally.
        """
        username = transfer.username
        filename = transfer.filename.replace("\\", "/")
        basename = filename.rsplit("/", 1)[-1]

        source_dir = (
            self._config.paths.download_path / "complete" / "soulseek" / username
        )
        if not source_dir.exists():
            logger.warning("Source dir not found: %s", source_dir)
            return None

        direct = source_dir / filename
        if direct.is_file():
            return direct

        for candidate in source_dir.glob("**/*"):
            if candidate.is_file() and candidate.name == basename:
                logger.warning(
                    "Exact path not found under %s; matched by basename %s "
                    "instead — verify no other in-flight download shares "
                    "this filename",
                    source_dir,
                    basename,
                )
                return candidate

        logger.warning(
            "File not found in source dir: %s (basename: %s)", source_dir, basename
        )
        return None
