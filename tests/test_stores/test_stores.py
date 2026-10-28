"""Stores (#86): only the behaviour that pays off; real SQLite file."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdPeer, SlskdStore
from app.stores.songs import IdentityConflict, SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
users, songs, reqs = UsersStore(), SongsStore(), RequestsStore()
downloads, slskd, files = DownloadsStore(), SlskdStore(), FilesStore()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "pv.db")
    database.migrate()
    return database


@pytest.fixture
def conn(db: Database) -> Iterator[sqlite3.Connection]:
    c = db.connect()
    yield c
    c.close()


def user(c: sqlite3.Connection, name: str = "alice") -> int:
    return users.upsert_on_login(c, name, "enc", False, NOW).id


# ---------------------------------------------------------------- users


def test_login_upsert_keeps_id_and_refreshes(conn: sqlite3.Connection) -> None:
    first = users.upsert_on_login(conn, "Alice", "enc1", False, NOW)
    users.set_status(conn, first.id, "needs_reauth")
    again = users.upsert_on_login(conn, "alice", "enc2", True, NOW + 5)
    assert again.id == first.id and again.username == "Alice"
    assert (again.nd_password_enc, again.is_admin, again.status) == (
        "enc2",
        1,
        "active",
    )
    assert again.created_at == NOW


def test_sessions_and_settings(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    s = users.create_session(conn, uid, "h1", NOW, NOW + 10)
    users.create_session(conn, uid, "h2", NOW, NOW + 100)
    users.touch_session(conn, s.id, NOW + 5, NOW + 50)
    assert users.delete_expired_sessions(conn, NOW + 60) == 1
    assert users.get_session(conn, "h1") is None and users.get_session(conn, "h2")
    users.set_setting(conn, uid, "theme", {"dark": True}, NOW)
    users.set_setting(conn, uid, "theme", {"dark": False}, NOW + 1)
    assert users.get_settings(conn, uid) == {"theme": {"dark": False}}
    assert users.get_setting(conn, uid, "missing") is None


# ---------------------------------------------------------------- songs


def test_find_or_create_reuses_song_and_adds_ids(conn: sqlite3.Connection) -> None:
    f = SongFields("Song", "Artist")
    a = songs.find_or_create(conn, f, {"deezer": "1"})
    b = songs.find_or_create(conn, f, {"deezer": "1", "musicbrainz": "mb-1"})
    c = songs.find_or_create(conn, f, {"musicbrainz": "mb-1"})
    assert a.id == b.id == c.id
    assert songs.identities(conn, a.id) == {"deezer": "1", "musicbrainz": "mb-1"}
    other = songs.find_or_create(conn, f, {"deezer": "2"})
    with pytest.raises(IdentityConflict):
        songs.find_or_create(conn, f, {"deezer": "2", "musicbrainz": "mb-1"})
    assert other.id != a.id


# ---------------------------------------------------------------- requests


def test_slskd_query_repeat_bumps_one_row(conn: sqlite3.Connection) -> None:
    uid, bob = user(conn), user(conn, "bob")
    first = reqs.record_slskd_query(conn, uid, "Daft Punk", NOW)
    again = reqs.record_slskd_query(conn, uid, "daft punk", NOW + 9)
    theirs = reqs.record_slskd_query(conn, bob, "daft punk", NOW)
    assert again.id == first.id and again.last_used_at == NOW + 9
    assert again.created_at == NOW and again.query == "Daft Punk"
    assert theirs.id != first.id
    assert first.origin_id is None


def test_slskd_requests_cannot_take_an_origin(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        reqs.create(conn, user(conn), "slskd", NOW, origin_id=1)


def test_catalog_search_repeat_bumps(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    a = reqs.record_catalog_search(conn, uid, "Abba", "artist", NOW)
    b = reqs.record_catalog_search(conn, uid, "ABBA", "artist", NOW + 3)
    c = reqs.record_catalog_search(conn, uid, "abba", "track", NOW + 4)
    assert a.id == b.id != c.id and b.last_used_at == NOW + 3
    assert [s.id for s in reqs.catalog_history(conn, uid, 1, 10)] == [c.id, a.id]


def test_history_pages_newest_first(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    ids = [reqs.create(conn, uid, "catalog", NOW + i).id for i in range(5)]
    bumped = reqs.record_slskd_query(conn, uid, "q", NOW - 1)
    reqs.record_slskd_query(conn, uid, "Q", NOW + 99)  # repeat moves it to the top
    pages = [[r.id for r in reqs.history(conn, uid, p, 2)] for p in (1, 2, 3, 4)]
    assert pages == [[bumped.id, ids[4]], [ids[3], ids[2]], [ids[1], ids[0]], []]
    assert reqs.count(conn, uid) == 6
    assert reqs.history(conn, user(conn, "bob"), 1, 10) == []


# ---------------------------------------------------------------- downloads + slskd


def _slskd_download(c: sqlite3.Connection) -> tuple[int, int]:
    song = songs.find_or_create(c, SongFields("T", "A"), {"deezer": "9"})
    req = reqs.create(c, user(c), "catalog", NOW, song_id=song.id)
    slskd.add_search(c, "s-1", req.id, "A T", "TA", NOW)
    return req.id, downloads.create(c, req.id, "slskd", NOW).id


def test_attempt_bookkeeping(conn: sqlite3.Connection) -> None:
    _, dl = _slskd_download(conn)
    a1 = slskd.add_attempt(conn, dl, "s-1", 0, "peer1", "a.flac", 10, NOW)
    slskd.set_transfer_id(conn, a1.id, "t-1")
    assert (
        slskd.attempt_by_transfer(conn, "t-1")
        == slskd.attempts_for_download(conn, dl)[0]
    )
    slskd.end_attempt(conn, a1.id, "tooSlow", NOW + 60)
    a2 = slskd.add_attempt(conn, dl, "s-1", 1, "peer2", "b.flac", 11, NOW + 61)
    assert slskd.tried_usernames(conn, dl) == {"peer1", "peer2"}
    assert [a.id for a in slskd.running_attempts(conn)] == [a2.id]
    assert len(slskd.attempts_for_download(conn, dl)) - 1 == 1  # retries


def test_peer_bookkeeping(conn: sqlite3.Connection) -> None:
    assert slskd.record_peer_failure(conn, "p") == 1
    assert slskd.record_peer_failure(conn, "p") == 2
    slskd.block_peer(conn, "p", NOW)
    slskd.block_peer(conn, "q", NOW)
    assert slskd.blocked_usernames(conn) == {"p", "q"}
    assert slskd.get_peer(conn, "p") == SlskdPeer("p", 2, NOW)
    slskd.clear_peer(conn, "p")
    assert slskd.get_peer(conn, "p") is None and slskd.blocked_usernames(conn) == {"q"}


def test_complete_hands_file_over(conn: sqlite3.Connection) -> None:
    req, dl = _slskd_download(conn)
    with pytest.raises(ValueError):
        downloads.set_state(conn, dl, "completed")
    downloads.complete(conn, dl, "A/T.flac", NOW + 5)
    song_id = reqs.get(conn, req)
    assert song_id and song_id.song_id
    f = files.for_song(conn, song_id.song_id)
    assert f and (f.file_location, f.source, f.downloaded_at) == (
        "A/T.flac",
        "slskd",
        NOW + 5,
    )
    t = files.add_tagging(conn, f.id, "tagged", NOW, tags_written={"title": "T"})
    assert files.tagging_for_file(conn, f.id) == [t] and t.tags_written == {
        "title": "T"
    }


def test_deleting_request_cascades_through_stores(conn: sqlite3.Connection) -> None:
    req, dl = _slskd_download(conn)
    slskd.add_attempt(conn, dl, "s-1", 0, "peer1", "a.flac", 10, NOW)
    conn.execute("DELETE FROM requests WHERE id = ?", (req,))
    assert downloads.get(conn, dl) is None
    assert slskd.searches_for_request(conn, req) == []
    assert slskd.attempts_for_download(conn, dl) == []


def test_transaction_spans_stores_and_rolls_back(db: Database) -> None:
    with db.transaction() as c:
        uid = user(c)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as c:
        req = reqs.create(c, uid, "catalog", NOW)
        downloads.create(c, req.id, "slskd", NOW)
        slskd.add_search(c, "s-x", 999, "q", "T", NOW)  # no such request
    with db.transaction() as c:
        assert reqs.count(c, uid) == 0
