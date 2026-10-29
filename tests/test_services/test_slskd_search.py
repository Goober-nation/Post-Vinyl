"""slskd search: stable-count exit, helpful peers, ladder and pooling (#101)."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.clients.slskd import PeerFile, PeerResponse, SearchState, SlskdError
from app.clients.slskd_fake import FakeSlskdClient
from app.config import SlskdSettings
from app.core import matcher
from app.services.slskd_gate import ParentLost, SlskdGate
from app.services.slskd_search import (
    HELPFUL_TO_STOP,
    SlskdSearcher,
    extension,
    helpful,
    matcher_song,
    pool,
)
from app.services.slskd_source import SlskdSource
from app.stores.database import Database
from app.stores.requests import Request, RequestsStore
from app.stores.slskd import SlskdStore
from app.stores.songs import Song, SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
SONG = matcher.Song("Feather", "Nujabes", "", 175)
GOOD = r"Music\Nujabes\Modal Soul\04 Feather.mp3"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def f(path: str = GOOD, ext: str = "mp3", bitrate: int | None = 320, size: int = 1,
      length: int | None = 175, locked: bool = False, depth: int | None = None,
      rate: int | None = None) -> PeerFile:  # fmt: skip
    return PeerFile(path, size, ext, length, bitrate, depth, rate, locked)


def peer(
    name: str, *files: PeerFile, slot: bool = True, speed: int = 100
) -> PeerResponse:
    return PeerResponse(name, slot, speed, 0, files or (f(),))


# ------------------------------------------------------------ helpful peers


def test_helpful_needs_a_matching_unlocked_file_and_unblocked_peer() -> None:
    responses = [
        peer("ok"),
        peer("live", f(r"Music\Nujabes\Feather (Live).mp3")),
        peer("locked", f(locked=True)),
        peer("other", f(r"Music\Someone\04 Feather.mp3")),
        peer("short", f(length=60)),
        peer("banned"),
    ]
    assert list(helpful(responses, SONG, "s1", {"banned"})) == ["ok"]


def test_best_file_is_lossless_then_bitrate_then_size() -> None:
    files = (
        f(GOOD, "mp3", 320, 9),
        f(GOOD, "mp3", 256, 99),
        f(GOOD.replace(".mp3", ".flac"), "", None, 30, depth=16, rate=44100),
    )
    best = helpful([peer("p", *files)], SONG, "s1", set())["p"]
    assert best.filename.endswith(".flac")
    lossy = helpful([peer("p", *files[:2])], SONG, "s1", set())["p"]
    assert lossy.bitrate == 320  # highest bitrate, not the bigger file


def test_cd_quality_beats_hi_res() -> None:
    """One peer's two Dracula copies (slskd web UI, 2026-10-29): 16/44.1 wins."""
    song = matcher.Song("Dracula", "Tame Impala", "", 205)
    files = (
        f(r"music\Tame Impala\Deadbeat (2025)\03 - Dracula.flac", "", None, 69_350_000,
          205, depth=24, rate=96000),
        f(r"music\Tame Impala\Dracula (2025)\01 - Dracula.flac", "", None, 22_890_000,
          205, depth=16, rate=44100),
        f(r"music\Tame Impala\Dracula (2025)\01 - Dracula.lrc", "", None, 2_080, None),
    )  # fmt: skip
    best = helpful([peer("p", *files)], song, "s1", set())["p"]
    assert best.bit_depth == 16 and best.sample_rate == 44100


def test_lossless_order_cd_then_unknown_then_hi_res_by_quality() -> None:
    a = f(GOOD.replace(".mp3", ".flac"), "", None, 9, depth=24, rate=48000)
    b = f(GOOD.replace(".mp3", ".flac"), "", None, 8, depth=24, rate=96000)
    c = f(GOOD.replace(".mp3", ".flac"), "", None, 7)  # flac, no depth/rate reported
    d = f(GOOD.replace(".mp3", ".flac"), "", None, 6, depth=16, rate=44100)
    order = [d, c, b, a]
    for i in range(len(order)):
        best = helpful([peer("p", *order[i:])], SONG, "s1", set())["p"]
        assert best.size == order[i].size


def test_lossless_by_attributes_even_without_a_lossless_suffix() -> None:
    """slskd's own rule: bit depth + sample rate reported means lossless."""
    files = (f(GOOD, "", 320, 9), f(GOOD.replace(".mp3", ".m4a"), "", None, 5,
                                    depth=16, rate=44100))  # fmt: skip
    assert helpful([peer("p", *files)], SONG, "s1", set())["p"].filename.endswith(
        ".m4a"
    )


def test_pool_keeps_best_file_per_peer_and_ranks() -> None:
    a = helpful(
        [peer("a", slot=False, speed=900), peer("b", speed=10)], SONG, "s1", set()
    )
    b = helpful([peer("a", f(GOOD.replace(".mp3", ".flac"), "flac", 1000), slot=False,
                      speed=900)], SONG, "s2", set())  # fmt: skip
    ranked = pool([a, b])
    assert [c.username for c in ranked] == ["b", "a"]  # free slot first
    assert ranked[1].search_id == "s2" and ranked[1].filename.endswith(".flac")


def test_matcher_uses_normalized_names() -> None:
    song = Song(1, "Feather", "Nujabes featuring Cise Starr & Akin from CYNE",
                "Feather", "Nujabes", None, None, None, None, 175)  # fmt: skip
    assert matcher.is_match(GOOD, 175, matcher_song(song))
    bare = Song(2, "Feather", "Nujabes", None, None, None, None, None, None, None)
    assert matcher_song(bare).artist == "Nujabes"  # no normalized names: as given


# ------------------------------------------------------------ stable-count exit


class Scripted(FakeSlskdClient):
    """get_search reports file counts from a script, one per poll."""

    def __init__(self, counts: list[int], responses: int = 10) -> None:
        super().__init__()
        self.counts, self.responses = counts, responses
        self.polls = 0

    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        sid = super().start_search(query, response_limit, idle_timeout_s)
        self.searches[sid] = replace(self.searches[sid], is_complete=False)
        return sid

    def get_search(self, search_id: str) -> SearchState:
        s = self.searches[search_id]
        if search_id in self.cancelled:
            return replace(s, is_complete=True)
        count = self.counts[min(self.polls, len(self.counts) - 1)]
        self.polls += 1
        return replace(s, file_count=count, response_count=self.responses)


def searcher(
    slskd: FakeSlskdClient, db: Database | None = None
) -> tuple[SlskdSearcher, Clock]:
    clock = Clock()
    gate = SlskdGate(slskd, SlskdSettings(), clock, clock.sleep)
    return SlskdSearcher(
        db or Database(":memory:"), slskd, gate, clock, clock.sleep
    ), clock


def test_stops_when_count_is_stable_for_8_s() -> None:
    slskd = Scripted([0, 10, 20, 20, 20, 20, 20, 20])
    s, clock = searcher(slskd)
    state = s.run_query("q", lambda: True)
    assert state.is_complete and slskd.cancelled
    assert clock.t - 1000 == 2 * 6  # last change at 4 s, stable until 12 s


def test_never_stops_before_5_s_and_times_out_at_30_s() -> None:
    s, clock = searcher(Scripted([0]))
    s.run_query("q", lambda: True)
    assert clock.t - 1000 == 8  # nothing ever arrives: stable 8 s (> 5 s minimum)
    s, clock = searcher(Scripted(list(range(100))))  # always growing
    s.run_query("q", lambda: True)
    assert clock.t - 1000 == 30


def test_stops_at_the_response_cap() -> None:
    s, clock = searcher(Scripted(list(range(100)), responses=600))
    s.run_query("q", lambda: True)
    assert clock.t == 1000


def test_lost_readiness_cancels_and_raises() -> None:
    slskd = Scripted([0, 5, 10])
    s, _ = searcher(slskd)
    with pytest.raises(ParentLost):
        s.run_query("q", lambda: False)
    assert slskd.cancelled


# ------------------------------------------------------------ the ladder


Setup = tuple[SlskdSearcher, FakeSlskdClient, Database, Request, Song]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields(
            "Feather", "Nujabes featuring Cise Starr & Akin from CYNE",
            query_title="Feather", main_artist="Nujabes", duration_s=175,
        ))  # fmt: skip
        req = RequestsStore().create(conn, uid, "catalog", NOW, song_id=song.id)
        RequestsStore().set_track_state(conn, req.id, "searching")
    slskd = FakeSlskdClient()
    # a healthy throttle check by default: an untrusted empty ladder retries forever
    slskd.results["2020"] = [peer(f"x{i}", f(r"x\y.mp3")) for i in range(300)]
    s, _ = searcher(slskd, db)
    return s, slskd, db, req, song


def state(db: Database, rid: int) -> str:
    with db.transaction() as conn:
        r = RequestsStore().get(conn, rid)
    assert r is not None
    return r.track_state


def strategies(db: Database, rid: int) -> list[str]:
    with db.transaction() as conn:
        return [x.strategy for x in SlskdStore().searches_for_request(conn, rid)]


def test_five_helpful_peers_end_the_ladder(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["feather nujabes"] = [peer(f"p{i}") for i in range(HELPFUL_TO_STOP)]
    assert s.search(req, song) is True
    assert strategies(db, req.id) == ["L0"] and state(db, req.id) == "found"


def test_fewer_than_five_pool_across_the_ladder(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["feather nujabes"] = [peer("a")]
    slskd.results["Feather"] = [peer("b"), peer("a")]
    assert s.search(req, song) is True
    assert strategies(db, req.id) == ["L0", "T"]  # TA == L0 for this song, dropped
    assert {c.username for c in s.candidates(req.id, song)} == {"a", "b"}


def test_not_found_once_empties_are_trusted(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["2020"] = [peer(f"x{i}", f(r"x\y.mp3")) for i in range(300)]
    assert s.search(req, song) is False
    assert state(db, req.id) == "notFound"
    assert strategies(db, req.id) == ["L0", "T"]  # the check search isn't the request's


class ThrottledOnce(FakeSlskdClient):
    """The first "2020" check comes back empty, later ones healthy."""

    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        if query == "2020":
            self.checks += 1
            n = 0 if self.checks == 1 else 300
            self.results["2020"] = [peer(f"x{i}", f(r"x\y.mp3")) for i in range(n)]
        return super().start_search(query, response_limit, idle_timeout_s)


def test_untrusted_empties_run_the_ladder_again(setup: Setup) -> None:
    _, _, db, req, song = setup
    s, _ = searcher(ThrottledOnce(), db)
    assert s.search(req, song) is False
    assert strategies(db, req.id) == ["L0", "T", "L0", "T"]


def test_dropped_meanwhile_stops(setup: Setup) -> None:
    s, _, db, req, song = setup
    with db.transaction() as conn:
        RequestsStore().set_track_state(conn, req.id, "dropped")
    assert s.search(req, song) is None
    assert state(db, req.id) == "dropped"


def test_blocked_peers_are_skipped(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["feather nujabes"] = [peer("bad")]
    slskd.results["2020"] = [peer(f"x{i}", f(r"x\y.mp3")) for i in range(300)]
    with db.transaction() as conn:
        SlskdStore().block_peer(conn, "bad", NOW)
    assert s.search(req, song) is False


# ------------------------------------------------------------ the source


def test_source_claims_queues_and_searches(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["feather nujabes"] = [peer(f"p{i}") for i in range(5)]
    with db.transaction() as conn:
        RequestsStore().set_track_state(conn, req.id, "reRequested")
        fresh = RequestsStore().get(conn, req.id)
    assert fresh is not None
    src = SlskdSource(db, s)
    src.start(fresh, song)
    src.start(fresh, song)  # stale second start: claim fails, not queued twice
    src.stop()
    assert state(db, req.id) == "found" and strategies(db, req.id) == ["L0"]


class Blocking(SlskdSearcher):
    """Holds the worker on its first request until released."""

    def __init__(self, inner: SlskdSearcher) -> None:
        self.inner, self.release, self.seen = inner, threading.Event(), []

    def search(self, request: Request, song: Song) -> bool | None:
        self.seen.append(request.id)
        self.release.wait(5)
        return self.inner.search(request, song)


def test_dropped_while_queued_runs_no_query(setup: Setup) -> None:
    s, slskd, db, req, song = setup
    slskd.results["2020"] = [peer(f"x{i}", f(r"x\y.mp3")) for i in range(300)]
    with db.transaction() as conn:
        store = RequestsStore()
        uid = UsersStore().get_by_username(conn, "alice")
        assert uid is not None
        store.set_track_state(conn, req.id, "requested")
        first = store.create(conn, uid.id, "catalog", NOW, song_id=song.id)
        queued = store.get(conn, req.id)
    assert queued is not None
    blocking = Blocking(s)
    src = SlskdSource(db, blocking)
    assert src.start(first, song)  # the worker holds on this one
    assert src.start(queued, song)
    with db.transaction() as conn:  # the user drops it (cancel = drop)
        RequestsStore().set_track_state(
            conn, queued.id, "dropped", expected="searching"
        )
    blocking.release.set()
    src.stop()
    assert strategies(db, queued.id) == []  # no slskd search spent on it
    assert state(db, queued.id) == "dropped"


class Crashing(SlskdSearcher):
    def __init__(self, inner: SlskdSearcher, crashes: int) -> None:
        self.inner, self.left, self.calls = inner, crashes, 0

    def search(self, request: Request, song: Song) -> bool | None:
        self.calls += 1
        if self.left:
            self.left -= 1
            raise RuntimeError("boom")
        return self.inner.search(request, song)


@pytest.mark.parametrize(
    ("crashes", "end", "calls"), [(1, "found", 2), (5, "notFound", 2)]
)
def test_crash_retries_once_then_ends_not_found(
    setup: Setup, crashes: int, end: str, calls: int
) -> None:
    s, slskd, db, req, song = setup
    slskd.results["feather nujabes"] = [peer(f"p{i}") for i in range(5)]
    with db.transaction() as conn:
        RequestsStore().set_track_state(conn, req.id, "requested")
        fresh = RequestsStore().get(conn, req.id)
    assert fresh is not None
    crashing = Crashing(s, crashes)
    src = SlskdSource(db, crashing)
    assert src.start(fresh, song)
    src.stop()
    assert state(db, req.id) == end and crashing.calls == calls


def test_second_start_reports_the_lost_claim(setup: Setup) -> None:
    s, _, db, req, song = setup
    with db.transaction() as conn:
        RequestsStore().set_track_state(conn, req.id, "requested")
        stale = RequestsStore().get(conn, req.id)
    assert stale is not None
    blocking = Blocking(s)
    src = SlskdSource(db, blocking)
    assert src.start(stale, song)
    assert not src.start(stale, song)
    blocking.release.set()
    src.stop()


# ------------------------------------------------------------ review fixes


def test_flac_with_empty_extension_field_still_wins() -> None:
    """slskd leaves `extension` empty for most files (live 2026-10-29)."""
    files = (f(GOOD, "", 320, 9), f(GOOD.replace(".mp3", ".flac"), "", None, 30))
    assert helpful([peer("p", *files)], SONG, "s1", set())["p"].filename.endswith(
        ".flac"
    )


def test_extension_comes_from_the_file_name() -> None:
    assert extension(r"a\b.c\03 - Dracula.FLAC", "") == "flac"
    assert extension(r"a\b.c\no suffix", "mp3") == "mp3"  # name has none: reported
    assert extension("x/y.mp3", "flac") == "mp3"  # the name wins, as in slskd's UI


class Refusing(Scripted):
    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        raise SlskdError("POST /searches: HTTP 400 bad query")


def test_slskd_refusing_while_ready_is_an_error_not_a_lost_parent() -> None:
    s, _ = searcher(Refusing([0]))
    with pytest.raises(SlskdError):
        s.run_query("q", lambda: True)
    with pytest.raises(ParentLost):  # slskd gone: readiness problem, the gate waits
        s.run_query("q", lambda: False)


class NeverEnds(Scripted):
    def get_search(self, search_id: str) -> SearchState:
        return replace(super().get_search(search_id), is_complete=False)


def test_stop_gives_up_when_slskd_never_ends_the_search() -> None:
    s, _ = searcher(NeverEnds([5]))
    with pytest.raises(SlskdError, match="didn't end"):
        s.run_query("q", lambda: True)
