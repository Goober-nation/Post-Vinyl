"""Initial schema and migration runner (#84): constraints, cascades, hand-off.

Runs against a real SQLite file; meant for the throwaway python:3.12-slim
container (AGENTS.md), but needs nothing beyond the standard library.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.stores.database import MIGRATIONS_DIR, Database

NOW = 1_800_000_000
LATEST = len(list(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "pv.db")
    assert database.migrate() == LATEST
    return database


@pytest.fixture
def conn(db: Database) -> Iterator[sqlite3.Connection]:
    c = db.connect()
    yield c
    c.close()


def add_user(c: sqlite3.Connection, name: str = "alice") -> int:
    cur = c.execute(
        "INSERT INTO users (username, nd_password_enc, created_at) VALUES (?, 'enc', ?)",
        (name, NOW),
    )
    return int(cur.lastrowid or 0)


def add_song(c: sqlite3.Connection, title: str = "Song") -> int:
    cur = c.execute(
        "INSERT INTO songs (title, artist_name) VALUES (?, 'Artist')", (title,)
    )
    return int(cur.lastrowid or 0)


def add_request(
    c: sqlite3.Connection, user: int, song: int | None, source: str = "catalog"
) -> int:
    cur = c.execute(
        "INSERT INTO requests (user_id, song_id, source, query, created_at,"
        " last_used_at) VALUES (?, ?, ?, 'q', ?, ?)",
        (user, song, source, NOW, NOW),
    )
    return int(cur.lastrowid or 0)


def add_download(c: sqlite3.Connection, request: int) -> int:
    cur = c.execute(
        "INSERT INTO downloads (request_id, source, created_at) VALUES (?, 'slskd', ?)",
        (request, NOW),
    )
    return int(cur.lastrowid or 0)


def complete(c: sqlite3.Connection, download: int, location: str) -> None:
    c.execute(
        "UPDATE downloads SET state = 'completed', file_location = ?, completed_at = ?"
        " WHERE id = ?",
        (location, NOW, download),
    )


# ---------------------------------------------------------------- runner


def test_migrate_is_idempotent(db: Database) -> None:
    assert db.migrate() == LATEST
    with db.transaction() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == LATEST


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_request(conn, user=999, song=None)


def test_failing_migration_leaves_db_unchanged(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migrations / "002_bad.sql").write_text("CREATE TABLE b (x INTEGER); NOT SQL;")
    db = Database(tmp_path / "t.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(migrations)
    c = db.connect()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "a" in tables and "b" not in tables
    c.close()


def test_gap_in_migration_numbers_refused(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_a.sql").write_text("SELECT 1;")
    (migrations / "003_c.sql").write_text("SELECT 1;")
    with pytest.raises(RuntimeError, match="without gaps"):
        Database(tmp_path / "t.db").migrate(migrations)


def test_newer_database_refused(db: Database) -> None:
    with db.transaction() as c:
        c.execute("PRAGMA user_version = 5")
    with pytest.raises(RuntimeError, match="newer than this app"):
        db.migrate()


# ---------------------------------------------------------------- constraints


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("UPDATE requests SET track_state = 'indexed'", ()),
        ("UPDATE downloads SET state = 'moved'", ()),
        ("UPDATE downloads SET state = 'completed'", ()),  # no file_location
        ("UPDATE users SET status = 'banned'", ()),
        ("UPDATE users SET is_admin = 2", ()),
    ],
)
def test_check_constraints(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> None:
    add_download(conn, add_request(conn, add_user(conn), add_song(conn)))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_usernames_unique_ignoring_case(conn: sqlite3.Connection) -> None:
    add_user(conn, "Alice")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "alice")


def test_typed_soulseek_query_unique_per_user(conn: sqlite3.Connection) -> None:
    alice, bob = add_user(conn, "alice"), add_user(conn, "bob")
    add_request(conn, alice, None, "slskd")
    add_request(conn, bob, None, "slskd")  # another user may repeat it
    add_request(conn, alice, None, "catalog")  # other sources aren't deduplicated
    with pytest.raises(sqlite3.IntegrityError):
        add_request(conn, alice, None, "slskd")


def test_attempt_outcome_and_end_go_together(conn: sqlite3.Connection) -> None:
    request = add_request(conn, add_user(conn), add_song(conn))
    download = add_download(conn, request)
    conn.execute(
        "INSERT INTO slskd_searches (id, request_id, query, strategy, created_at)"
        " VALUES ('s1', ?, 'q', 'L0', ?)",
        (request, NOW),
    )
    insert = (
        "INSERT INTO slskd_attempts (download_id, search_id, rank, username, filename,"
        " size, outcome, started_at, ended_at) VALUES (?, 's1', 1, 'peer', 'f', 1, ?, ?, ?)"
    )
    conn.execute(insert, (download, None, NOW, None))  # running
    conn.execute(insert, (download, "tooSlow", NOW, NOW + 20))  # finished
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (download, "noStart", NOW, None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (download, "slow", NOW, NOW))


def test_song_identity_unique_per_source_id(conn: sqlite3.Connection) -> None:
    a, b = add_song(conn, "A"), add_song(conn, "B")
    conn.execute("INSERT INTO song_identity VALUES (?, 'musicbrainz', 'mbid-1')", (a,))
    conn.execute("INSERT INTO song_identity VALUES (?, 'isrc', 'X1')", (a,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO song_identity VALUES (?, 'musicbrainz', 'mbid-1')", (b,)
        )


# ---------------------------------------------------------------- hand-off


def test_completed_download_hands_off_once(conn: sqlite3.Connection) -> None:
    song = add_song(conn)
    download = add_download(conn, add_request(conn, add_user(conn), song))
    conn.execute("UPDATE downloads SET state = 'downloading' WHERE id = ?", (download,))
    assert conn.execute("SELECT COUNT(*) FROM file_management").fetchone()[0] == 0
    complete(conn, download, "peer/Album/01.flac")
    complete(conn, download, "peer/Album/01.flac")  # already completed: no re-fire
    rows = conn.execute("SELECT * FROM file_management").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["song_id"], row["source"], row["file_location"], row["file_state"]) == (
        song,
        "slskd",
        "peer/Album/01.flac",
        "downloaded",
    )


def test_redownload_replaces_the_songs_file(conn: sqlite3.Connection) -> None:
    song = add_song(conn)
    user = add_user(conn)
    first = add_download(conn, add_request(conn, user, song))
    complete(conn, first, "old.mp3")
    conn.execute(
        "UPDATE file_management SET file_state = 'indexed', navidrome_id = 'nd1',"
        " verification = 'success'"
    )
    second = add_download(conn, add_request(conn, user, song))
    complete(conn, second, "new.flac")
    [row] = conn.execute("SELECT * FROM file_management").fetchall()
    assert row["file_location"] == "new.flac" and row["file_state"] == "downloaded"
    # navidrome_id stays: the files service finds and deletes the old copy by it (#103)
    assert row["navidrome_id"] == "nd1" and row["verification"] is None


def test_request_without_song_does_not_hand_off(conn: sqlite3.Connection) -> None:
    download = add_download(conn, add_request(conn, add_user(conn), None, "slskd"))
    complete(conn, download, "f.mp3")
    assert conn.execute("SELECT COUNT(*) FROM file_management").fetchone()[0] == 0


# ---------------------------------------------------------------- cascades


def test_deleting_a_request_cascades_but_files_stay(conn: sqlite3.Connection) -> None:
    song = add_song(conn)
    request = add_request(conn, add_user(conn), song)
    download = add_download(conn, request)
    conn.execute(
        "INSERT INTO slskd_searches (id, request_id, query, strategy, created_at)"
        " VALUES ('s1', ?, 'q', 'L0', ?)",
        (request, NOW),
    )
    conn.execute(
        "INSERT INTO slskd_attempts (download_id, search_id, rank, username, filename,"
        " size, started_at) VALUES (?, 's1', 1, 'peer', 'f', 1, ?)",
        (download, NOW),
    )
    complete(conn, download, "f.flac")
    conn.execute("DELETE FROM requests WHERE id = ?", (request,))
    for table in ("downloads", "slskd_searches", "slskd_attempts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM file_management").fetchone()[0] == 1


def test_song_with_a_file_cannot_be_deleted(conn: sqlite3.Connection) -> None:
    song = add_song(conn)
    complete(conn, add_download(conn, add_request(conn, add_user(conn), song)), "f")
    conn.execute("DELETE FROM requests")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM songs WHERE id = ?", (song,))


def test_deleting_an_origin_unlinks_its_requests(conn: sqlite3.Connection) -> None:
    user = add_user(conn)
    conn.execute("INSERT INTO origins (id, kind) VALUES (7, 'catalog_search')")
    conn.execute(
        "INSERT INTO catalog_searches (id, user_id, query, kind, created_at,"
        " last_used_at) VALUES (7, ?, 'q', 'track', ?, ?)",
        (user, NOW, NOW),
    )
    request = add_request(conn, user, add_song(conn))
    conn.execute("UPDATE requests SET origin_id = 7 WHERE id = ?", (request,))
    conn.execute("DELETE FROM origins WHERE id = 7")
    assert conn.execute("SELECT COUNT(*) FROM catalog_searches").fetchone()[0] == 0
    origin = conn.execute(
        "SELECT origin_id FROM requests WHERE id = ?", (request,)
    ).fetchone()[0]
    assert origin is None


def test_deleting_a_user_ends_sessions_but_requests_block(
    conn: sqlite3.Connection,
) -> None:
    user = add_user(conn)
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at,"
        " expires_at) VALUES (?, 'h', ?, ?, ?)",
        (user, NOW, NOW, NOW + 1),
    )
    add_request(conn, user, None, "slskd")
    with pytest.raises(sqlite3.IntegrityError):  # requests are never pruned
        conn.execute("DELETE FROM users WHERE id = ?", (user,))
    conn.execute("DELETE FROM requests")
    conn.execute("DELETE FROM users WHERE id = ?", (user,))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_user_settings_one_value_per_key_and_go_with_the_user(
    conn: sqlite3.Connection,
) -> None:
    user = add_user(conn)
    insert = "INSERT INTO user_settings VALUES (?, 'theme', '\"dark\"', ?)"
    conn.execute(insert, (user, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (user, NOW))
    conn.execute("DELETE FROM users WHERE id = ?", (user,))
    assert conn.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0] == 0


def test_every_foreign_key_column_is_indexed(conn: sqlite3.Connection) -> None:
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    missing = []
    for table in tables:
        indexed_first = {
            conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchone()[2]
            for idx in conn.execute(f"PRAGMA index_list('{table}')")
        }
        pk = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')") if r[5] == 1}
        for fk in conn.execute(f"PRAGMA foreign_key_list('{table}')"):
            if fk[3] not in indexed_first | pk:
                missing.append(f"{table}.{fk[3]}")
    assert missing == []


def test_attempt_outcomes(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    c = db.connect()
    request = add_request(c, add_user(c), add_song(c))
    download = add_download(c, request)
    c.execute("INSERT INTO slskd_searches (id, request_id, query, strategy, created_at)"
              " VALUES ('s1', ?, 'q', 'L0', ?)", (request, NOW))  # fmt: skip
    insert = (
        "INSERT INTO slskd_attempts (download_id, search_id, rank, username, filename,"
        " size, outcome, started_at, ended_at) VALUES (?, 's1', 0, 'peer', 'f', 1, ?, ?, ?)"
    )
    for outcome in (
        "completed",
        "noStart",
        "tooSlow",
        "failed",
        "skipped",
        "interrupted",
    ):
        c.execute(insert, (download, outcome, NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(insert, (download, "bogus", NOW, NOW))
    c.close()
