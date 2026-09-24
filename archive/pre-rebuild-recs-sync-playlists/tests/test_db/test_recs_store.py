"""
Tests for RecsStore — SQLite persistence for recommendations.
"""

import pytest

from app.db.database import Database
from app.db.recs_store import RecsStore


def _make_config(tmpdir):
    """Build a minimal test config."""
    from pathlib import Path

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmpdir) / "data")

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    return cfg


@pytest.fixture
def store(tmp_path):
    config = _make_config(str(tmp_path))
    db = Database(config)
    db.initialize_schema()
    store = RecsStore(db)
    yield store
    db.close()


class TestInsert:
    def test_insert_rec_returns_id(self, store):
        rec_id = store.insert_rec(
            source="comfort_zone",
            artist="Artist 1",
            track="Track 1",
            mbid="mbid-1",
            status="queued",
        )
        assert isinstance(rec_id, int)
        assert rec_id > 0

    def test_insert_rec_persists(self, store):
        store.insert_rec(
            source="fresh_picks",
            artist="Artist 2",
            track="Track 2",
            mbid=None,
            status="in_library",
            playlist_id="pl-1",
        )
        rec = store.get_rec(1)
        assert rec is not None
        assert rec["source"] == "fresh_picks"
        assert rec["artist"] == "Artist 2"
        assert rec["track"] == "Track 2"
        assert rec["mbid"] is None
        assert rec["status"] == "in_library"
        assert rec["playlist_id"] == "pl-1"


class TestUpdate:
    def test_update_status_basic(self, store):
        rec_id = store.insert_rec(
            source="deep_cuts",
            artist="A",
            track="T",
            mbid="mbid-x",
            status="queued",
            search_id="s-1",
        )
        store.update_status(rec_id, status="downloaded", download_id="dl-1")
        rec = store.get_rec(rec_id)
        assert rec["status"] == "downloaded"
        assert rec["download_id"] == "dl-1"

    def test_update_status_preserves_existing_fields(self, store):
        rec_id = store.insert_rec(
            source="comfort_zone",
            artist="A",
            track="T",
            mbid="mbid-y",
            status="queued",
            search_id="s-2",
        )
        store.update_status(rec_id, status="error")
        rec = store.get_rec(rec_id)
        assert rec["status"] == "error"
        assert rec["search_id"] == "s-2"

    def test_update_status_coalesce(self, store):
        rec_id = store.insert_rec(
            source="comfort_zone",
            artist="A",
            track="T",
            mbid=None,
            status="queued",
            search_id="s-3",
        )
        store.update_status(rec_id, status="downloaded", search_id=None)
        rec = store.get_rec(rec_id)
        assert rec["status"] == "downloaded"
        assert rec["search_id"] == "s-3"


class TestQuery:
    def test_get_recs_by_status(self, store):
        store.insert_rec("comfort_zone", "A1", "T1", None, "queued", search_id="s-a")
        store.insert_rec("fresh_picks", "A2", "T2", None, "queued", search_id="s-b")
        store.insert_rec("deep_cuts", "A3", "T3", None, "in_library")

        queued = store.get_recs_by_status("queued")
        assert len(queued) == 2

        in_lib = store.get_recs_by_status("in_library")
        assert len(in_lib) == 1

    def test_get_rec_none(self, store):
        assert store.get_rec(9999) is None

    def test_count_recs(self, store):
        assert store.count_recs() == 0
        store.insert_rec("comfort_zone", "A", "T", None, "queued")
        assert store.count_recs() == 1
        store.insert_rec("fresh_picks", "B", "U", None, "in_library")
        assert store.count_recs() == 2


class TestCountRecsByStatus:
    def test_empty_table_returns_empty_dict(self, store):
        result = store.count_recs_by_status()
        assert result == {}

    def test_mixed_statuses(self, store):
        store.insert_rec("comfort_zone", "A1", "T1", None, "in_library")
        store.insert_rec("comfort_zone", "A2", "T2", None, "queued")
        store.insert_rec("fresh_picks", "A3", "T3", None, "error")
        store.insert_rec("deep_cuts", "A4", "T4", None, "queued")
        store.insert_rec("deep_cuts", "A5", "T5", None, "queued")

        result = store.count_recs_by_status()
        assert result == {"in_library": 1, "queued": 3, "error": 1}


class TestListRecs:
    def test_empty_table_returns_empty_list(self, store):
        result = store.list_recs()
        assert result == []

    def test_unknown_status_returns_empty(self, store):
        store.insert_rec("comfort_zone", "A", "T", None, "queued")
        result = store.list_recs(status="nonexistent")
        assert result == []

    def test_returns_newest_first(self, store):
        store.insert_rec("comfort_zone", "A1", "T1", None, "queued")
        store.insert_rec("fresh_picks", "A2", "T2", None, "in_library")
        store.insert_rec("deep_cuts", "A3", "T3", None, "queued")

        result = store.list_recs()
        assert len(result) == 3
        assert result[0]["id"] > result[1]["id"] > result[2]["id"]

    def test_status_filter(self, store):
        store.insert_rec("comfort_zone", "A1", "T1", None, "in_library")
        store.insert_rec("fresh_picks", "A2", "T2", None, "queued")
        store.insert_rec("deep_cuts", "A3", "T3", None, "queued")

        queued = store.list_recs(status="queued")
        assert len(queued) == 2
        assert all(r["status"] == "queued" for r in queued)

        in_lib = store.list_recs(status="in_library")
        assert len(in_lib) == 1
        assert in_lib[0]["status"] == "in_library"

    def test_limit_and_offset(self, store):
        for i in range(5):
            store.insert_rec("comfort_zone", f"A{i}", f"T{i}", None, "queued")

        # limit only
        result = store.list_recs(limit=3, offset=0)
        assert len(result) == 3

        # offset
        result_page2 = store.list_recs(limit=3, offset=3)
        assert len(result_page2) == 2

        # no overlap
        ids_page1 = {r["id"] for r in store.list_recs(limit=3, offset=0)}
        ids_page2 = {r["id"] for r in store.list_recs(limit=3, offset=3)}
        assert ids_page1.isdisjoint(ids_page2)

    def test_limit_with_status(self, store):
        for i in range(3):
            store.insert_rec("comfort_zone", f"A{i}", f"T{i}", None, "queued")
        store.insert_rec("fresh_picks", "B", "U", None, "in_library")

        result = store.list_recs(status="queued", limit=2, offset=0)
        assert len(result) == 2
        assert all(r["status"] == "queued" for r in result)

    def test_returns_expected_columns(self, store):
        store.insert_rec(
            "comfort_zone", "Artist", "Track", "mbid-1",
            "queued", search_id="s-1",
        )
        rows = store.list_recs()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] is not None
        assert r["source"] == "comfort_zone"
        assert r["artist"] == "Artist"
        assert r["track"] == "Track"
        assert r["mbid"] == "mbid-1"
        assert r["status"] == "queued"
        assert r["search_id"] == "s-1"
        assert r["download_id"] is None
        assert r["playlist_id"] is None
        assert r["created_at"] is not None
        assert r["processed_at"] is None


class TestGetRecBySearchId:
    """The lookup DownloadMonitor uses to recover rec intent at import
    time — must include `source` (P6.7-0b category routing)."""

    def test_returns_source_with_the_row(self, store):
        store.insert_rec(
            "fresh_picks", "Artist", "Track", None,
            "queued", search_id="s-intent",
        )
        rec = store.get_rec_by_search_id("s-intent")
        assert rec is not None
        assert rec["track"] == "Track"
        assert rec["artist"] == "Artist"
        assert rec["source"] == "fresh_picks"

    def test_no_match_returns_none(self, store):
        assert store.get_rec_by_search_id("s-never-existed") is None

    def test_most_recent_match_wins(self, store):
        store.insert_rec(
            "comfort_zone", "A", "T1", None, "queued", search_id="s-reused"
        )
        store.insert_rec(
            "deep_cuts", "B", "T2", None, "queued", search_id="s-reused"
        )
        rec = store.get_rec_by_search_id("s-reused")
        assert rec["track"] == "T2"
        assert rec["source"] == "deep_cuts"


class TestWorkerState:
    def test_set_and_get(self, store):
        store.set_worker_state("rec_puller.last_run_at", "1723456789.5")
        assert store.get_worker_state("rec_puller.last_run_at") == "1723456789.5"

    def test_get_missing_returns_none(self, store):
        assert store.get_worker_state("nope") is None

    def test_set_overwrites(self, store):
        store.set_worker_state("k", "1")
        store.set_worker_state("k", "2")
        assert store.get_worker_state("k") == "2"
