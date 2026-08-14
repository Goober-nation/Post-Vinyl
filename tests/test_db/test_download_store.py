"""
Unit tests for DownloadStore.
"""

import tempfile
from unittest.mock import MagicMock

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore


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
    return DownloadStore(db)


def _make_transfer(
    tid, username, filename, size, state, progress=0.0, speed=None, is_rec=False
):
    t = MagicMock()
    t.transfer_id = tid
    t.username = username
    t.filename = filename
    t.size = size
    t.state = state
    t.progress = progress
    t.speed = speed
    t.is_rec_download = is_rec
    return t


class TestInsertPending:
    def test_insert_pending_returns_id(self, store):
        sid = store.insert_pending("search-1", "peer1", "song.mp3", 100, False)
        assert sid.startswith("pending:peer1:song.mp3:")
        row = store.get_transfer(sid)
        assert row is not None
        assert row["state"] == "queued"
        assert row["search_id"] == "search-1"

    def test_get_pending_search_id_returns_correct_value(self, store):
        store.insert_pending("search-X", "userA", "track.flac", 42, False)
        sid = store.get_pending_search_id("userA", "track.flac")
        assert sid == "search-X"

    def test_get_pending_search_id_returns_none_when_unknown(self, store):
        assert store.get_pending_search_id("no", "nope") is None

    def test_insert_pending_writes_library_columns(self, store):
        sid = store.insert_pending(
            "search-1",
            "peer1",
            "song.mp3",
            100,
            False,
            is_library_download=True,
            mb_recording_id="mbid-123",
        )
        row = store.get_transfer(sid)
        assert row["is_library_download"] == 1
        assert row["mb_recording_id"] == "mbid-123"

    def test_insert_pending_defaults_library_columns(self, store):
        sid = store.insert_pending("search-1", "peer1", "song.mp3", 100, False)
        row = store.get_transfer(sid)
        assert row["is_library_download"] == 0
        assert row["mb_recording_id"] is None

    def test_library_download_is_manual_for_the_queue_gate(self, store):
        """A MusicBrainz-initiated download carries is_rec_download = 0, so
        has_active_manual_downloads() already treats it as manual work that
        gates rec queueing — no change to that filter was needed."""
        store.insert_pending(
            "s1", "peer1", "a.mp3", 100, False, is_library_download=True
        )
        assert store.has_active_manual_downloads() is True


class TestUpsertTransfer:
    def test_upsert_new_transfer(self, store):
        t = _make_transfer(
            "uuid-1", "peer1", "song.mp3", 100, "downloading", progress=45.0, speed=1024
        )
        is_new, prev = store.upsert_transfer(t)
        assert is_new is True
        assert prev is None
        row = store.get_transfer("uuid-1")
        assert row["state"] == "downloading"
        assert row["progress"] == 45.0

    def test_upsert_by_slskd_id_updates_existing(self, store):
        t = _make_transfer("uuid-2", "peer2", "track.mp3", 200, "downloading")
        store.upsert_transfer(t)
        t.state = "completed"
        t.progress = 100.0
        is_new, prev = store.upsert_transfer(t)
        assert is_new is False
        assert prev == "downloading"
        row = store.get_transfer("uuid-2")
        assert row["state"] == "completed"

    def test_upsert_adopts_pending_row(self, store):
        store.insert_pending("search-3", "peer3", "adopt.mp3", 300, False)
        t = _make_transfer(
            "uuid-adopt", "peer3", "adopt.mp3", 300, "downloading", progress=50.0
        )
        is_new, prev = store.upsert_transfer(t)
        assert is_new is False
        assert prev == "queued"
        row = store.get_transfer("uuid-adopt")
        assert row["slskd_id"] == "uuid-adopt"
        assert row["search_id"] == "search-3"

    def test_upsert_new_untracked_transfer(self, store):
        t = _make_transfer("uuid-nt", "newguy", "newtrack.mp3", 500, "queued")
        is_new, prev = store.upsert_transfer(t)
        assert is_new is True
        assert prev is None


class TestDeleteTransfers:
    def test_deletes_given_ids(self, store):
        store.upsert_transfer(_make_transfer("uuid-d1", "p1", "a.mp3", 1, "completed"))
        store.upsert_transfer(_make_transfer("uuid-d2", "p2", "b.mp3", 1, "failed"))
        store.upsert_transfer(_make_transfer("uuid-d3", "p3", "c.mp3", 1, "queued"))

        deleted = store.delete_transfers(["uuid-d1", "uuid-d2"])

        assert deleted == 2
        assert store.get_transfer("uuid-d1") is None
        assert store.get_transfer("uuid-d2") is None
        assert store.get_transfer("uuid-d3") is not None

    def test_empty_list_returns_zero(self, store):
        assert store.delete_transfers([]) == 0

    def test_unknown_ids_return_zero(self, store):
        assert store.delete_transfers(["does-not-exist"]) == 0


class TestRetryTracking:
    def test_increment_and_get_retry_count(self, store):
        t = _make_transfer("uuid-r", "pr", "fr.mp3", 1, "failed")
        store.upsert_transfer(t)
        assert store.get_retry_count("uuid-r") == 0
        store.increment_retry_count("uuid-r")
        assert store.get_retry_count("uuid-r") == 1
        store.increment_retry_count("uuid-r")
        assert store.get_retry_count("uuid-r") == 2

    def test_record_retry_attempt(self, store, db):
        t = _make_transfer("uuid-ra", "pr2", "fra.mp3", 1, "failed")
        store.upsert_transfer(t)
        store.record_retry_attempt("uuid-ra", "altpeer", True)
        store.record_retry_attempt("uuid-ra", "altpeer2", False)
        attempts = db.fetch_all(
            "SELECT * FROM download_retry_attempts WHERE download_id = ?",
            ("uuid-ra",),
        )
        assert len(attempts) == 2
        assert attempts[0]["success"] == 1
        assert attempts[1]["success"] == 0


class TestPeerReputation:
    def test_increment_peer_failure_new_peer(self, store):
        count = store.increment_peer_failure("badpeer")
        assert count == 1
        assert store.get_peer_failure_count("badpeer") == 1

    def test_increment_peer_failure_existing_peer(self, store):
        store.increment_peer_failure("badpeer")
        store.increment_peer_failure("badpeer")
        assert store.get_peer_failure_count("badpeer") == 2

    def test_block_and_check_peer(self, store):
        store.increment_peer_failure("blockme")
        assert not store.is_peer_blocked("blockme")
        store.set_peer_blocked("blockme")
        assert store.is_peer_blocked("blockme")

    def test_is_peer_blocked_unknown_returns_false(self, store):
        assert not store.is_peer_blocked("nobody")

    def test_get_peer_failure_count_unknown_returns_zero(self, store):
        assert store.get_peer_failure_count("nobody") == 0

    def test_ban_duration_none_is_permanent(self, store):
        """Omitting ban_duration_seconds preserves the pre-expiry behavior:
        a block never lifts on its own, however old blocked_at gets."""
        store.increment_peer_failure("blockme")
        store.set_peer_blocked("blockme")
        store._db.execute(
            "UPDATE peers SET blocked_at = ? WHERE username = ?",
            (0, "blockme"),
        )
        assert store.is_peer_blocked("blockme")

    def test_ban_expires_and_resets_failure_count(self, store):
        import time

        store.increment_peer_failure("stale")
        store.increment_peer_failure("stale")
        store.set_peer_blocked("stale")
        # Backdate blocked_at well past a 1-second ban duration.
        store._db.execute(
            "UPDATE peers SET blocked_at = ? WHERE username = ?",
            (int(time.time()) - 10, "stale"),
        )
        assert not store.is_peer_blocked("stale", ban_duration_seconds=1)
        assert store.get_peer_failure_count("stale") == 0
        # Lifted for good, not just for this one check.
        assert not store.is_peer_blocked("stale", ban_duration_seconds=1)

    def test_ban_still_active_within_window(self, store):
        store.increment_peer_failure("fresh")
        store.set_peer_blocked("fresh")
        assert store.is_peer_blocked("fresh", ban_duration_seconds=86400)


class TestFileMoved:
    def test_mark_and_check_file_moved(self, store):
        t = _make_transfer("uuid-fm", "p", "f.mp3", 1, "completed")
        store.upsert_transfer(t)
        assert not store.file_moved("uuid-fm")
        store.mark_file_moved("uuid-fm", "/target/dir")
        assert store.file_moved("uuid-fm")
        row = store.get_transfer("uuid-fm")
        assert row["target_dir"] == "/target/dir"


class TestHasActiveManualDownloads:
    def test_false_when_empty(self, store):
        assert store.has_active_manual_downloads() is False

    def test_true_when_manual_pending_row(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        assert store.has_active_manual_downloads() is True

    def test_false_when_only_rec_rows(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, True)
        assert store.has_active_manual_downloads() is False

    def test_false_when_manual_finished(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        db = store._db
        db.execute("UPDATE downloads SET state = 'completed' WHERE state = 'queued'")
        assert store.has_active_manual_downloads() is False

    def test_false_when_mixed_rec_active(self, store):
        """A completed manual row plus an active rec row is not 'manual active'."""
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        store.insert_pending("s2", "peer2", "b.mp3", 100, True)
        db = store._db
        db.execute("UPDATE downloads SET state = 'completed' WHERE is_rec_download = 0")
        assert store.has_active_manual_downloads() is False


class TestFailStalePending:
    """P6.5 review fix (2026-08-11): a queue-time pending row slskd never
    adopts used to hold has_active_manual_downloads() True forever, gating
    all rec queueing with no way to clear it."""

    def _age_rows(self, store, seconds):
        store._db.execute(
            "UPDATE downloads SET created_at = created_at - ?", (seconds,)
        )

    def test_no_op_when_nothing_stale(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        assert store.fail_stale_pending(300) == []
        assert store.has_active_manual_downloads() is True

    def test_marks_old_pending_row_failed(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        self._age_rows(store, 600)

        failed = store.fail_stale_pending(300)
        assert len(failed) == 1
        assert failed[0]["username"] == "peer1"
        assert failed[0]["filename"] == "a.mp3"

    def test_failing_the_row_releases_the_rec_queue_gate(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        self._age_rows(store, 600)
        assert store.has_active_manual_downloads() is True

        store.fail_stale_pending(300)
        assert store.has_active_manual_downloads() is False

    def test_failed_row_is_deletable_via_finished_states(self, store):
        """'failed' is in the routes' FINISHED_STATES, so the existing
        delete-finished endpoint can clear the row — no new route needed."""
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        self._age_rows(store, 600)
        store.fail_stale_pending(300)

        stale = store.get_transfers_by_state("failed")
        assert len(stale) == 1
        assert store.delete_transfers([stale[0]["id"]]) == 1

    def test_leaves_adopted_rows_alone(self, store):
        """Only unadopted 'pending:' rows are reaped — a row with a real
        slskd id is a live transfer no matter how old."""
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        store._db.execute(
            "UPDATE downloads SET id = 'uuid-1', slskd_id = 'uuid-1', "
            "state = 'downloading'"
        )
        self._age_rows(store, 600)
        assert store.fail_stale_pending(300) == []
        assert store.has_active_manual_downloads() is True

    def test_reaps_rec_pending_rows_too(self, store):
        """The gate only counts manual rows, but a rec row slskd dropped is
        just as dead — leaving it 'queued' would misreport active work."""
        store.insert_pending("s1", "peer1", "a.mp3", 100, True)
        self._age_rows(store, 600)
        assert len(store.fail_stale_pending(300)) == 1

    def test_late_adoption_still_matches_the_failed_row(self, store):
        """The row keeps its 'pending:' id and NULL slskd_id, so if slskd
        does eventually report the transfer, upsert_transfer() adopts it
        and overwrites the state instead of creating a duplicate."""
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        self._age_rows(store, 600)
        store.fail_stale_pending(300)

        transfer = _make_transfer("uuid-late", "peer1", "a.mp3", 100, "downloading")
        is_new, prev_state = store.upsert_transfer(transfer)

        assert is_new is False
        assert prev_state == "failed"
        assert store.get_transfer("uuid-late")["state"] == "downloading"
        # Adopted in place, not duplicated: the old pending id is gone.
        assert store.get_transfers_by_state("failed") == []


class TestManualGateTimeLimit:
    """Found live 2026-08-11: a manual download adopted by slskd and parked
    in "Queued, Remotely" — the peer has it in *their* upload queue — held
    the rec-queue gate for 11+ minutes and would have held it for hours.
    Nothing else clears that state: it isn't an orphan (slskd keeps
    reporting it) and isn't a stale pending row (it has a real slskd id).
    """

    def _age(self, store, seconds):
        store._db.execute(
            "UPDATE downloads SET created_at = created_at - ?", (seconds,)
        )

    def _adopt(self, store, state="queued"):
        store.insert_pending("s1", "peer1", "a.mp3", 100, False)
        store._db.execute(
            "UPDATE downloads SET id = 'uuid-1', slskd_id = 'uuid-1', state = ?",
            (state,),
        )

    def test_no_grace_argument_keeps_the_old_behavior(self, store):
        self._adopt(store)
        self._age(store, 86400)
        assert store.has_active_manual_downloads() is True

    def test_fresh_queued_row_still_holds_the_gate(self, store):
        self._adopt(store)
        assert store.has_active_manual_downloads(600) is True

    def test_long_queued_row_stops_holding_the_gate(self, store):
        self._adopt(store)
        self._age(store, 700)
        assert store.has_active_manual_downloads(600) is False

    def test_downloading_row_is_never_aged_out(self, store):
        """Bytes are moving — a large file legitimately takes a long time."""
        self._adopt(store, state="downloading")
        self._age(store, 86400)
        assert store.has_active_manual_downloads(600) is True

    def test_rec_rows_never_hold_the_gate_either_way(self, store):
        store.insert_pending("s1", "peer1", "a.mp3", 100, True)
        assert store.has_active_manual_downloads(600) is False

    def test_the_transfer_row_itself_is_untouched(self, store):
        """Only the gate claim expires. The download keeps going and still
        completes if the peer eventually comes through."""
        self._adopt(store)
        self._age(store, 700)
        store.has_active_manual_downloads(600)
        assert store.get_transfer("uuid-1")["state"] == "queued"

    def test_a_fresh_row_still_gates_when_an_old_one_has_expired(self, store):
        self._adopt(store)
        self._age(store, 700)
        store.insert_pending("s2", "peer2", "b.mp3", 100, False)
        assert store.has_active_manual_downloads(600) is True
