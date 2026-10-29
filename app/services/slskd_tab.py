"""The slskd tab: typed searches, what any search found, queueing picked files (#108).

A typed query is one request (``record_slskd_query``); it runs as one search
through the gate, so it shares the rate cap and readiness checks with the
core loop. Its search row is stored as soon as slskd gives the id, and its
state is always read **live from slskd** by that id, never from the DB
(``core-db-redesign.md``). Repeating a query re-reads the stored results
(re-read, don't re-search); only a search that ended ``notFound`` runs again.

Every file of every response is shown, grouped by peer. A request with a
song gets the matcher's label per file; a typed query has no song, so only
excluded versions are marked, against the query's own words.

A picked file becomes its own request and song (user, 2026-10-30), with the
names from its path (``core/file_song.py``), queued as exactly that file:
the peer and file are checked against the search's stored responses first. While the queue is paused (#111) picks
are refused; a typed search waits in the gate.
Owns ``searching`` / ``found`` / ``notFound`` for typed and picked requests.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.clients.slskd import PeerFile, PeerResponse, SlskdClientBase, SlskdError
from app.core import matcher, states
from app.core.file_song import names_from_path
from app.core.normalize import credits_from_joined, query_names
from app.services.queue_pause import QueuePaused
from app.services.requests import RequestNotFound
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import Candidate, SlskdSearcher, matcher_song
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.requests import Request, RequestsStore
from app.stores.slskd import SlskdSearch, SlskdStore
from app.stores.songs import Song, SongFields, SongsStore

log = logging.getLogger(__name__)

TYPED_STRATEGY = "typed"
RECENT_LIMIT = 50


class FileNotInSearch(LookupError):
    """The picked peer/file isn't in the request's search results."""


@dataclass(frozen=True)
class FileView:
    filename: str
    size: int
    length: int | None
    bitrate: int | None
    bit_depth: int | None
    sample_rate: int | None
    locked: bool
    # matcher label ("correct", "excluded:live", "no_title", …); a typed
    # search only gets "excluded:<word>", "locked", "not_audio" or None
    label: str | None


@dataclass(frozen=True)
class PeerView:
    username: str
    has_free_slot: bool
    upload_speed: int
    queue_length: int
    files: list[FileView]


@dataclass(frozen=True)
class SearchView:
    id: str
    query: str
    strategy: str
    state: str | None  # slskd's own, e.g. "InProgress", "Completed, TimedOut"
    is_complete: bool
    response_count: int
    file_count: int
    peers: list[PeerView]  # only once complete; slskd stores them at the end


@dataclass(frozen=True)
class TypedSearch:
    request: Request
    latest: SearchView | None  # without peers


@dataclass(frozen=True)
class Pick:
    username: str
    filename: str


class SlskdTabBase(ABC):
    @abstractmethod
    def search(self, user_id: int, query: str) -> Request:
        """Start (or re-read) a typed search; returns its request at once."""

    @abstractmethod
    def recent(self, user_id: int, limit: int = RECENT_LIMIT) -> list[TypedSearch]: ...

    @abstractmethod
    def searches(self, user_id: int, request_id: int) -> list[SearchView]:
        """Every slskd search of one of the user's requests, newest first."""

    @abstractmethod
    def queue(self, user_id: int, request_id: int, picks: list[Pick]) -> list[Request]:
        """One request + song per picked file of a typed search, queued."""


def label_typed(f: PeerFile, query: str) -> str | None:
    if f.locked:
        return "locked"
    if not matcher.is_audio(f.filename):
        return "not_audio"
    word = matcher.excluded_word(f.filename, query)
    return f"excluded:{word}" if word else None


def group(
    responses: list[PeerResponse], label: Callable[[PeerFile], str | None]
) -> list[PeerView]:
    return [
        PeerView(
            p.username,
            p.has_free_slot,
            p.upload_speed,
            p.queue_length,
            [
                FileView(
                    f.filename,
                    f.size,
                    f.length,
                    f.bitrate,
                    f.bit_depth,
                    f.sample_rate,
                    f.locked,
                    label(f),
                )
                for f in p.files
            ],
        )
        for p in responses
    ]


def song_fields(f: PeerFile) -> SongFields:
    title, artist = names_from_path(f.filename)
    names = query_names(title, credits_from_joined(((artist, ""),)), None)
    return SongFields(
        title=title,
        artist_name=artist,
        query_title=names.track,
        main_artist=names.artist,
        duration_s=f.length,
    )


class SlskdTab(SlskdTabBase):
    def __init__(
        self,
        db: Database,
        client: SlskdClientBase,
        gate: SlskdGate,
        searcher: SlskdSearcher,
        downloader: SlskdDownloader,
        clock: Callable[[], float] = time.time,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
        paused: Callable[[], bool] = lambda: False,
        sources: SourcePreferences | None = None,  # None: slskd always on (tests)
    ) -> None:
        self._paused = paused
        self._sources = sources
        self._db = db
        self._client = client
        self._gate = gate
        self._searcher = searcher
        self._downloader = downloader
        self._clock = clock
        self._spawn = run_in_background or (
            lambda fn: threading.Thread(
                target=fn, name="slskd-typed", daemon=True
            ).start()
        )
        self._requests = RequestsStore()
        self._songs = SongsStore()
        self._store = SlskdStore()

    def _now(self) -> int:
        return int(self._clock())

    def _owned(self, user_id: int, request_id: int) -> Request:
        with self._db.transaction() as conn:
            req = self._requests.get(conn, request_id)
        if req is None or req.user_id != user_id:
            raise RequestNotFound(f"request {request_id}")
        return req

    @staticmethod
    def _typed(req: Request) -> bool:
        return req.source == "slskd" and req.query is not None

    # ------------------------------------------------------------ typed search

    def _require_slskd(self, user_id: int) -> None:
        """Nothing goes to slskd while it's off for the user (user, 2026-10-30)."""
        if self._sources is not None:
            self._sources.require(user_id, "download", "slskd")

    def search(self, user_id: int, query: str) -> Request:
        query = " ".join(query.split())
        if not query:
            raise ValueError("empty search")
        self._require_slskd(user_id)
        with self._db.transaction(write=True) as conn:
            req = self._requests.record_slskd_query(conn, user_id, query, self._now())
            old = req.track_state
            if old in ("notFound", "dropped"):  # ended: may run again
                old = states.re_request(old)
                self._requests.set_track_state(
                    conn, req.id, old, expected=req.track_state
                )
            if old not in ("requested", "reRequested"):
                return req  # searching now, or found: re-read what is stored
            new = states.start_search(old)
            self._requests.set_track_state(conn, req.id, new, expected=old)
        started = Request(**{**vars(req), "track_state": new})
        self._spawn(lambda: self._run(started))
        return started

    def _run(self, req: Request) -> None:
        assert req.query is not None

        def store(search_id: str) -> None:
            with self._db.transaction(write=True) as conn:
                self._store.add_search(
                    conn,
                    search_id,
                    req.id,
                    req.query or "",
                    TYPED_STRATEGY,
                    self._now(),
                )

        found = False
        try:
            ran = self._gate.search(
                req.query,
                lambda q, ready: self._searcher.run_query(q, ready, on_start=store),
                lambda s: s.response_count,
            )
            found = ran.result.file_count > 0
            log.info("typed search %s %r: %d responses, %d files", req.id, req.query,
                     ran.result.response_count, ran.result.file_count)  # fmt: skip
        except Exception:  # any error: the request must not stay searching
            log.exception("typed search %s %r failed", req.id, req.query)
        with self._db.transaction(write=True) as conn:
            self._requests.set_track_state(
                conn, req.id, states.search_done("searching", found=found),
                expected="searching",
            )  # fmt: skip

    def recent(self, user_id: int, limit: int = RECENT_LIMIT) -> list[TypedSearch]:
        with self._db.transaction() as conn:
            reqs = self._requests.slskd_queries(conn, user_id, min(max(limit, 1), 200))
            latest = {r.id: self._store.searches_for_request(conn, r.id) for r in reqs}
        return [
            TypedSearch(
                r, self._view(found[-1], None) if (found := latest[r.id]) else None
            )
            for r in reqs
        ]

    # ------------------------------------------------------------ any request

    def _view(
        self, s: SlskdSearch, label: Callable[[PeerFile], str | None] | None
    ) -> SearchView:
        try:
            st = self._client.get_search(s.id)
        except SlskdError as e:  # slskd down or the search was removed there
            log.info("search %s unreadable: %s", s.id, e)
            return SearchView(s.id, s.query, s.strategy, None, False, 0, 0, [])
        complete = st.is_complete
        peers: list[PeerView] = []
        if complete and label is not None:  # the list (no label) shows no files
            responses = self._client.get_responses(s.id)
            # slskd 0.26.2 reports a cancelled search complete before its
            # responses read back (#110): not complete until they do
            complete = bool(responses) or not st.response_count
            peers = group(responses, label) if complete else []
        return SearchView(s.id, s.query, s.strategy, st.state, complete,
                          st.response_count, st.file_count, peers)  # fmt: skip

    def searches(self, user_id: int, request_id: int) -> list[SearchView]:
        req = self._owned(user_id, request_id)
        with self._db.transaction() as conn:
            found = self._store.searches_for_request(conn, request_id)
            song = self._songs.get(conn, req.song_id) if req.song_id else None
        if self._typed(req):
            query = req.query or ""

            def label(f: PeerFile) -> str | None:
                return label_typed(f, query)
        else:
            target = matcher_song(song) if song else None

            def label(f: PeerFile) -> str | None:
                if target is None:
                    return None
                return matcher.label_file(f.filename, f.length, target, locked=f.locked)

        return [self._view(s, label) for s in reversed(found)]

    # ------------------------------------------------------------ picked files

    def _find(
        self, request_id: int, pick: Pick
    ) -> tuple[SlskdSearch, PeerResponse, PeerFile]:
        with self._db.transaction() as conn:
            found = self._store.searches_for_request(conn, request_id)
        for s in reversed(found):
            for peer in self._client.get_responses(s.id):
                if peer.username != pick.username:
                    continue
                for f in peer.files:
                    if f.filename == pick.filename and not f.locked:
                        return s, peer, f
        raise FileNotInSearch(f"{pick.username}: {pick.filename}")

    def queue(self, user_id: int, request_id: int, picks: list[Pick]) -> list[Request]:
        req = self._owned(user_id, request_id)
        if not self._typed(req):
            raise ValueError("files can only be picked from a typed Soulseek search")
        if not picks:
            raise ValueError("no files picked")
        self._require_slskd(user_id)
        if self._paused():
            raise QueuePaused("the queue is paused; picks wait until it's resumed")
        found = [
            self._find(request_id, p) for p in picks
        ]  # all checked before any write
        out: list[Request] = []
        for search, peer, f in found:
            with self._db.transaction(write=True) as conn:
                song: Song = self._songs.create(conn, song_fields(f))
                pick = self._requests.create_pick(conn, user_id, song.id, self._now())
                state = states.search_done(
                    states.start_search(pick.track_state), found=True
                )
                self._requests.set_track_state(
                    conn, pick.id, state, expected=pick.track_state
                )
            cand = Candidate(
                peer.username, f.filename, f.size, f.length, f.bitrate, f.bit_depth,
                f.sample_rate, peer.has_free_slot, peer.upload_speed, search.id,
            )  # fmt: skip
            self._downloader.start(pick.id, cand)
            with self._db.transaction() as conn:
                out.append(self._requests.get(conn, pick.id) or pick)
        return out
