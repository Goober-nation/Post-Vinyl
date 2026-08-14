"""
Unit tests for SearchStore — user-facing search headers only.

The job/response persistence these replaced (P6.5-4) was dropped in
migration 005: slskd is the system of record for search results and retains
them durably, so musica's copy was duplication that also made startup
proportional to all history. What's left is the header data musica genuinely
owns.
"""

import tempfile

import pytest

from app.db.database import Database
from app.db.search_store import SearchStore


class _MockConfig:
    """Minimal config for Database in tests."""

    class PathsConfig:
        data_dir: str

    def __init__(self, data_dir: str) -> None:
        self.paths = self.PathsConfig()
        self.paths.data_dir = data_dir


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = _MockConfig(tmpdir)
        database = Database(cfg)
        database.initialize_schema()
        yield database
        database.close()


@pytest.fixture
def store(db):
    return SearchStore(db)


class TestSchemaAfterMigration005:
    def test_duplicate_tables_are_gone(self, db):
        """search_jobs/search_responses must not come back — they duplicated
        slskd, and search_responses grew ~113 rows per search."""
        tables = {
            r["name"]
            for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "search_jobs" not in tables
        assert "search_responses" not in tables

    def test_searches_and_worker_state_survive(self, db):
        """`searches` is the header data musica owns; `worker_state` is
        RecPuller's own state, not a copy of anything slskd has."""
        tables = {
            r["name"]
            for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "searches" in tables
        assert "worker_state" in tables

    def test_searches_holds_exactly_the_header_fields(self, db):
        cols = {
            r["name"] for r in db.fetch_all("PRAGMA table_info(searches)")
        }
        assert cols == {
            "id",
            "query",
            "artist",
            "created_at",
            "status",
            "response_count",
            "file_count",
        }


class TestHeaders:
    def test_insert_and_get(self, store):
        store.insert_search("s1", "bohemian rhapsody", "Queen", "searching")
        row = store.get_search("s1")
        assert row["query"] == "bohemian rhapsody"
        assert row["artist"] == "Queen"
        assert row["status"] == "searching"
        assert row["created_at"] > 0

    def test_get_missing_returns_none(self, store):
        assert store.get_search("nope") is None

    def test_update_status_and_counts(self, store):
        store.insert_search("s1", "q", None, "searching")
        store.update_status("s1", "completed", response_count=12, file_count=34)
        row = store.get_search("s1")
        assert row["status"] == "completed"
        assert row["response_count"] == 12
        assert row["file_count"] == 34

    def test_update_status_preserves_counts_when_omitted(self, store):
        store.insert_search("s1", "q", None, "searching")
        store.update_status("s1", "completed", response_count=5, file_count=7)
        store.update_status("s1", "cancelled")
        row = store.get_search("s1")
        assert row["status"] == "cancelled"
        assert (row["response_count"], row["file_count"]) == (5, 7)

    def test_update_status_on_an_unknown_search_is_a_no_op(self, store):
        """RecPuller's background searches never get a header row, and
        SlskdSearch calls update_status on every status change regardless of
        origin — so this has to be harmless, not an error."""
        store.update_status("never-inserted", "completed")
        assert store.get_search("never-inserted") is None

    def test_list_recent_is_newest_first_and_capped(self, store):
        for i in range(5):
            store.insert_search(f"s{i}", f"query {i}", None, "completed")
            store._db.execute(
                "UPDATE searches SET created_at = ? WHERE id = ?", (1000 + i, f"s{i}")
            )
        rows = store.list_recent(limit=3)
        assert [r["id"] for r in rows] == ["s4", "s3", "s2"]

    def test_all_searches_for_hydration(self, store):
        store.insert_search("s1", "a", None, "completed")
        store.insert_search("s2", "b", "Artist", "searching")
        assert {r["id"] for r in store.all_searches()} == {"s1", "s2"}

    def test_headers_survive_store_recreation(self, db, store):
        """The point of persisting headers at all: a saved search still
        resolves after a restart, so its results can be re-fetched."""
        store.insert_search("s1", "persisted", "Artist", "completed")
        assert SearchStore(db).get_search("s1")["query"] == "persisted"
