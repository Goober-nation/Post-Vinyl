"""slskd search: query ladder, stable-count exit, pooled helpful peers (#101).

Part of the slskd backend. For one request:

1. Queries L0 → TA → T (``core/strategy.py``) from the song's normalized
   names, each through the gate (``slskd_gate.py``: one at a time, rate cap,
   readiness). Each query is recorded in ``slskd_searches`` with its shape.
2. Each query stops on the **stable-count exit** (user, 2026-10-26): the file
   count unchanged for 8 s, polled every 2 s, never before 5 s; 30 s timeout;
   600 responses at most. slskd stores responses only when a search ends, so
   a search we stop is cancelled first and then read.
3. A **helpful peer** has at least one file the matcher accepts and isn't
   blocked. A query ends the ladder with ≥ 5 helpful peers; otherwise the
   next one runs and the peers of every query are pooled (user, 2026-10-29).
4. ``found`` with ≥ 1 helpful peer. ``notFound`` only once the gate trusts
   the empty results (``empty_trusted``); if it doesn't, the ladder runs again.

Candidates are never stored: ``candidates()`` re-reads the request's searches
from slskd by id (slskd keeps them) and ranks them again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.clients.slskd import PeerResponse, SearchState, SlskdClientBase, SlskdError
from app.core import matcher, states
from app.core.ranking import rank_peers
from app.core.strategy import track_queries
from app.services.slskd_gate import ParentLost, SlskdGate
from app.stores.database import Database
from app.stores.requests import Request, RequestsStore
from app.stores.slskd import SlskdStore
from app.stores.songs import Song

log = logging.getLogger(__name__)

STABLE_S = 8
POLL_S = 2
MIN_S = 5
TIMEOUT_S = 30
RESPONSE_CAP = 600
HELPFUL_TO_STOP = 5
LOSSLESS = frozenset({"flac", "alac", "wav", "aiff", "aif", "ape", "wv"})
STOP_DEADLINE_S = 30  # after a cancel, slskd must report the search ended by then


def extension(filename: str, reported: str = "") -> str:
    """The file's extension from its name, as slskd's own web UI shows it (0.26.1:
    ``getFileName(f.filename)``; the API's ``extension`` is mostly empty, live
    2026-10-29: 512 of 825 files)."""
    suffix = filename.replace("\\", "/").rsplit("/", 1)[-1].rpartition(".")
    return (suffix[2] if suffix[1] else reported).lower()


def file_key(
    filename: str,
    reported_ext: str,
    bitrate: int | None,
    bit_depth: int | None,
    sample_rate: int | None,
    size: int,
) -> tuple[bool, int, int, int]:
    """Best file first (user, 2026-10-29). Lossless before lossy; lossless =
    bit depth and sample rate reported (slskd's own rule, ``formatAttributes``)
    or a lossless extension. Among lossless, CD quality (16/44.1) first, then an
    unknown format, then hi-res by depth × rate; among lossy, highest bitrate.
    Then the larger file."""
    lossless = bool(bit_depth and sample_rate) or (
        extension(filename, reported_ext) in LOSSLESS
    )
    if lossless:
        if (bit_depth, sample_rate) == (16, 44100):
            tier, quality = 0, 0
        elif not (bit_depth and sample_rate):
            tier, quality = 1, 0
        else:
            tier, quality = 2, (bit_depth or 0) * (sample_rate or 0)
        return (False, tier, -quality, -size)
    return (True, 0, -(bitrate or 0), -size)


@dataclass(frozen=True)
class Candidate:
    """A helpful peer and its best matching file."""

    username: str
    filename: str
    size: int
    length: int | None
    bitrate: int | None
    bit_depth: int | None
    sample_rate: int | None
    has_free_slot: bool
    upload_speed: int
    search_id: str


def matcher_song(song: Song) -> matcher.Song:
    return matcher.Song(
        title=song.query_title or song.title,
        artist=song.main_artist or song.artist_name,
        album=song.album_title or "",
        length_s=song.duration_s,
    )


def helpful(
    responses: Iterable[PeerResponse],
    song: matcher.Song,
    search_id: str,
    blocked: set[str],
) -> dict[str, Candidate]:
    """Best matching file per helpful peer."""
    out: dict[str, Candidate] = {}
    for peer in responses:
        if peer.username in blocked:
            continue
        files = [
            f
            for f in peer.files
            if matcher.is_match(f.filename, f.length, song, locked=f.locked)
        ]
        if not files:
            continue
        best = min(
            files,
            key=lambda f: file_key(
                f.filename, f.extension, f.bitrate, f.bit_depth, f.sample_rate, f.size
            ),
        )
        out[peer.username] = Candidate(
            peer.username, best.filename, best.size, best.length, best.bitrate,
            best.bit_depth, best.sample_rate, peer.has_free_slot, peer.upload_speed,
            search_id,
        )  # fmt: skip
    return out


def _cand_key(c: Candidate) -> tuple[bool, int, int, int]:
    return file_key(c.filename, "", c.bitrate, c.bit_depth, c.sample_rate, c.size)


def pool(found: Iterable[dict[str, Candidate]]) -> list[Candidate]:
    """One candidate per peer across queries (its best file), ranked."""
    best: dict[str, Candidate] = {}
    for per_query in found:
        for name, cand in per_query.items():
            old = best.get(name)
            if old is None or _cand_key(cand) < _cand_key(old):
                best[name] = cand
    return rank_peers(best.values())


class SlskdSearcher:
    def __init__(
        self,
        db: Database,
        client: SlskdClientBase,
        gate: SlskdGate,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._client = client
        self._gate = gate
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._requests = RequestsStore()
        self._store = SlskdStore()
        self._ban_days = gate.ban_days

    def _ban_cutoff(self) -> int:
        return int(self._wall()) - self._ban_days * 86_400

    # ------------------------------------------------------------ one query

    def run_query(self, query: str, still_ready: Callable[[], bool]) -> SearchState:
        """One slskd search with the stable-count exit; read by the gate."""
        try:
            search_id = self._client.start_search(query, RESPONSE_CAP, TIMEOUT_S)
            started = self._clock()
            last_count, last_change = -1, started
            while True:
                state = self._client.get_search(search_id)
                now = self._clock()
                if state.file_count != last_count:
                    last_count, last_change = state.file_count, now
                if state.is_complete:
                    return state
                if not still_ready():
                    self._client.cancel_search(search_id)
                    raise ParentLost
                elapsed = now - started
                if (
                    (elapsed >= MIN_S and now - last_change >= STABLE_S)
                    or elapsed >= TIMEOUT_S
                    or state.response_count >= RESPONSE_CAP
                ):
                    return self._stop(search_id)
                self._sleep(POLL_S)
        except SlskdError as e:
            if still_ready():  # slskd is up and refused: not a readiness problem
                raise
            log.info("slskd went away during search %r: %s", query, e)
            raise ParentLost from e

    def _stop(self, search_id: str) -> SearchState:
        """Cancel, then wait until slskd has stored the responses. slskd 0.26.2
        reports a cancelled search complete while its responses still read as
        ``[]`` for up to ~0.5 s (live 2026-10-29, #110), so wait for those too."""
        self._client.cancel_search(search_id)
        deadline = self._clock() + STOP_DEADLINE_S
        while not (
            (state := self._client.get_search(search_id)).is_complete
            and (not state.response_count or self._client.get_responses(search_id))
        ):
            if self._clock() >= deadline:
                raise SlskdError(f"search {search_id} didn't end after cancel")
            self._sleep(0.5)
        return state

    # ------------------------------------------------------------ a request

    def search(self, request: Request, song: Song) -> bool | None:
        """Run the ladder for a ``searching`` request; True found, False notFound,
        None when the request was moved meanwhile (e.g. dropped)."""
        target = matcher_song(song)
        queries = track_queries(target.title, target.artist)
        if not self._still_searching(request.id):  # dropped while queued
            return None
        laps = 0
        while True:
            laps += 1
            pooled: list[dict[str, Candidate]] = []
            first_empty: float | None = None
            for shape, query in queries:
                ran = self._gate.search(
                    query, self.run_query, lambda s: s.response_count
                )
                with self._db.transaction(write=True) as conn:
                    current = self._requests.get(conn, request.id)
                    if current is None or current.track_state != "searching":
                        return None
                    self._store.add_search(
                        conn, ran.result.id, request.id, query, shape, int(self._wall())
                    )
                    blocked = self._store.blocked_usernames(conn, self._ban_cutoff())
                peers = helpful(
                    self._client.get_responses(ran.result.id), target, ran.result.id,
                    blocked,
                )  # fmt: skip
                log.info(
                    "request %s %s %r: %d responses, %d files, %d helpful peers, id %s",
                    request.id, shape, query, ran.result.response_count,
                    ran.result.file_count, len(peers), ran.result.id,
                )  # fmt: skip
                pooled.append(peers)
                if not peers and first_empty is None:
                    first_empty = ran.started_at
                if len(peers) >= HELPFUL_TO_STOP:
                    break
            found = any(pooled)
            if found or self._gate.empty_trusted(
                first_empty if first_empty is not None else self._clock(),
                self.run_query,
                lambda s: s.response_count,
            ):
                return self._finish(request.id, found)
            # a throttled account blocks every request alike, so keep trying, loudly
            log.warning(
                "request %s: empty results not trusted (lap %d); searching again",
                request.id,
                laps,
            )

    def _still_searching(self, request_id: int) -> bool:
        with self._db.transaction() as conn:
            current = self._requests.get(conn, request_id)
        return current is not None and current.track_state == "searching"

    def _finish(self, request_id: int, found: bool) -> bool | None:
        new = states.search_done("searching", found=found)
        with self._db.transaction(write=True) as conn:
            if not self._requests.set_track_state(
                conn, request_id, new, expected="searching"
            ):
                return None
        return found

    def candidates(self, request_id: int, song: Song) -> list[Candidate]:
        """Helpful peers from every search of the request, re-read from slskd."""
        with self._db.transaction() as conn:
            searches = self._store.searches_for_request(conn, request_id)
            blocked = self._store.blocked_usernames(conn, self._ban_cutoff())
        target = matcher_song(song)
        return pool(
            helpful(self._client.get_responses(s.id), target, s.id, blocked)
            for s in searches
        )
