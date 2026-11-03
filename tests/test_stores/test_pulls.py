"""Pull tables: every kind reads the same, cascades, ownership through origins."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.stores.database import Database
from app.stores.pulls import PULL_TABLES, LbTrack, NewItem, Pull, PullsStore
from app.stores.requests import RequestsStore
from app.stores.users import UsersStore

store = PullsStore()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "pv.db")
    database.migrate()
    with database.transaction(write=True) as conn:
        UsersStore().upsert_on_login(conn, "alice", "enc", False, 1)
    return database


def recs(conn: sqlite3.Connection, user_id: int = 1) -> Pull:
    return store.create(conn, "lb_recs", user_id, "r", 1, lb_last_updated=5,
                        first_rank=1, last_rank=2)  # fmt: skip


def radio(conn: sqlite3.Connection) -> Pull:
    return store.create(conn, "lb_radio", 1, "x", 2, artist="A", level="medium", size=1)


def test_every_kind_reads_the_same(db: Database) -> None:
    assert set(PULL_TABLES) == {"lb_recs", "lb_radio"}
    with db.transaction(write=True) as conn:
        a, b = recs(conn), radio(conn)
        assert [p.kind for p in store.for_user(conn, 1)] == ["lb_radio", "lb_recs"]
        store.set_state(conn, b, "cancelled")
        store.set_playlist(conn, a, "pl")
        assert [p.id for p in store.running(conn)] == [a.id]
        got = store.get(conn, a.id)
        assert got is not None and got.navidrome_playlist_id == "pl"


def test_origin_user_knows_pulls(db: Database) -> None:
    with db.transaction(write=True) as conn:
        assert RequestsStore().origin_user(conn, radio(conn).id) == 1


def test_items_stay_in_order_and_settle(db: Database) -> None:
    with db.transaction(write=True) as conn:
        pull = recs(conn)
        store.add_items(conn, pull.id, [NewItem("m1", "a", "b"), NewItem("m2", None, None)])
        first = store.next_queued(conn, pull.id)
        assert first is not None and first.position == 1
        assert not store.is_settled(conn, pull.id)
        store.set_item(conn, first, "failed")
        store.cancel_queued(conn, pull.id)
        assert store.is_settled(conn, pull.id)
        assert store.counts(conn, pull.id).failed == 1


def test_deleting_a_user_removes_lists_and_pulls(db: Database) -> None:
    track = LbTrack(1, "m", 0.5, None, "t", "a", None, None, None, None)
    with db.transaction(write=True) as conn:
        store.add_lb_list(conn, 1, 5, 9, [track])
        assert [x.songs for x in store.lb_lists(conn, 1)] == [1]
        assert store.lb_tracks(conn, 1, 5, 1, 1) == [track]
        recs(conn)
        conn.execute("DELETE FROM users WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM lb_playlists").fetchone()[0] == 0
        assert store.for_user(conn, 1) == []
