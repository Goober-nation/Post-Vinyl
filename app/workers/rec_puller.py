"""
RecPuller — Background worker that drives the recommendation pipeline.

Fetches ListenBrainz recommendations, classifies against library,
adds in-library tracks to a playlist, and searches/queues downloads
for missing tracks via slskd.
"""

import threading
import time
from collections.abc import Sequence

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.playlist_store import PlaylistStore
from app.db.recs_store import RecsStore
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError,
    RecommendationFetchError,
    SearchInitiationError,
    SearchNotFoundError,
    SearchRateLimitedError,
    SlskdConnectionError,
)
from app.logging_config import get_logger
from app.services import track_requester
from app.services.playlist_registry import resolve_playlist_id
from app.services.query_builder import build_search_queries
from app.services.rec_playlist import RecPlaylistService
from app.services.recommendation import normalize_text
from app.sse import EventHub

logger = get_logger(__name__)

# Delay between each to-download track's search+queue cycle. Without this,
# a pull with many missing tracks fires all of slskd's peer-connection
# attempts within the same few seconds, which overwhelms virtualized
# Docker networking (e.g. Docker Desktop on macOS) and can knock the
# Soulseek server connection itself offline. Spreading connections out
# over time keeps the peer-connection rate manageable.
DOWNLOAD_PACE_SECONDS = 2.0

# G1 fix: cap on total distinct peers tried per rec (original search +
# re-search combined) before giving up. queue() has a 45s HTTP timeout
# (DownloadService.queue) — an unbounded walk against slow/unresponsive
# peers can turn one rec into minutes of blocking sequential calls, live-
# confirmed 2026-08-13 to stall the whole pull for ~90s+ with just 2-3
# slow peers.
MAX_QUEUE_ATTEMPTS = 5

# How often the manual-download wait (P6.5-5) re-checks the DB while
# pausing rec queueing. Tests override it via the `manual_wait_poll`
# constructor param.
MANUAL_WAIT_POLL_SECONDS = 5.0

# How often run()'s loop wakes to check whether any category is due for a
# periodic pull. Independent of any category's own interval_days — this is
# just the check-in cadence, coarse because intervals are now day-granularity
# (P6.5-2). Tests override it via the `interval` constructor param.
DEFAULT_TICK_SECONDS = 3600

# Comfort Zone and Deep Cuts use their configured cadence. Fresh Picks has its
# own nightly cadence rather than sharing either interval.
CATEGORIES_WITH_INTERVAL = ("comfort_zone", "fresh_picks", "deep_cuts")
FRESH_PICKS_INTERVAL_SECONDS = 24 * 60 * 60


class RecPuller:
    """
    Polls ListenBrainz for recommendations, classifies against library,
    adds matches to playlist, and queues downloads for missing tracks.

    Runs as a daemon thread.  pull_once() is synchronous and independently
    testable; start() / stop() manage the thread.
    """

    def __init__(
        self,
        config,
        recs_service,
        library_service,
        search_service,
        download_service,
        database: Database,
        event_hub: EventHub,
        interval: int | None = None,
        manual_wait_poll: float | None = None,
    ) -> None:
        self._config = config
        self._recs_service = recs_service
        self._library_service = library_service
        self._search_service = search_service
        self._download_service = download_service
        self._store = RecsStore(database)
        set_state_store = getattr(recs_service, "set_state_store", None)
        if set_state_store is not None:
            set_state_store(self._store)
        self._download_store = DownloadStore(database)
        self._playlist_store = PlaylistStore(database)
        self._event_hub = event_hub
        # P6.7-7 (S12 gap): gets a completed rec download into its category
        # playlist — used by the per-pull retry pass (_add_downloaded_recs)
        # for completions whose add-on-completion hook missed (index lag).
        self._rec_playlist = RecPlaylistService(
            config, library_service, self._store, self._playlist_store
        )
        self.interval = interval if interval is not None else DEFAULT_TICK_SECONDS
        # P6.5-5: poll cadence for the manual-download wait (tests lower it).
        self._manual_wait_poll = (
            manual_wait_poll
            if manual_wait_poll is not None
            else MANUAL_WAIT_POLL_SECONDS
        )
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._pull_lock = threading.Lock()
        self._last_run_at: float | None = None
        # Per-category last-run — drives due-checking for CATEGORIES_WITH_INTERVAL
        # (P6.5-2). Updated for every category included (count > 0) in any
        # completed pull cycle, manual or periodic.
        self._category_last_run_at: dict[str, float | None] = {
            "comfort_zone": None,
            "fresh_picks": None,
            "deep_cuts": None,
        }
        # P6.5-4: last-run timestamps are persisted to SQLite (worker_state)
        # so a restart doesn't reset every category's interval clock. Loaded
        # lazily via _ensure_state_loaded() — the schema may not exist yet.
        self._state_loaded = False
        self._abort_requested = threading.Event()

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
        logger.info("RecPuller started (interval=%ds)", self.interval)

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
        logger.info("RecPuller stopped")

    def run(self) -> None:
        """Main loop — pulls until stopped.

        Waits a full interval before the first pull too, so a server
        restart doesn't itself trigger an immediate pull — only an
        elapsed interval does. Use trigger_pull() (POST /api/recs/pull)
        for an on-demand pull right after startup.
        """
        while not self._stopped.is_set():
            self._stopped.wait(self.interval)
            if self._stopped.is_set():
                break
            try:
                self.pull_once()
            except Exception:
                logger.exception("Unhandled error in pull_once")

    def pull_once(self) -> dict:
        """
        Public entry point used by the background poller (run()'s loop only).

        Only categories that are both individually *enabled* and due (per
        their own interval_days, P6.5-2/P6.5-3b) are included — see
        _due_counts(). There's no single master gate any more (P6.5-3b
        replaced it with 3 independent per-category enabled flags); if
        nothing is enabled+due, _due_counts() returns all zeros and
        _pull_once_locked() skips via its own "no category due" check.

        Skips (without touching last_run_at) if a pull is already in
        flight — the poller's `run()` loop and a manual `trigger_pull()`
        share the same underlying lock, so they can never overlap.
        """
        if not self._pull_lock.acquire(blocking=False):
            logger.info("RecPuller: pull_once skipped — a pull is already running")
            return {"skipped": "already running"}
        try:
            return self._pull_once_locked(counts_override=self._due_counts())
        finally:
            self._pull_lock.release()

    def trigger_pull(self, categories: Sequence[str] | None = None) -> bool:
        """
        Manual pull entry point (used by POST /api/recs/pull).

        Deliberately calls _pull_once_locked() directly rather than going
        through pull_once(), so it is unaffected by per-category due-ness
        (P6.5-2). ``categories`` selects the configured count for each
        explicitly requested category, regardless of its own schedule or
        periodic enabled flag. The enabled flags control automatic scheduling
        only; an explicit manual pull is always allowed for a configured
        category. Omitting ``categories`` keeps the legacy all-category
        behavior for API callers that do not need selection.

        Starts a pull in a background thread and returns immediately.
        Returns True if a pull was started, False if one was already
        running (caller should treat False as "already in progress").
        """
        counts = self._manual_counts(categories)
        if not self._pull_lock.acquire(blocking=False):
            return False

        def _wrapper() -> None:
            try:
                self._pull_once_locked(counts_override=counts)
            except Exception:
                logger.exception("Unhandled error in triggered pull_once")
            finally:
                self._pull_lock.release()

        threading.Thread(target=_wrapper, daemon=True).start()
        return True

    def _manual_counts(self, categories: Sequence[str] | None) -> dict[str, int]:
        """Build counts for an explicit manual category selection."""
        selected = (
            CATEGORIES_WITH_INTERVAL
            if categories is None
            else tuple(dict.fromkeys(categories))
        )
        unknown = set(selected) - set(CATEGORIES_WITH_INTERVAL)
        if unknown:
            raise ValueError(f"Unknown recommendation category: {sorted(unknown)}")
        if not selected:
            raise ValueError("At least one recommendation category is required")

        return {
            category: (
                self._fresh_picks_count()
                if category == "fresh_picks"
                else getattr(self._config.recs, f"{category}_count")
            )
            if category in selected
            else 0
            for category in CATEGORIES_WITH_INTERVAL
        }

    def is_running(self) -> bool:
        """True only while a pull is actively executing (not while idle/sleeping)."""
        return self._pull_lock.locked()

    def request_abort(self) -> bool:
        """
        Ask an in-flight pull to stop as soon as possible.

        Checked between tracks in the search/queue loop (and once right
        after the ListenBrainz fetch returns) — an already-dispatched
        HTTP call to LB or slskd is not interrupted mid-flight, but no
        further ones are started. Returns True if a pull was actually
        running when this was called.
        """
        was_running = self.is_running()
        self._abort_requested.set()
        if was_running:
            logger.info("RecPuller: abort requested for in-flight pull")
        return was_running

    def _attempt_queue(
        self, rec, job, candidates: list, tried_usernames: set[str]
    ) -> dict:
        """Try queueing each untried, already-viable/free-slot candidate in
        order (G1 fix: never give up while there are candidates left to
        try, and never pick a peer with no free slot).

        Bounded by MAX_QUEUE_ATTEMPTS *total* across both the original
        search and the re-search — `tried_usernames` is shared across both
        calls, so the count accumulates. Live testing showed an unbounded
        walk can turn one rec into many sequential 45s HTTP timeouts
        (DownloadService.queue()) when peers are slow rather than
        outright rejecting, stalling the whole pull for minutes.

        Returns a dict with "kind" of "success" (+ username/filename/
        search_id), "aborted" (a manual download took priority — caller
        must stop the whole pull loop), or "exhausted" (+ message: every
        candidate in `candidates` was tried and failed, or the attempt cap
        was reached).
        """
        last_message = "no candidates"
        for r in candidates:
            if len(tried_usernames) >= MAX_QUEUE_ATTEMPTS:
                last_message = (
                    f"gave up after {MAX_QUEUE_ATTEMPTS} peer attempts (cap reached)"
                )
                break
            if r.username in tried_usernames:
                continue
            if not self._wait_for_manual_downloads():
                return {"kind": "aborted"}
            tried_usernames.add(r.username)
            try:
                result = self._download_service.queue(
                    r.username,
                    [{"filename": r.filename, "size": r.size}],
                    search_id=job.search_id,
                )
            except Exception as e:  # noqa: BLE001 — queue() can raise impl-specific errors
                logger.warning(
                    "RecPuller: queue failed for %s - %s via %s: %s",
                    rec.artist,
                    rec.track,
                    r.username,
                    e,
                )
                last_message = str(e)
                continue
            if result.enqueued_count > 0:
                self._download_store.insert_pending(
                    search_id=job.search_id,
                    username=r.username,
                    filename=r.filename,
                    size=r.size,
                    is_rec_download=True,
                )
                return {
                    "kind": "success",
                    "username": r.username,
                    "filename": r.filename,
                    "search_id": job.search_id,
                }
            logger.info(
                "RecPuller: %s - %s via %s enqueued 0, trying next candidate",
                rec.artist,
                rec.track,
                r.username,
            )
            last_message = "queue returned 0 enqueued"
        return {"kind": "exhausted", "message": last_message}

    def _wait_for_manual_downloads(self) -> bool:
        """P6.5-5: pause rec queueing while a manual download is in flight.

        Manual (incl. MusicBrainz) downloads always take priority over rec
        downloads: a rec track is only searched/queued once no manual
        transfer is active. Polls the DB every `_manual_wait_poll` seconds.
        Returns False if an abort was requested or the worker is stopping —
        the caller then aborts the pull rather than queueing out of order.

        A manual download that is still merely *queued* only holds the gate
        for `download.manual_gate_minutes`. Without that cap a peer that
        parks the transfer in its own upload queue — routine on Soulseek,
        and observed live sitting there for 11+ minutes — would stop recs
        indefinitely, since such a row is neither an orphan nor a stale
        pending row. See DownloadStore.has_active_manual_downloads.
        """
        grace = (
            getattr(getattr(self._config, "download", None), "manual_gate_minutes", 10)
            * 60
        )
        while self._download_store.has_active_manual_downloads(int(grace)):
            if self._abort_requested.is_set() or self._stopped.is_set():
                return False
            self._stopped.wait(self._manual_wait_poll)
        return True

    def last_run_at(self) -> float | None:
        """Epoch seconds of the last pull attempt that actually did work, or None."""
        self._ensure_state_loaded()
        return self._last_run_at

    def category_last_run_at(self) -> dict[str, float | None]:
        """Epoch seconds of the last completed pull that included each category
        (count > 0), manual or periodic, or None if it's never been included."""
        self._ensure_state_loaded()
        return dict(self._category_last_run_at)

    def _ensure_state_loaded(self) -> None:
        """Load persisted last-run timestamps from SQLite (P6.5-4), once.

        Lazy because RecPuller may be constructed before the DB schema is
        initialized; the poller and routes only read these values after
        startup completes.
        """
        if self._state_loaded:
            return
        self._state_loaded = True

        def _as_float(raw: str | None) -> float | None:
            if raw is None:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        self._last_run_at = _as_float(
            self._store.get_worker_state("rec_puller.last_run_at")
        )
        for category in self._category_last_run_at:
            loaded = _as_float(
                self._store.get_worker_state(
                    f"rec_puller.category_last_run_at.{category}"
                )
            )
            if loaded is not None:
                self._category_last_run_at[category] = loaded
        if self._last_run_at is not None or any(
            v is not None for v in self._category_last_run_at.values()
        ):
            logger.info(
                "RecPuller: restored last-run state (last_run_at=%s, categories=%s)",
                self._last_run_at,
                self._category_last_run_at,
            )

    def _persist_state(self, now: float) -> None:
        """Write the current last-run timestamps to SQLite (P6.5-4)."""
        self._store.set_worker_state("rec_puller.last_run_at", str(now))
        for category, last in self._category_last_run_at.items():
            if last is not None:
                self._store.set_worker_state(
                    f"rec_puller.category_last_run_at.{category}", str(last)
                )

    def _due_counts(self) -> dict[str, int]:
        """Per-category counts for a periodic pull.

        Fresh Picks is checked independently on a 24-hour nightly cadence;
        it is not tied to Comfort Zone or Deep Cuts' configured intervals.
        """
        self._ensure_state_loaded()
        now = time.time()
        counts = {"comfort_zone": 0, "fresh_picks": 0, "deep_cuts": 0}
        for category in CATEGORIES_WITH_INTERVAL:
            if not getattr(self._config.recs, f"{category}_enabled"):
                continue
            last = self._category_last_run_at.get(category)
            if category == "fresh_picks":
                due = last is None or now - last >= FRESH_PICKS_INTERVAL_SECONDS
                count = self._fresh_picks_count()
            else:
                interval_days = getattr(self._config.recs, f"{category}_interval_days")
                due = last is None or now - last >= interval_days * 86400
                count = getattr(self._config.recs, f"{category}_count")
            if due:
                counts[category] = count
        return counts

    def next_periodic_pull_at(self) -> float | None:
        """Epoch seconds of the earliest upcoming periodic pull across
        *enabled* categories in CATEGORIES_WITH_INTERVAL, or None if none
        of them are enabled (no periodic pull is scheduled at all). A
        category that's never run yet is treated as due on the very next
        tick.
        """
        self._ensure_state_loaded()
        now = time.time()
        due_ats = []
        for category in CATEGORIES_WITH_INTERVAL:
            if not getattr(self._config.recs, f"{category}_enabled"):
                continue
            last = self._category_last_run_at.get(category)
            if category == "fresh_picks":
                interval_seconds = FRESH_PICKS_INTERVAL_SECONDS
            else:
                interval_seconds = (
                    getattr(self._config.recs, f"{category}_interval_days") * 86400
                )
            due_ats.append(now if last is None else last + interval_seconds)
        return min(due_ats) if due_ats else None

    def _fresh_picks_count(self) -> int:
        """Read the canonical Fresh Picks count from [fresh_picks].

        2026-08-13: the old `recs.fresh_picks_count` alias is gone — this
        section is the single source of truth. Defaults to 0 (category
        effectively disabled) if a caller supplies a config without it.
        """
        return int(getattr(getattr(self._config, "fresh_picks", None), "count", 0))

    def _group_by_category(self, recs) -> dict[str, list]:
        """Split in-library recs by category (source).

        P6.7-0b's no-fallback decision extends here: a rec whose source is
        unknown has no playlist to belong to. It is still recorded as
        in_library (with no playlist_id) and a warning is logged, but it is
        never added to any playlist.
        """
        grouped: dict[str, list] = {}
        for rec in recs:
            source = getattr(rec, "source", None)
            if source not in ("comfort_zone", "fresh_picks", "deep_cuts"):
                logger.warning(
                    "RecPuller: in-library rec %s - %s has unknown source %r; "
                    "no playlist to add it to (no fallback)",
                    rec.artist,
                    rec.track,
                    source,
                )
                continue
            grouped.setdefault(source, []).append(rec)
        return grouped

    def _limit_fresh_candidates(
        self, in_library: list, to_download: list
    ) -> tuple[list, list, int]:
        """Keep Fresh Picks at N obtainable tracks while retaining failures.

        The recommendation service intentionally over-fetches candidates. We
        should search the buffer only until enough Fresh Picks are obtainable,
        not queue every over-fetched track after the target is already full.
        """
        target = self._fresh_picks_count()
        fresh_in_library = [rec for rec in in_library if rec.source == "fresh_picks"][
            :target
        ]
        selected_ids = {id(rec) for rec in fresh_in_library}
        shaped_in_library = [
            rec
            for rec in in_library
            if rec.source != "fresh_picks" or id(rec) in selected_ids
        ]
        fresh_slots = max(0, target - len(fresh_in_library))
        return shaped_in_library, list(to_download), fresh_slots

    def _ensure_playlist(self, category: str, existing: list) -> str | None:
        """Find or create the Navidrome playlist for a category.

        Only called when this pull has song IDs to add to it — strict
        laziness (P6.7-1 decision 2026-08-13): a playlist deleted by the
        user stays deleted until there are tracks for it again. Returns the
        playlist ID, or None when creation fails. ID-tracked (see
        app.services.playlist_registry) so a rename performed directly in
        Navidrome doesn't orphan this lookup.
        """
        playlist_name = getattr(self._config.recs, f"{category}_playlist_name")
        return resolve_playlist_id(
            role=category,
            desired_name=playlist_name,
            existing=existing,
            store=self._playlist_store,
            library_service=self._library_service,
            create_if_missing=True,
        )

    def _write_category_playlist(
        self, category: str, playlist_id: str, song_ids: list[str], existing: list
    ) -> bool:
        """Append category songs, applying Fresh Picks' rolling cap."""
        if category != "fresh_picks":
            return self._library_service.add_to_playlist(playlist_id, song_ids)

        target = self._fresh_picks_count()
        if target <= 0:
            return True
        try:
            detail = self._library_service.get_playlist_detail(playlist_id)
        except Exception as e:  # noqa: BLE001 — playlist backends vary
            logger.error("RecPuller: get_playlist_detail failed for Fresh Picks: %s", e)
            return False

        current_ids = {song.song_id for song in detail.songs if song.song_id}
        new_ids = [song_id for song_id in song_ids if song_id not in current_ids]
        overflow = max(0, len(detail.songs) + len(new_ids) - target)
        # P6.7-7: the shared rotation threshold (default 1 = "rated <2★ or
        # unrated → Trash"). Aligned here so Fresh Picks' rolling cap and
        # the other categories' rotation agree on what counts as trash-worthy.
        threshold = getattr(
            getattr(self._config, "recs", None), "rotation_trash_rating", 1
        )
        eligible = [song for song in detail.songs if (song.rating or 0) <= threshold]
        to_drop = eligible[:overflow]

        if to_drop:
            trash_id = self._ensure_trash_playlist(existing)
            if not trash_id:
                logger.error(
                    "RecPuller: cannot trim Fresh Picks; Trash playlist unavailable"
                )
                return False
            dropped_ids = [song.song_id for song in to_drop if song.song_id]
            if dropped_ids and not self._library_service.add_to_playlist(
                trash_id, dropped_ids
            ):
                logger.error("RecPuller: failed to add evicted Fresh Picks to Trash")
                return False
            if dropped_ids and not self._library_service.remove_songs_from_playlist(
                playlist_id, dropped_ids
            ):
                logger.error("RecPuller: failed to remove evicted Fresh Picks")
                return False

        if not new_ids:
            return True
        return self._library_service.add_to_playlist(playlist_id, new_ids)

    def _ensure_trash_playlist(self, existing: list) -> str | None:
        """Find or lazily create the playlist consumed by TrashPurge."""
        return resolve_playlist_id(
            role="trash",
            desired_name="Trash",
            existing=existing,
            store=self._playlist_store,
            library_service=self._library_service,
            create_if_missing=True,
        )

    # ------------------------------------------------------------------
    # P6.7-7: rotation + downloaded-recs playlist linkage
    # ------------------------------------------------------------------

    def _rotate_playlists(self, counts: dict[str, int], existing: list) -> dict:
        """Evict rec-sourced tracks from each counted category's playlist.

        Only tracks **acquired via Soulseek** (a rec row with a completed
        download) are touched, and only if their current rating is at or
        below `recs.rotation_trash_rating` (or they're unrated): those are
        moved to the Trash playlist, where TrashPurge deletes the file.
        Rec-sourced tracks rated above the threshold are removed from the
        playlist but stay in the library. Tracks that matched the existing
        library (or were added by the user) are never touched — recs merely
        pointed at the user's own music.

        Runs before this pull's picks are added, so the new picks survive
        the cycle. The failsafe is structural: rows still queued, in-flight
        or permanently failed have no completed download, so they are never
        in the rotation candidate set and cannot block it.

        Returns {"trashed": int, "removed": int}.
        """
        stats = {"trashed": 0, "removed": 0}
        threshold = getattr(
            getattr(self._config, "recs", None), "rotation_trash_rating", 1
        )

        for category in CATEGORIES_WITH_INTERVAL:
            if counts[category] <= 0:
                continue
            playlist_name = getattr(self._config.recs, f"{category}_playlist_name")
            playlist_id = resolve_playlist_id(
                role=category,
                desired_name=playlist_name,
                existing=existing,
                store=self._playlist_store,
                library_service=self._library_service,
                create_if_missing=False,
            )
            if playlist_id is None:
                continue  # no playlist -> nothing to rotate (laziness)
            try:
                detail = self._library_service.get_playlist_detail(playlist_id)
            except Exception as e:  # noqa: BLE001 — playlist backends vary
                logger.error(
                    "RecPuller: get_playlist_detail failed for rotation (%s): %s",
                    category,
                    e,
                )
                continue

            rows = self._store.get_recs_for_playlist_rotation(playlist_id)
            if not rows:
                continue

            trash_id: str | None = None
            for entry in detail.songs:
                if not self._match_rotation_row(entry, rows):
                    continue  # user's own music — never touched
                if (entry.rating or 0) <= threshold:
                    if trash_id is None:
                        trash_id = self._ensure_trash_playlist(existing)
                        if trash_id is None:
                            logger.error(
                                "RecPuller: rotation abort for %s — Trash "
                                "playlist unavailable",
                                category,
                            )
                            break
                    if not self._library_service.add_to_playlist(
                        trash_id, [entry.song_id]
                    ):
                        logger.error(
                            "RecPuller: failed to move %s to Trash", entry.title
                        )
                        break
                    if not self._library_service.remove_songs_from_playlist(
                        playlist_id, [entry.song_id]
                    ):
                        logger.error(
                            "RecPuller: failed to remove %s from playlist %s",
                            entry.title,
                            category,
                        )
                        break
                    stats["trashed"] += 1
                    logger.info(
                        "RecPuller: rotated %s - %s (rating %d) to Trash",
                        entry.artist,
                        entry.title,
                        entry.rating,
                    )
                else:
                    if not self._library_service.remove_songs_from_playlist(
                        playlist_id, [entry.song_id]
                    ):
                        logger.error(
                            "RecPuller: failed to remove %s from playlist %s",
                            entry.title,
                            category,
                        )
                        break
                    stats["removed"] += 1
                    logger.info(
                        "RecPuller: rotated %s - %s (rating %d) out of playlist %s",
                        entry.artist,
                        entry.title,
                        entry.rating,
                        category,
                    )
        return stats

    def _match_rotation_row(self, entry, rows: list[dict]) -> dict | None:
        """Match a playlist entry against the playlist's downloaded-recs rows.

        MBID match first, normalized artist+track as fallback — the same
        identity rules the rest of the pipeline uses.
        """
        if entry.mbid:
            for row in rows:
                if row["mbid"] and entry.mbid.casefold() == row["mbid"].casefold():
                    return row
        entry_key = self._rec_key(entry.artist, entry.title)
        for row in rows:
            if self._rec_key(row["artist"], row["track"]) == entry_key:
                return row
        return None

    def _add_downloaded_recs(self, counts: dict[str, int]) -> None:
        """Retry pass: add downloaded recs to their category playlist.

        The add-on-completion hook in DownloadMonitor runs the moment a
        rec's file lands in the library, but Navidrome's index can lag the
        beets import, so the song may not be findable then. This runs every
        pull for the counted categories and clears rows that still have no
        playlist linkage (S12).
        """
        self._rec_playlist.retry_unplaylisted_downloads(
            tuple(
                category
                for category in CATEGORIES_WITH_INTERVAL
                if counts[category] > 0
            )
        )

    def _rec_key(self, artist: str, track: str) -> str:
        return f"{normalize_text(artist)}::{normalize_text(track)}"

    def _drop_active_recs(self, recs: list) -> list:
        """Filter out recs that already have an active ledger row (G2/G3/G4
        fix). Matches by mbid first, falling back to normalized
        artist+track — the same identity rules RecommendationService uses
        for its own library matching.
        """
        active_rows = self._store.get_active_recs()
        active_mbids = {row["mbid"].lower() for row in active_rows if row["mbid"]}
        active_keys = {
            self._rec_key(row["artist"], row["track"]) for row in active_rows
        }

        kept = []
        skipped = 0
        for rec in recs:
            if rec.mbid and rec.mbid.lower() in active_mbids:
                skipped += 1
                continue
            if self._rec_key(rec.artist, rec.track) in active_keys:
                skipped += 1
                continue
            kept.append(rec)

        if skipped:
            logger.info(
                "RecPuller: skipping %d rec(s) already active in ledger", skipped
            )
        return kept

    def _reconcile_stale_recs(self) -> None:
        """Mark ledger rows 'removed' when the library no longer has them
        (G5 fix): nothing else ever revisits a downloaded/in_library row
        against the real library, so a pruned or manually-deleted file
        leaves a permanently-lying ledger entry. Run once per pull, before
        the active-recs filter, so a removed rec becomes eligible again.
        """
        rows = [
            row
            for row in self._store.get_active_recs()
            if row["status"] in ("downloaded", "in_library")
        ]
        for row in rows:
            try:
                songs = self._library_service.search_library(row["track"])
            except Exception:  # noqa: BLE001 — library backend can raise many different errors
                logger.warning(
                    "RecPuller: reconcile probe failed for %s - %s, leaving as-is",
                    row["artist"],
                    row["track"],
                )
                continue

            key = self._rec_key(row["artist"], row["track"])
            still_present = any(
                self._rec_key(song.artist, song.title) == key for song in songs
            )
            if not still_present:
                logger.info(
                    "RecPuller: %s - %s no longer in library, marking removed",
                    row["artist"],
                    row["track"],
                )
                self._store.update_status(row["id"], "removed")

    def _pull_once_locked(self, counts_override: dict[str, int] | None = None) -> dict:
        """
        One recommendation cycle (sync, independently testable).

        MUST be called with `self._pull_lock` held (pull_once/trigger_pull
        both do this). Returns a summary dict.

        counts_override: if given (periodic path via pull_once(), already
        filtered to due+enabled categories by _due_counts()), used instead
        of the full configured counts. Manual pulls pass a map containing the
        selected categories and use those configured counts regardless of
        per-category due-ness or enabled state.
        """
        # A fresh pull always starts un-aborted, even if the previous run
        # was stopped via request_abort().
        self._abort_requested.clear()
        started_work = False
        try:
            # ------------------------------------------------------------------
            # 1. Gates
            #
            # Per-category recs.*_enabled (P6.5-3b) gates the periodic path
            # through _due_counts(). Manual pulls call this method directly
            # and intentionally bypass enabled/due checks as long as
            # ListenBrainz is configured.
            # ------------------------------------------------------------------
            if not self._config.listenbrainz.enabled:
                logger.info("RecPuller: skipped — listenbrainz disabled in config")
                return {"skipped": "listenbrainz disabled"}

            if counts_override is not None:
                counts = counts_override
            else:
                counts = {
                    category: (
                        self._fresh_picks_count()
                        if category == "fresh_picks"
                        else getattr(self._config.recs, f"{category}_count")
                    )
                    for category in ("comfort_zone", "fresh_picks", "deep_cuts")
                }
            if sum(counts.values()) == 0:
                reason = (
                    "no category due"
                    if counts_override is not None
                    else "all counts zero"
                )
                logger.info("RecPuller: skipped — %s", reason)
                return {"skipped": reason}

            started_work = True
            return self._run_pull(counts)
        finally:
            if started_work:
                now = time.time()
                self._last_run_at = now
                for category, count in counts.items():
                    if count > 0:
                        self._category_last_run_at[category] = now
                self._persist_state(now)

    def _run_pull(self, counts: dict[str, int]) -> dict:
        """The actual fetch → classify → playlist/queue pipeline (gates already passed)."""
        source_names = [k for k, v in counts.items() if v > 0]

        # ------------------------------------------------------------------
        # 2. SSE: pull started
        # ------------------------------------------------------------------
        self._event_hub.publish(
            "rec.pull_started",
            {"sources": source_names, "counts": counts},
        )

        # ------------------------------------------------------------------
        # 3. Fetch recommendations
        # ------------------------------------------------------------------
        logger.info(
            "RecPuller: fetching recommendations (comfort_zone=%d, fresh_picks=%d, deep_cuts=%d)",
            counts["comfort_zone"],
            counts["fresh_picks"],
            counts["deep_cuts"],
        )
        try:
            recs = self._recs_service.fetch_recommendations(counts)
        except (
            ListenBrainzDisabledError,
            ListenBrainzConnectionError,
            RecommendationFetchError,
        ) as e:
            logger.error("RecPuller: fetch failed: %s", e)
            return {"error": f"fetch failed: {e}"}

        self._publish_category_warnings()

        # G5 fix: reconcile stale downloaded/in_library rows against the
        # real library on every pull, regardless of whether this fetch
        # returned anything — the ledger can go stale from files pruned or
        # deleted outside a pull cycle, not just from what LB just sent.
        self._reconcile_stale_recs()

        # ------------------------------------------------------------------
        # 3a. Rotation (P6.7-7) + downloaded-recs retry pass (S12 gap)
        #
        # Both run even when the fetch returned nothing: a drained Deep
        # Cuts pool or a Comfort Zone wraparound still means this pull is
        # the category's cycle, so prior pulls' tracks rotate out and any
        # completed downloads missed by the add-on-completion hook (index
        # lag) finally reach the playlist. One list_playlists call serves
        # rotation's Trash lookup, the retry pass and the in-library adds
        # below.
        # ------------------------------------------------------------------
        try:
            existing = self._library_service.list_playlists()
        except Exception:  # noqa: BLE001 — playlist listing may fail for many reasons
            logger.warning("RecPuller: list_playlists failed, assuming none")
            existing = []

        rotation = self._rotate_playlists(counts, existing)
        if rotation["trashed"] or rotation["removed"]:
            logger.info(
                "RecPuller: rotation — %d to Trash, %d removed from playlists",
                rotation["trashed"],
                rotation["removed"],
            )
        self._add_downloaded_recs(counts)

        if not recs:
            logger.info("RecPuller: fetch returned no recommendations")
            self._event_hub.publish(
                "rec.pull_completed",
                {
                    "total": 0,
                    "in_library": 0,
                    "to_download": 0,
                    "queued": 0,
                    "failures": [],
                },
            )
            return {"fetched": 0}

        logger.info("RecPuller: fetched %d total recommendations", len(recs))

        if self._abort_requested.is_set():
            logger.info("RecPuller: aborted after fetch, before classify/download")
            self._event_hub.publish(
                "rec.pull_completed",
                {
                    "total": len(recs),
                    "in_library": 0,
                    "to_download": 0,
                    "queued": 0,
                    "failures": [],
                    "aborted": True,
                },
            )
            return {"fetched": len(recs), "aborted": True}

        # ------------------------------------------------------------------
        # 3b. Drop recs already active in the ledger (G2/G3/G4 fix):
        # without this, every pull re-fetches the same LB pool and
        # reprocesses recs it already has an active row for — duplicate
        # ledger rows, duplicate playlist adds, and downloaded tracks that
        # only reach their playlist a pull late. Rows in a terminal-failure
        # status (error/search_failed/queue_failed) are NOT considered
        # active, so those recs retry here as intended.
        # ------------------------------------------------------------------
        recs = self._drop_active_recs(recs)
        if not recs:
            logger.info("RecPuller: all fetched recs already active in ledger")
            self._event_hub.publish(
                "rec.pull_completed",
                {
                    "total": 0,
                    "in_library": 0,
                    "to_download": 0,
                    "queued": 0,
                    "failures": [],
                },
            )
            return {"fetched": 0}

        # ------------------------------------------------------------------
        # 4. Build library songs (probe library for every rec track)
        # ------------------------------------------------------------------
        library_songs: list = []
        seen_ids: set = set()
        for rec in recs:
            try:
                songs = self._library_service.search_library(rec.track)
            except Exception:  # noqa: BLE001 — library backend can raise many different errors
                logger.warning(
                    "RecPuller: library probe failed for %s - %s",
                    rec.artist,
                    rec.track,
                )
                continue
            for song in songs:
                if song.song_id not in seen_ids:
                    library_songs.append(song)
                    seen_ids.add(song.song_id)

        logger.info(
            "RecPuller: library probe — %d songs from %d recs",
            len(library_songs),
            len(recs),
        )

        # ------------------------------------------------------------------
        # 5. SSE: classifying
        # ------------------------------------------------------------------
        self._event_hub.publish(
            "rec.classifying",
            {"total": len(recs)},
        )

        # ------------------------------------------------------------------
        # 6. Classify
        # ------------------------------------------------------------------
        try:
            classification = self._recs_service.classify(recs, library_songs)
        except Exception as e:  # noqa: BLE001 — third-party classify() may raise unexpected errors
            logger.error("RecPuller: classification failed: %s", e)
            return {"error": f"classify failed: {e}"}

        in_library = classification.in_library
        to_download = classification.to_download
        in_library, to_download, fresh_download_slots = self._limit_fresh_candidates(
            in_library, to_download
        )

        logger.info(
            "RecPuller: classification — %d in library, %d to download",
            len(in_library),
            len(to_download),
        )

        playlist_id: str | None = None
        queued: list[dict] = []
        failures: list[dict] = []

        # ------------------------------------------------------------------
        # 7. Playlist (in_library). Rotation and the downloaded-recs retry
        #    pass already ran in step 3a, on `existing`.
        # ------------------------------------------------------------------
        if in_library:
            # P6.7-1: one playlist per category, named independently
            # (comfort_zone/fresh_picks/deep_cuts). The old merged "Recs"
            # playlist is gone. A category's playlist is only ever created
            # or reused when this pull actually has song IDs to add to it —
            # a user-deleted playlist stays gone until there are tracks for
            # it again (lazy recreation, phase note 2026-08-11).
            playlist_id_by_category: dict[str, str | None] = {}
            for category, category_recs in self._group_by_category(in_library).items():
                song_ids: list[str] = []
                for rec in category_recs:
                    match_song = self._recs_service._find_library_match(
                        rec, library_songs
                    )
                    if match_song and match_song.song_id:
                        song_ids.append(match_song.song_id)
                    else:
                        logger.warning(
                            "RecPuller: no song_id for in-library rec %s - %s",
                            rec.artist,
                            rec.track,
                        )

                playlist_id = None
                if song_ids:
                    playlist_id = self._ensure_playlist(category, existing)

                if song_ids and playlist_id:
                    try:
                        written = self._write_category_playlist(
                            category, playlist_id, song_ids, existing
                        )
                        if written:
                            logger.info(
                                "RecPuller: added %d in-library songs to playlist %s",
                                len(song_ids),
                                playlist_id,
                            )
                        else:
                            logger.error(
                                "RecPuller: playlist write failed for %s", playlist_id
                            )
                    except Exception as e:  # noqa: BLE001 — Navidrome addToPlaylist may raise many error types
                        logger.error("RecPuller: add_to_playlist failed: %s", e)

                playlist_id_by_category[category] = playlist_id

            # Every in-library rec is recorded — including unknown-source
            # ones, which get no playlist (P6.7-0b: no fallback).
            for rec in in_library:
                self._store.insert_rec(
                    source=rec.source,
                    artist=rec.artist,
                    track=rec.track,
                    mbid=rec.mbid,
                    status="in_library",
                    playlist_id=playlist_id_by_category.get(rec.source),
                )

        # ------------------------------------------------------------------
        # 8. Search + Queue (to_download)
        # ------------------------------------------------------------------
        aborted = False
        fresh_downloaded = 0
        for i, rec in enumerate(to_download):
            if self._abort_requested.is_set():
                logger.info(
                    "RecPuller: aborted — stopping before %d of %d remaining to_download tracks",
                    len(to_download) - i,
                    len(to_download),
                )
                aborted = True
                break

            if rec.source == "fresh_picks" and fresh_downloaded >= fresh_download_slots:
                continue

            # P6.5-5: manual downloads always beat recs — wait for any
            # in-flight manual transfer to clear before searching for this
            # track.
            if not self._wait_for_manual_downloads():
                logger.info(
                    "RecPuller: aborted — stopping before %d of %d remaining to_download tracks (manual-download wait)",
                    len(to_download) - i,
                    len(to_download),
                )
                aborted = True
                break

            if i > 0:
                self._stopped.wait(DOWNLOAD_PACE_SECONDS)

            # P6.5-6: build the query via the pipeline (feat truncation,
            # paren qualifiers, 2-word cap) and walk the re-query ladder
            # until a rung's pass ratio clears the threshold; if none does,
            # fall back to the best rung by ratio (2026-08-10 decision). The
            # ladder lives in the shared `track_requester` driver, shared
            # with the MusicBrainz resolve job.
            queries = build_search_queries(rec.track, rec.artist)
            artist_words = track_requester.artist_words(rec.artist)

            # best_job is the rung that produced best_filtered — NOT
            # necessarily the last rung attempted. Everything downstream
            # (queue, pending row, rec row) must be keyed to the search the
            # chosen peer actually came from, or alternative-peer retry picks
            # from a different query's response pool.
            best_job, best_filtered, search_error = track_requester.run_ladder(
                self._search_service, self._config, rec.track, rec.artist
            )

            if search_error is not None:
                logger.warning(
                    "RecPuller: search failed for %s - %s: %s",
                    rec.artist,
                    rec.track,
                    search_error,
                )
                failures.append(
                    {"artist": rec.artist, "track": rec.track, "message": search_error}
                )
                self._store.insert_rec(
                    source=rec.source,
                    artist=rec.artist,
                    track=rec.track,
                    mbid=rec.mbid,
                    status="search_failed",
                )
                continue

            # best_job is None only when no rung ever returned results, in
            # which case best_filtered is empty too.
            if not best_filtered or best_job is None:
                logger.warning(
                    "RecPuller: no viable candidate for %s - %s",
                    rec.artist,
                    rec.track,
                )
                failures.append(
                    {
                        "artist": rec.artist,
                        "track": rec.track,
                        "message": "no viable candidate",
                    }
                )
                self._store.insert_rec(
                    source=rec.source,
                    artist=rec.artist,
                    track=rec.track,
                    mbid=rec.mbid,
                    status="search_failed",
                )
                continue

            # G1 fix: never pick a peer with no free slot, and never give up
            # after one candidate — walk every free-slot candidate from this
            # search, then re-search once and walk fresh candidates too,
            # before finally giving up on the rec.
            free_slot_results = [r for r in best_filtered if r.has_free_slot]
            if not free_slot_results:
                logger.warning(
                    "RecPuller: no free-slot candidate for %s - %s (%d candidates, all busy)",
                    rec.artist,
                    rec.track,
                    len(best_filtered),
                )
                failures.append(
                    {
                        "artist": rec.artist,
                        "track": rec.track,
                        "message": "no free-slot candidate",
                    }
                )
                self._store.insert_rec(
                    source=rec.source,
                    artist=rec.artist,
                    track=rec.track,
                    mbid=rec.mbid,
                    status="search_failed",
                )
                continue

            tried_usernames: set[str] = set()
            outcome = self._attempt_queue(
                rec, best_job, free_slot_results, tried_usernames
            )

            if (
                outcome["kind"] == "exhausted"
                and len(tried_usernames) < MAX_QUEUE_ATTEMPTS
            ):
                logger.info(
                    "RecPuller: all %d free-slot candidates failed for %s - %s, re-searching",
                    len(free_slot_results),
                    rec.artist,
                    rec.track,
                )
                requery_job = None
                requery_results: list = []
                try:
                    requery_job = self._search_service.search(queries[0])
                    requery_results = self._search_service.get_results(
                        requery_job.search_id
                    )
                except (
                    SlskdConnectionError,
                    SearchNotFoundError,
                    SearchInitiationError,
                    SearchRateLimitedError,
                ) as e:
                    logger.warning(
                        "RecPuller: re-search failed for %s - %s: %s",
                        rec.artist,
                        rec.track,
                        e,
                    )

                fresh_candidates = [
                    r
                    for r in requery_results
                    if r.has_free_slot
                    and r.username not in tried_usernames
                    and track_requester.is_viable_candidate(r, artist_words)
                ]
                if fresh_candidates and requery_job is not None:
                    outcome = self._attempt_queue(
                        rec, requery_job, fresh_candidates, tried_usernames
                    )
                # else: leave `outcome` as the original "exhausted" result —
                # its message is still the most useful one to record.

            if outcome["kind"] == "aborted":
                logger.info(
                    "RecPuller: aborted — manual download queued during search for %s - %s",
                    rec.artist,
                    rec.track,
                )
                aborted = True
                break

            if outcome["kind"] == "success":
                logger.info(
                    "RecPuller: queued %s - %s from %s",
                    rec.artist,
                    rec.track,
                    outcome["username"],
                )
                queued.append(
                    {
                        "artist": rec.artist,
                        "track": rec.track,
                        "username": outcome["username"],
                        "filename": outcome["filename"],
                    }
                )
                self._store.insert_rec(
                    source=rec.source,
                    artist=rec.artist,
                    track=rec.track,
                    mbid=rec.mbid,
                    status="queued",
                    search_id=outcome["search_id"],
                )
                if rec.source == "fresh_picks":
                    fresh_downloaded += 1
                continue

            message = outcome.get("message", "no viable candidate")
            logger.warning(
                "RecPuller: giving up on %s - %s after trying %d peer(s): %s",
                rec.artist,
                rec.track,
                len(tried_usernames),
                message,
            )
            failures.append(
                {"artist": rec.artist, "track": rec.track, "message": message}
            )
            self._store.insert_rec(
                source=rec.source,
                artist=rec.artist,
                track=rec.track,
                mbid=rec.mbid,
                status="queue_failed",
            )

        # ------------------------------------------------------------------
        # 9. SSE: pull completed
        # ------------------------------------------------------------------
        self._event_hub.publish(
            "rec.pull_completed",
            {
                "total": len(recs),
                "in_library": len(in_library),
                "to_download": len(to_download),
                "queued": len(queued),
                "playlist_id": playlist_id or None,
                "aborted": aborted,
                "failures": [
                    {
                        "artist": f["artist"],
                        "track": f["track"],
                        "message": f["message"],
                    }
                    for f in failures
                ],
            },
        )

        # ------------------------------------------------------------------
        # 10. Return summary
        # ------------------------------------------------------------------
        return {
            "fetched": len(recs),
            "in_library": len(in_library),
            "to_download": len(to_download),
            "queued": len(queued),
            "playlist_id": playlist_id or None,
            "aborted": aborted,
            "failures": failures,
        }

    def _publish_category_warnings(self) -> None:
        """Publish persisted/category-local warnings for the frontend."""
        warnings = self._store.category_warnings()
        service_warnings = getattr(self._recs_service, "category_warnings", None)
        if service_warnings is not None:
            warnings = {**warnings, **service_warnings()}
        for category, message in warnings.items():
            self._event_hub.publish(
                "rec.warning", {"category": category, "message": message}
            )
