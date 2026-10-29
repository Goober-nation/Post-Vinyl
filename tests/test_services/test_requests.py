"""Requests service on a real SQLite file (#96)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.states import IllegalTransition
from app.services.requests import (
    MAX_PAGE,
    MAX_PAGE_SIZE,
    RequestNotFound,
    RequestService,
    SongNotFound,
    StateChanged,
)
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> float:
        self.t += 1
        return self.t


Setup = tuple[RequestService, Database, int, int, int]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        alice = UsersStore().upsert_on_login(conn, "alice", "enc", False, NOW).id
        bob = UsersStore().upsert_on_login(conn, "bob", "enc", False, NOW).id
        song = SongsStore().create(conn, SongFields("Feather", "Nujabes")).id
    return RequestService(db, Clock()), db, alice, bob, song


def test_every_create_is_a_new_requested_row(setup: Setup) -> None:
    svc, _, alice, _, song = setup
    a = svc.create(alice, song, "catalog")
    b = svc.create(alice, song, "catalog")
    assert a.id != b.id
    assert (a.track_state, a.song_id, a.source) == ("requested", song, "catalog")


def test_slskd_query_repeat_bumps_one_row(setup: Setup) -> None:
    svc, _, alice, _, _ = setup
    a = svc.record_slskd_query(alice, "nujabes  feather")
    b = svc.record_slskd_query(alice, "Nujabes Feather")
    assert a.id == b.id and b.last_used_at > a.last_used_at
    with pytest.raises(ValueError):
        svc.record_slskd_query(alice, "   ")


def test_drop_and_re_request_follow_the_state_graph(setup: Setup) -> None:
    svc, _, alice, _, song = setup
    req = svc.create(alice, song, "catalog")
    with pytest.raises(IllegalTransition):  # requested hasn't ended
        svc.re_request(alice, req.id)
    assert svc.drop(alice, req.id).track_state == "dropped"
    with pytest.raises(IllegalTransition):
        svc.drop(alice, req.id)
    assert svc.re_request(alice, req.id).track_state == "reRequested"


def test_ended_request_can_be_re_requested(setup: Setup) -> None:
    svc, db, alice, _, song = setup
    req = svc.create(alice, song, "catalog")
    with db.transaction() as conn:  # the download service ended it
        RequestsStore().set_track_state(conn, req.id, "notDownloaded")
    assert svc.re_request(alice, req.id).track_state == "reRequested"


def test_other_users_requests_are_invisible(setup: Setup) -> None:
    svc, _, alice, bob, song = setup
    req = svc.create(alice, song, "catalog")
    for call in (svc.get, svc.drop, svc.re_request):
        with pytest.raises(RequestNotFound):
            call(bob, req.id)
    assert svc.history(bob).total == 0


def test_change_made_meanwhile_is_not_overwritten(setup: Setup) -> None:
    svc, db, alice, _, song = setup
    req = svc.create(alice, song, "catalog")
    store = RequestsStore()
    with db.transaction() as conn:
        assert store.set_track_state(conn, req.id, "searching", expected="requested")
        assert not store.set_track_state(conn, req.id, "dropped", expected="requested")
    assert svc.get(alice, req.id).track_state == "searching"


def test_create_checks_source_song_and_origin_owner(setup: Setup) -> None:
    svc, db, alice, bob, song = setup
    with db.transaction() as conn:
        bobs = RequestsStore().record_catalog_search(conn, bob, "x", "track", NOW).id
        alices = (
            RequestsStore().record_catalog_search(conn, alice, "x", "track", NOW).id
        )
    with pytest.raises(ValueError):
        svc.create(alice, song, "slskd")
    with pytest.raises(ValueError):
        svc.create(alice, song, "anything")
    with pytest.raises(SongNotFound):
        svc.create(alice, 999, "catalog")
    with pytest.raises(RequestNotFound):
        svc.create(alice, song, "catalog", origin_id=bobs)
    with pytest.raises(RequestNotFound):
        svc.create(alice, song, "catalog", origin_id=12345)
    assert svc.create(alice, song, "catalog", origin_id=alices).origin_id == alices


def test_change_committed_meanwhile_waits_instead_of_failing(setup: Setup) -> None:
    """A real race: another service holds the write lock and moves the state."""
    svc, db, alice, _, song = setup
    req = svc.create(alice, song, "catalog")
    result: list[object] = []
    with db.transaction(write=True) as conn:
        RequestsStore().set_track_state(conn, req.id, "searching")
        worker = threading.Thread(
            target=lambda: result.append(_try(lambda: svc.drop(alice, req.id)))
        )
        worker.start()
        time.sleep(0.3)  # the drop is now waiting on the lock
    worker.join(5)
    assert not isinstance(result[0], Exception), result[0]
    assert svc.get(alice, req.id).track_state == "dropped"  # searching → dropped


def _try(call: Callable[[], object]) -> object:
    try:
        return call()
    except sqlite3.Error as e:  # what an unguarded race raises
        return e


def test_state_changed_raised_when_guarded_write_misses(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, alice, _, song = setup
    req = svc.create(alice, song, "catalog")
    monkeypatch.setattr(
        RequestsStore, "set_track_state", lambda *a, **kw: False
    )  # another service won the race
    with pytest.raises(StateChanged):
        svc.drop(alice, req.id)


def test_history_defaults_and_bounds(setup: Setup) -> None:
    svc, _, alice, _, song = setup
    svc.create(alice, song, "catalog")
    assert svc.history(alice).page_size == 20
    huge = svc.history(alice, page=10**30)  # no overflow
    assert huge.items == [] and huge.page == MAX_PAGE


def test_history_pages_newest_first_and_caps_page_size(setup: Setup) -> None:
    svc, _, alice, _, song = setup
    ids = [svc.create(alice, song, "catalog").id for _ in range(5)]
    first = svc.history(alice, page=1, page_size=2)
    assert [r.id for r in first.items] == ids[::-1][:2] and first.total == 5
    assert [r.id for r in svc.history(alice, page=3, page_size=2).items] == [ids[0]]
    assert svc.history(alice, page_size=10_000).page_size == MAX_PAGE_SIZE
