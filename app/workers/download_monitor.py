"""
DownloadMonitor — Background worker that polls slskd for transfer state
and drives the download lifecycle: tracking, retry, file-move, SSE events.
"""

import os
import threading
import time
from pathlib import Path

from app.db.database import Database
from app.db.download_store import ALLOWED_EXTENSIONS, DownloadStore
from app.db.recs_store import RecsStore
from app.db.search_store import SearchStore
from app.exceptions import SlskdConnectionError
from app.logging_config import get_logger
from app.services.beets import BeetsService
from app.services.rec_playlist import RecPlaylistService
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
        beets_service: object | None = None,
    ) -> None:
        self._config = config
        self._download_service = download_service
        self._library_service = library_service
        self._store = DownloadStore(database)
        # P-MB-1 wiring: source of "what the user actually asked for" for a
        # completed download, passed to beets so the import can be
        # constrained to it instead of trusting the peer's own tags.
        self._search_store = SearchStore(database)
        self._recs_store = RecsStore(database)
        # P6.7-7 (S12 gap): gets a completed rec download into its category
        # playlist the moment its file lands in the library.
        self._rec_playlist = RecPlaylistService(
            config, library_service, self._recs_store
        )
        self._event_hub = event_hub
        # P6.6-2: beets owns import (tag/rename/move) for completed
        # transfers, replacing _move_file(). Injectable for tests.
        self._beets_service = beets_service or BeetsService(config)
        self.interval = interval or config.download.check_interval
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        # transfer_id -> consecutive polls slskd hasn't reported it. Drives
        # orphan reconciliation; in-memory so a musica restart re-grants the
        # grace period rather than judging slskd on its first poll.
        self._missing_polls: dict[str, int] = {}
        # transfer_id -> wall-clock time its source file was first not found
        # on disk. Drives _handle_missing_source's timeout. In-memory rather
        # than `completed_at`: upsert_transfer refreshes completed_at to
        # "now" on every poll for as long as slskd keeps reporting a
        # transfer "completed" (see upsert_transfer's existing-row branch),
        # so a stale row that slskd reports as permanently completed would
        # never age past a completed_at-based timeout.
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

        for t in transfers:
            is_new, prev_state = self._store.upsert_transfer(t)

            # --- SSE: transfer.started (new OR pending-adopted OR queued→downloading) ---
            if t.state in ("queued", "downloading") and (
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
            if t.state == "downloading" and (is_new or prev_state != "downloading"):
                self._event_hub.publish(
                    "transfer.progress",
                    {
                        "transfer_id": t.transfer_id,
                        "progress": int(t.progress),
                        "speed": int(t.speed) if t.speed is not None else None,
                    },
                )

            # --- Failure handling (on transition into failed) ---
            if t.state == "failed" and prev_state != "failed":
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
                count = 0
                if t.fail_reason not in _CONNECTIVITY_FAIL_REASONS:
                    count = self._store.increment_peer_failure(t.username)
                if count and count >= self._config.download.bad_peer_threshold:
                    self._store.set_peer_blocked(t.username)
                    logger.warning(
                        "Peer %s blocked after %d failures",
                        t.username,
                        count,
                    )
                    blocked_msg = f"Peer blocked after {count} failures"

                will_retry, error_msg = self._attempt_retry(
                    t.transfer_id, t.username, t.filename
                )
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
                row = self._store.get_transfer(t.transfer_id)
                # Use the DB row's rec flag (set at queue time) — the slskd-parsed
                # Transfer always reports is_rec_download=False.
                is_rec = bool(row and row.get("is_rec_download"))
                # P6.8: a MusicBrainz-initiated download is a manual-priority
                # row (is_rec_download = 0) that must import into the "library"
                # profile, pinned to its exact recording MBID.
                is_library = bool(row and row.get("is_library_download"))
                mbid = row.get("mb_recording_id") if row else None
                if not self._store.import_handled(t.transfer_id):
                    moved = self._import_via_beets(
                        t, is_rec=is_rec, is_library=is_library, mbid=mbid
                    )
                    if moved:
                        moved_files.append(str(moved))
                        self._store.mark_file_moved(
                            t.transfer_id,
                            str(moved.parent),
                        )
                        should_emit = True

                row = self._store.get_transfer(t.transfer_id)
                target_dir = row["target_dir"] if row else ""

                # Reconcile this on every completed-transfer poll, not only
                # the first SSE emission. A row can already be `completed`
                # when musica adopts it, and a previous playlist lookup may
                # have lost a race with Navidrome's scan. Do it before the
                # event so the frontend's refresh sees the new rec state.
                self._sync_rec_completion(row, t.transfer_id)

                if should_emit:
                    self._event_hub.publish(
                        "transfer.completed",
                        {
                            "transfer_id": t.transfer_id,
                            "filename": t.filename,
                            "target_dir": target_dir or "",
                        },
                    )

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

        playlist_linked = self._rec_playlist.retry_unplaylisted_downloads()

        return {
            "transfers_seen": transfers_seen,
            "moved": moved_files,
            "scan_triggered": scan_triggered,
            "retried": retried,
            "playlist_linked": playlist_linked,
        }

    def _sync_rec_completion(self, row: dict | None, transfer_id: str) -> None:
        """Synchronize a completed rec row and retry its playlist linkage."""
        if not row or not row.get("is_rec_download") or not row.get("search_id"):
            return

        search_id = row["search_id"]
        rowcount = self._store.mark_rec_downloaded(search_id, transfer_id)
        if rowcount > 0:
            logger.info(
                "Recommendation marked downloaded: %s -> %s", search_id, transfer_id
            )

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    def _attempt_retry(
        self, transfer_id: str, username: str, filename: str
    ) -> tuple[bool, str]:
        """Try to re-queue a failed download from an alternative peer.

        Shared by the slskd-reported failure path and orphan reconciliation
        (a transfer slskd stopped reporting), which need identical retry
        semantics — same budget, same candidate pool, same bookkeeping.

        Returns (will_retry, error_message).
        """
        retry_count = self._store.get_retry_count(transfer_id)
        if retry_count >= self._config.download.max_retries_per_track:
            return (False, "Max retries exceeded")

        # Find search context
        search_id = self._store.get_pending_search_id(username, filename)
        if not search_id:
            return (False, "No search context for retry")

        # Re-read the search this track came from. slskd retains completed
        # searches, so this works after a restart without musica keeping a
        # copy — and it re-reads an existing search rather than starting a
        # new one, so retry still picks from the same candidate pool.
        try:
            responses = self._download_service.fetch_search_responses(search_id)
        except SlskdConnectionError as e:
            return (False, str(e))
        if not responses:
            return (False, "No search responses available for retry")

        peer, candidate = self._pick_alternative_peer(responses, username)
        if not (peer and candidate):
            return (False, "No viable alternative peer")

        original = self._store.get_transfer(transfer_id)
        pending_id = self._store.insert_pending(
            search_id=search_id,
            username=peer,
            filename=candidate["filename"],
            size=candidate.get("size", 0),
            is_rec_download=bool(original and original.get("is_rec_download")),
            is_library_download=bool(original and original.get("is_library_download")),
            mb_recording_id=original.get("mb_recording_id") if original else None,
            retry_count=retry_count + 1,
        )
        try:
            result = self._download_service.queue(
                peer, [candidate], search_id=search_id
            )
        except Exception as e:  # noqa: BLE001 — queue() can raise various impl-specific errors
            self._store.delete_transfers([pending_id])
            self._store.record_retry_attempt(transfer_id, peer, False)
            return (False, str(e))

        if result.enqueued_count <= 0:
            self._store.delete_transfers([pending_id])
            self._store.record_retry_attempt(transfer_id, peer, False)
            return (False, "Queue returned 0 enqueued")

        self._store.increment_retry_count(transfer_id)
        self._store.record_retry_attempt(transfer_id, peer, True)
        self._abandon_at_slskd(transfer_id, username, filename)
        logger.info("Retry queued: %s from %s", filename, peer)
        return (True, "")

    def _abandon_at_slskd(self, transfer_id: str, username: str, filename: str) -> None:
        """Drop the superseded transfer from slskd once a retry is queued.

        Without this musica gives up on a peer while slskd keeps working on
        it: a 'failed' state is often transient (queued-then-retried by
        slskd itself, or a peer that comes back), so the original transfer
        can complete *after* musica has already re-queued the same track
        elsewhere — and both copies land. Live-confirmed 2026-08-11: a
        Comfort Zone pull of 5 tracks downloaded 2 of them twice, from the
        exact peer pairs this method would have separated (whitelamp+N+3 and
        M3H9X+bob-bob-bob-123).

        Best-effort: a failure here only risks the duplicate we already
        have today, so it must never break the retry that just succeeded.
        """
        try:
            removed = self._download_service.delete_transfer(transfer_id)
        except Exception:
            logger.warning(
                "Could not remove superseded transfer %s from slskd; a "
                "duplicate download may land if the original completes",
                transfer_id,
                exc_info=True,
            )
            return
        if removed:
            logger.info(
                "Removed superseded transfer from slskd: %s / %s", username, filename
            )
        else:
            logger.warning(
                "slskd refused to remove superseded transfer %s / %s; a "
                "duplicate download may land if the original completes",
                username,
                filename,
            )

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
        live = self._store.get_adopted_live_transfers()
        live_ids = {row["id"] for row in live}

        # Drop counters for rows that came back or are no longer live.
        self._missing_polls = {
            tid: misses
            for tid, misses in self._missing_polls.items()
            if tid in live_ids and tid not in reported_ids
        }

        orphans = []
        for row in live:
            if row["id"] in reported_ids:
                continue
            misses = self._missing_polls.get(row["id"], 0) + 1
            self._missing_polls[row["id"]] = misses
            if misses >= int(grace):
                orphans.append(row)

        retried: list[str] = []
        for row in orphans:
            self._missing_polls.pop(row["id"], None)
            self._store.mark_failed([row["id"]])
            logger.warning(
                "Download orphaned — slskd stopped reporting it after %d polls: "
                "%s / %s",
                grace,
                row["username"],
                row["filename"],
            )
            will_retry, error_msg = self._attempt_retry(
                row["id"], row["username"], row["filename"]
            )
            if will_retry:
                retried.append(row["id"])
            self._event_hub.publish(
                "transfer.failed",
                {
                    "transfer_id": row["id"],
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
        DownloadStore.fail_stale_pending for why 'failed' is the right
        resting state.
        """
        timeout = getattr(
            getattr(self._config, "download", None), "pending_timeout_minutes", 5
        )
        try:
            stale = self._store.fail_stale_pending(int(timeout) * 60)
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
            self._event_hub.publish(
                "transfer.failed",
                {
                    "transfer_id": row["id"],
                    "error": (
                        f"slskd never picked up this download within {timeout} minutes"
                    ),
                    "will_retry": False,
                },
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pick_alternative_peer(
        self,
        responses: list[dict],
        skip_username: str,
    ) -> tuple[str | None, dict | None]:
        """Pick an alternative peer from search responses."""
        ban_seconds = self._config.download.peer_ban_days * 86400
        for response in responses:
            peer = response.get("username", "")
            if not peer or peer == skip_username:
                continue
            if self._store.is_peer_blocked(peer, ban_seconds):
                continue
            if (
                self._store.get_peer_failure_count(peer)
                >= self._config.download.bad_peer_threshold
            ):
                continue
            if not response.get("hasFreeUploadSlot"):
                continue

            # Find first audio file
            for f in response.get("files", []):
                fname = f.get("filename", "")
                ext = os.path.splitext(fname)[1].lower()
                if ext in ALLOWED_EXTENSIONS:
                    return (peer, {"filename": fname, "size": f.get("size", 0)})

        return (None, None)

    def _import_via_beets(
        self,
        transfer,
        is_rec: bool,
        is_library: bool = False,
        mbid: str | None = None,
    ) -> Path | None:
        """
        Hand a completed download off to beets for tag/rename/move.
        Returns the target path beets placed it at, or None if beets is
        disabled, the source file couldn't be located, or the import failed
        — in every None case the source file is left untouched.

        `is_library` / `mbid` (P6.8) route a MusicBrainz-initiated download
        into the "library" profile pinned to `mbid`. Such rows are manual
        (is_rec=False), so `is_rec` stays False and the MBID is authoritative
        — the intent title/artist are still threaded when a search row exists,
        but BeetsService ignores them once an mbid is given.
        """
        if not getattr(getattr(self._config, "beets", None), "enabled", False):
            logger.debug(
                "beets disabled; leaving completed file in place: %s",
                transfer.filename,
            )
            return None

        source = self._resolve_source_path(transfer)
        if source is None:
            self._handle_missing_source(transfer)
            return None
        # Self-healed: a source that was missing on an earlier poll turned
        # up before the timeout — drop the tracking entry so a later,
        # unrelated miss on this same transfer_id (there shouldn't be one,
        # but the file is beets' to consume from here) starts its own clock.
        self._missing_source_since.pop(transfer.transfer_id, None)

        title, artist, category = self._resolve_intent(transfer, is_rec)
        result = self._beets_service.import_file(
            source,
            is_rec=is_rec,
            title=title,
            artist=artist,
            category=category,
            library=is_library,
            mbid=mbid,
        )
        if not result.ok:
            if getattr(result, "duplicate", False):
                # Terminal, not a failure: the track is already in the
                # library, so re-importing will be skipped identically on
                # every future poll. Record it as handled so the monitor
                # stops re-running the import each cycle. The downloaded
                # file is deliberately left on disk rather than deleted —
                # disposal is TrashPurge's job, not beets'.
                #
                # It is recorded as *skipped*, not moved. This used to call
                # mark_file_moved(transfer_id, "") — file_moved = 1 with an
                # empty target_dir — which told every reader of the row that
                # the file had been filed away while it was in fact still
                # sitting in downloads/complete/soulseek/ with nothing
                # pointing at it. See migration 007.
                self._store.mark_import_skipped(transfer.transfer_id)
                logger.info(
                    "Duplicate download for %s left in place at %s (already "
                    "in library); not retrying",
                    transfer.transfer_id,
                    source,
                )
                return None
            logger.warning(
                "beets import failed for %s: %s", transfer.transfer_id, result.error
            )
            return None

        self._store.set_import_unmatched(transfer.transfer_id, not result.matched)
        return result.target_path

    def _handle_missing_source(self, transfer) -> None:
        """A transfer reports 'completed' but its file is nowhere on disk.

        Usually transient — slskd can report completion a moment before the
        file is visible on the mount — so this only acts once
        `missing_source_timeout_minutes` has passed since the source was
        *first* seen missing, with no source ever found in between; every
        poll before that is a silent no-op, left to retry next cycle exactly
        as before. Tracked in `_missing_source_since` rather than the row's
        `completed_at`: upsert_transfer refreshes `completed_at` to "now" on
        every poll for as long as slskd keeps reporting a transfer
        "completed" (see its existing-row branch), so a row slskd reports as
        permanently completed would never age past a completed_at-based
        timeout.

        Past the timeout, retrying is pointless: nothing about the file
        reappearing is a function of time anymore. The dominant real case is
        a stale row adopted from slskd's own transfer history (which
        outlives a musica reset) pointing at a file the reset just deleted —
        that file will never exist.

        Marks it with `mark_import_skipped()`, the same terminal marker the
        duplicate-download path uses, rather than `mark_failed()`: slskd
        keeps reporting this transfer as "completed" forever (it is not
        wrong — the network transfer genuinely did complete), so
        `upsert_transfer` would silently overwrite a `state='failed'` back
        to `'completed'` on the very next poll. `import_skipped` is what
        `import_handled()` actually gates on, so it is the one flag that
        durably stops the poll-every-cycle retry regardless of what `state`
        says.
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
        self._store.mark_import_skipped(transfer_id)
        logger.warning(
            "Giving up on %s / %s: transfer completed but no source file "
            "was found on disk within %d minute(s); not retrying further",
            transfer.username,
            transfer.filename,
            timeout_minutes,
        )

    def _resolve_intent(
        self, transfer, is_rec: bool
    ) -> tuple[str | None, str | None, str | None]:
        """What the user actually asked for when this download was queued.

        Looked up via the transfer's `search_id`: a manual download's intent
        lives in the `searches` header row (query/artist), a rec's in the
        `recommendations` row it was queued from (track/artist/source) —
        recs never get a `searches` row (see SearchStore's docstring). The
        third element is the rec category (`source`) that decides which
        beets profile the file lands in (P6.7-0b); manual downloads and
        downloads with no search context yield None for it. Returns
        (None, None, None) when there's no search context to look up, which
        tells BeetsService to fall back to matching on the file's own tags.
        """
        row = self._store.get_transfer(transfer.transfer_id)
        search_id = row.get("search_id") if row else None
        if not search_id:
            return None, None, None
        if is_rec:
            rec = self._recs_store.get_rec_by_search_id(search_id)
            if not rec:
                return None, None, None
            return rec.get("track"), rec.get("artist"), rec.get("source")
        search = self._search_store.get_search(search_id)
        if not search:
            return None, None, None
        return search.get("query"), search.get("artist"), None

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
