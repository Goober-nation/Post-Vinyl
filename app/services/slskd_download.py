"""slskd download: candidates, attempts, stall rule, peer bans (#102).

Part of the slskd backend. ``start`` takes a ``found`` request: it writes the
``downloads`` row (before slskd is called), moves the request to
``midDownload`` and queues the best candidate. ``poll`` (the download monitor
calls it every few seconds) follows each running attempt:

- queued longer than ``queue_wait_seconds`` (15 s) → ``noStart``, except a
  file picked in the slskd tab, which waits as long as the peer queues it;
- downloading → ``core/stall.py`` decides ``noStart`` / ``tooSlow``;
- slskd ends it: succeeded → ``completed``, otherwise ``failed``.

A failed attempt cancels the transfer, counts a failure for the peer (every
outcome counts; ``peer_ban_failures`` → banned ``peer_ban_days``) and queues
the next untried, unbanned candidate on the **same** downloads row, up to
``candidates_per_download``. With none left the request ends by its last
attempt: never started (``noStart``, or failed with no bytes) →
``notDownloaded``; started and broke → ``badDownload`` (user, 2026-10-29). A
completed download clears the peer's record. ``next_peer`` (the user skips
the running attempt, #111) ends it as ``skipped``: the peer isn't blamed, and
the next candidate is queued like after a failure.

Each attempt saves into its own folder, ``post-vinyl/<download>/<attempt>``,
passed to slskd as the batch destination, so ``file_location`` is known
exactly (slskd 0.26.1: destination + the remote file's sanitized base name).
Transfers this app didn't start are never looked at: only attempts in the DB.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.clients.slskd import SlskdClientBase, SlskdError, Transfer
from app.config import SlskdSettings
from app.core import states
from app.core.stall import stall_outcome
from app.services.slskd_search import Candidate, SlskdSearcher
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdAttempt, SlskdStore
from app.stores.songs import SongsStore

log = logging.getLogger(__name__)

DESTINATION_ROOT = "post-vinyl"


def local_name(remote_filename: str) -> str:
    """The name slskd saves a remote file under (0.26.1 ``GetFileNameSafely`` on
    Linux): the last segment split on both slashes, NUL replaced, "."/".." → "_"."""
    name = remote_filename.replace("\\", "/").rsplit("/", 1)[-1].replace("\0", "_")
    return "_" if name in (".", "..") else name


def destination(download_id: int, attempt_id: int) -> str:
    return f"{DESTINATION_ROOT}/{download_id}/{attempt_id}"


def transfer_phase(state: str) -> str:
    """``done``, ``failed``, ``downloading`` or ``queued`` from slskd's state."""
    if "Completed" in state:
        return "done" if "Succeeded" in state else "failed"
    if "InProgress" in state:
        return "downloading"
    return "queued"  # Requested, Queued (Remotely/Locally), Initializing


@dataclass
class _Watch:
    """In-memory progress of one running attempt (a restart starts it fresh)."""

    seen_at: float
    samples: list[tuple[float, int]] = field(default_factory=list)


class SlskdDownloader:
    def __init__(
        self,
        db: Database,
        client: SlskdClientBase,
        searcher: SlskdSearcher,
        settings: SlskdSettings,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._client = client
        self._searcher = searcher
        self._settings = settings
        self._clock = clock
        self._wall = wall_clock
        self._requests = RequestsStore()
        self._downloads = DownloadsStore()
        self._store = SlskdStore()
        self._songs = SongsStore()
        self._watch: dict[int, _Watch] = {}  # attempt id → progress
        self._lock = threading.Lock()  # poll and start may run on different threads

    def _now(self) -> int:
        return int(self._wall())

    def _ban_cutoff(self) -> int:
        return self._now() - self._settings.peer_ban_days * 86_400

    # ------------------------------------------------------------ start

    def start(self, request_id: int, pinned: Candidate | None = None) -> bool:
        """A ``found`` request: row first, then the first candidate. False when
        the request moved meanwhile (e.g. dropped). ``pinned``: a file the user
        picked (#108), tried first; the request has no searches of its own, so
        there is no other candidate after it."""
        with self._lock, self._db.transaction(write=True) as conn:
            req = self._requests.get(conn, request_id)
            if req is None or req.track_state != "found":
                return False
            row = self._downloads.create(conn, request_id, "slskd", self._now())
            self._requests.set_track_state(
                conn, request_id, states.start_download("found"), expected="found"
            )
        self._next(row.id, request_id, pinned)
        return True

    def _candidates(self, download_id: int, request_id: int) -> list[Candidate]:
        with self._db.transaction(write=True) as conn:
            req = self._requests.get(conn, request_id)
            song = self._songs.get(conn, req.song_id) if req and req.song_id else None
            tried = self._store.tried_usernames(conn, download_id)
            blocked = self._store.blocked_usernames(conn, self._ban_cutoff())
        if song is None:
            return []
        return [
            c
            for c in self._searcher.candidates(request_id, song)
            if c.username not in tried and c.username not in blocked
        ]

    def _next(
        self, download_id: int, request_id: int, pinned: Candidate | None = None
    ) -> None:
        """Queue the next candidate, or end the download when none is left."""
        while True:
            with self._db.transaction() as conn:
                attempts = self._store.attempts_for_download(conn, download_id)
            if len(attempts) >= self._settings.candidates_per_download:
                return self._give_up(download_id, request_id, attempts)
            cands = (
                [pinned]
                if pinned and not attempts
                else self._candidates(download_id, request_id)
            )
            if not cands:
                return self._give_up(download_id, request_id, attempts)
            cand = cands[0]
            with self._db.transaction(write=True) as conn:
                attempt = self._store.add_attempt(
                    conn, download_id, cand.search_id, len(attempts), cand.username,
                    cand.filename, cand.size, self._now(),
                )  # fmt: skip
            try:
                result = self._client.queue_download(
                    cand.username,
                    [(cand.filename, cand.size)],
                    search_id=cand.search_id,
                    destination=destination(download_id, attempt.id),
                )
            except SlskdError as e:  # peer offline, slskd refused, …
                log.info("download %s: queueing from %s failed: %s", download_id,
                         cand.username, e)  # fmt: skip
                self._end(attempt, "failed")
                continue
            transfer = next(
                (t for t in result.transfers if t.filename == cand.filename), None
            )
            if transfer is None:
                log.info("download %s: %s refused %s: %s", download_id, cand.username,
                         cand.filename, result.failures.get(cand.filename))  # fmt: skip
                self._end(attempt, "failed")
                continue
            with self._db.transaction(write=True) as conn:
                self._store.set_transfer_id(conn, attempt.id, transfer.id)
                self._downloads.set_state(conn, download_id, "queued")  # a new peer
            self._watch[attempt.id] = _Watch(self._clock())
            log.info("download %s: attempt %s queued from %s (rank %s)", download_id,
                     attempt.id, cand.username, attempt.rank)  # fmt: skip
            return None

    # ------------------------------------------------------------ ending

    def _end(self, attempt: SlskdAttempt, outcome: str) -> None:
        """Close the attempt; every failure counts toward the peer's ban."""
        self._watch.pop(attempt.id, None)
        with self._db.transaction(write=True) as conn:
            self._store.end_attempt(conn, attempt.id, outcome, self._now())
            if outcome == "completed":
                self._store.clear_peer(conn, attempt.username)
                return
            if outcome == "skipped":  # the user's choice, not the peer's fault
                return
            failures = self._store.record_peer_failure(conn, attempt.username)
            if failures >= self._settings.peer_ban_failures:
                self._store.block_peer(conn, attempt.username, self._now())
                log.info("peer %s banned after %d failures", attempt.username, failures)

    def _give_up(
        self, download_id: int, request_id: int, attempts: list[SlskdAttempt]
    ) -> None:
        last = attempts[-1] if attempts else None
        broke = last is not None and last.outcome == "tooSlow"
        if last is not None and last.outcome == "failed" and last.transfer_id:
            broke = self._bytes_of(last) > 0
        with self._db.transaction(write=True) as conn:
            self._downloads.set_state(conn, download_id, "failed")
            self._requests.set_track_state(
                conn, request_id, states.download_failed("midDownload", bad=broke),
                expected="midDownload",
            )  # fmt: skip
        log.info("download %s: gave up after %d attempts (%s)", download_id,
                 len(attempts), "badDownload" if broke else "notDownloaded")  # fmt: skip

    def _bytes_of(self, attempt: SlskdAttempt) -> int:
        try:
            return self._client.get_download(
                attempt.username, attempt.transfer_id or ""
            ).bytes_transferred
        except SlskdError:
            return 0

    def _cancel(self, attempt: SlskdAttempt) -> None:
        try:
            self._client.cancel_download(
                attempt.username, attempt.transfer_id or "", forget=False
            )
        except SlskdError as e:
            log.info("cancelling transfer %s failed: %s", attempt.transfer_id, e)

    def next_peer(self, download_id: int) -> bool:
        """Skip the running attempt and queue the next candidate (#111).
        False when the download isn't running."""
        with self._lock:
            with self._db.transaction() as conn:
                download = self._downloads.get(conn, download_id)
                running = self._store.running_for_download(conn, download_id)
                req = (
                    self._requests.get(conn, download.request_id) if download else None
                )
            if download is None or running is None or req is None:
                return False
            if req.track_state != "midDownload":
                return False
            if running.transfer_id:
                self._cancel(running)
            self._end(running, "skipped")
            log.info("download %s: attempt %s skipped by the user", download_id,
                     running.id)  # fmt: skip
            self._next(download_id, req.id)
        return True

    # ------------------------------------------------------------ polling

    def poll(self) -> None:
        """One round over every running attempt (the download monitor's tick)."""
        with self._lock:
            with self._db.transaction() as conn:
                running = self._store.running_attempts(conn)
            for attempt in running:
                if attempt.transfer_id is None:
                    continue  # being queued right now
                try:
                    transfer = self._client.get_download(
                        attempt.username, attempt.transfer_id
                    )
                except SlskdError as e:
                    log.info("transfer %s unreadable: %s", attempt.transfer_id, e)
                    continue
                self._follow(attempt, transfer)

    def _follow(self, attempt: SlskdAttempt, transfer: Transfer) -> None:
        with self._db.transaction() as conn:
            download = self._downloads.get(conn, attempt.download_id)
            req = self._requests.get(conn, download.request_id) if download else None
        if download is None or req is None:
            return
        if req.track_state != "midDownload":  # dropped: stop, not the peer's fault
            self._cancel(attempt)
            self._watch.pop(attempt.id, None)
            with self._db.transaction(write=True) as conn:
                self._store.end_attempt(conn, attempt.id, "failed", self._now())
                self._downloads.set_state(conn, download.id, "cancelled")
            return
        now = self._clock()
        watch = self._watch.setdefault(attempt.id, _Watch(now))
        phase = transfer_phase(transfer.state)
        outcome: str | None = None
        if phase == "done":
            return self._complete(attempt, download.id, req.id)
        if phase == "failed":
            log.info("attempt %s failed at slskd: %s %s", attempt.id, transfer.state,
                     transfer.exception)  # fmt: skip
            outcome = "failed"
        elif phase == "queued" and not watch.samples:
            # a file the user picked waits in the peer's queue as long as it
            # takes: it has no other candidate (user, 2026-10-30, #108)
            picked = req.source == "slskd" and req.query is None
            if not picked and now - watch.seen_at > self._settings.queue_wait_seconds:
                outcome = "noStart"
        elif phase == "downloading" or watch.samples:
            if download.state == "queued":  # bytes flow: the tab shows it (#111)
                with self._db.transaction(write=True) as conn:
                    self._downloads.set_state(conn, download.id, "downloading")
            watch.samples.append((now, transfer.bytes_transferred))
            outcome = stall_outcome(watch.samples, now)
        if outcome is None:
            return None
        if outcome != "failed":
            self._cancel(attempt)
        self._end(attempt, outcome)
        self._next(download.id, req.id)
        return None

    def _complete(
        self, attempt: SlskdAttempt, download_id: int, request_id: int
    ) -> None:
        location = (
            f"{destination(download_id, attempt.id)}/{local_name(attempt.filename)}"
        )
        self._end(attempt, "completed")
        with self._db.transaction(write=True) as conn:
            self._downloads.complete(conn, download_id, location, self._now())
            self._requests.set_track_state(
                conn, request_id, states.download_done("midDownload"),
                expected="midDownload",
            )  # fmt: skip
        log.info("download %s completed: %s", download_id, location)
