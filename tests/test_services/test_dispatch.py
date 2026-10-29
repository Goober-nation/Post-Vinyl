"""Download dispatch and the DownloadSource contract (#99).

The behaviour tests run on the real dispatch (real SQLite, fake sources) and
on FakeDownloadDispatch, so the fake can't drift.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.dispatch import (
    DownloadDispatch,
    DownloadDispatchBase,
    DownloadStatus,
    NotStartable,
)
from app.services.dispatch_fake import FakeDownloadDispatch, FakeRequest
from app.services.download_source_fake import FakeDownloadSource
from app.services.requests import RequestNotFound
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
CONFIGURED = {"download": ("slskd", "other")}


@dataclass
class Case:
    svc: DownloadDispatchBase
    alice: int
    bob: int
    req: int  # alice's request with a song, state requested
    no_song: int  # alice's request without a song
    set_enabled: Callable[[list[str]], None]
    set_state: Callable[[int, str], None]
    state: Callable[[int], str]
    started: Callable[[], list[tuple[str, int]]]
    cancelled: Callable[[], list[int]]


@pytest.fixture(params=["real", "fake"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Case:
    if request.param == "fake":
        fake = FakeDownloadDispatch(
            requests={1: FakeRequest(1), 2: FakeRequest(1, has_song=False)},
            enabled=list(CONFIGURED["download"]),
        )

        def set_fake(on: list[str]) -> None:
            fake.enabled = on

        def set_state_fake(rid: int, state: str) -> None:
            fake.requests[rid].track_state = state

        return Case(
            fake, 1, 2, 1, 2, set_fake, set_state_fake,
            lambda rid: fake.requests[rid].track_state,
            lambda: fake.started, lambda: fake.cancelled,
        )  # fmt: skip
    db = Database(tmp_path / "pv.db")
    db.migrate()
    store = RequestsStore()
    with db.transaction() as conn:
        alice = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        bob = UsersStore().upsert_on_login(conn, "bob", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields("Feather", "Nujabes")).id
        req = store.create(conn, alice, "catalog", NOW, song_id=song).id
        no_song = store.record_slskd_query(conn, alice, "feather", NOW).id
    prefs = SourcePreferences(db, CONFIGURED)
    slskd, other = FakeDownloadSource(db, "slskd"), FakeDownloadSource(db, "other")
    real = DownloadDispatch(db, prefs, [slskd, other])

    def set_real(on: list[str]) -> None:
        prefs.set_for_user(
            alice, "download", on, [n for n in CONFIGURED["download"] if n not in on]
        )

    def set_state_real(rid: int, state: str) -> None:
        with db.transaction() as conn:
            store.set_track_state(conn, rid, state)

    def state_real(rid: int) -> str:
        with db.transaction() as conn:
            r = store.get(conn, rid)
        assert r is not None
        return r.track_state

    return Case(
        real, alice, bob, req, no_song, set_real, set_state_real, state_real,
        lambda: [(s.source, r) for s in (slskd, other) for r in s.started],
        lambda: slskd.cancelled + other.cancelled,
    )  # fmt: skip


def test_first_enabled_source_takes_the_request(case: Case) -> None:
    assert case.svc.dispatch(case.alice, case.req) == "slskd"
    assert case.started() == [("slskd", case.req)]
    assert case.state(case.req) == "searching"


def test_user_order_picks_the_source(case: Case) -> None:
    case.set_enabled(["other"])
    assert case.svc.dispatch(case.alice, case.req) == "other"


def test_no_enabled_source_waits(case: Case) -> None:
    case.set_enabled([])
    assert case.svc.dispatch(case.alice, case.req) is None
    assert case.state(case.req) == "requested" and case.started() == []


def test_only_waiting_requests_with_a_song_start(case: Case) -> None:
    with pytest.raises(NotStartable):
        case.svc.dispatch(case.alice, case.no_song)
    case.set_state(case.req, "found")
    with pytest.raises(NotStartable):
        case.svc.dispatch(case.alice, case.req)
    case.set_state(case.req, "reRequested")
    assert case.svc.dispatch(case.alice, case.req) == "slskd"


def test_other_users_requests_are_invisible(case: Case) -> None:
    for call in (case.svc.dispatch, case.svc.cancel, case.svc.status):
        with pytest.raises(RequestNotFound):
            call(case.bob, case.req)
    with pytest.raises(RequestNotFound):
        case.svc.dispatch(case.alice, 999)


def test_cancel_reaches_the_source(case: Case) -> None:
    case.svc.dispatch(case.alice, case.req)
    case.svc.cancel(case.alice, case.req)
    assert case.cancelled() == [case.req]


# ------------------------------------------------------------ real only


Real = tuple[DownloadDispatch, Database, FakeDownloadSource, int, int]


@pytest.fixture
def real(tmp_path: Path) -> Real:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        alice = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields("Feather", "Nujabes")).id
        req = RequestsStore().create(conn, alice, "catalog", NOW, song_id=song).id
    src = FakeDownloadSource(db, "slskd")
    return (
        DownloadDispatch(db, SourcePreferences(db, CONFIGURED), [src]),
        db,
        src,
        alice,
        req,
    )


def test_second_dispatch_does_not_start_twice(real: Real) -> None:
    """Two dispatches read 'requested' before either claims: only one wins."""
    svc, db, src, alice, req = real
    with db.transaction() as conn:
        stale = RequestsStore().get(conn, req)
    svc.dispatch(alice, req)
    assert stale is not None
    with db.transaction() as conn:
        song = SongsStore().get(conn, stale.song_id or 0)
    assert song is not None
    src.start(stale, song)  # the loser of the race, holding the old state
    assert src.started == [req]


def test_status_reads_what_the_source_wrote(real: Real) -> None:
    svc, _, src, alice, req = real
    assert svc.status(alice, req) == DownloadStatus("requested", None, None, None)
    svc.dispatch(alice, req)
    src.finish(req, "Nujabes/Feather.flac")
    assert svc.status(alice, req) == DownloadStatus(
        "downloaded", "slskd", "completed", "Nujabes/Feather.flac"
    )


def test_configured_source_without_a_backend_is_skipped(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        alice = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields("Feather", "Nujabes")).id
        req = RequestsStore().create(conn, alice, "catalog", NOW, song_id=song).id
    svc = DownloadDispatch(
        db, SourcePreferences(db, CONFIGURED), [FakeDownloadSource(db, "other")]
    )
    assert svc.dispatch(alice, req) == "other"  # slskd listed first but not built
