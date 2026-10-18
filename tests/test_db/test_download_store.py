"""DownloadStore: one row per requested file, attempts, adoption, handoff (#67)."""

import time

import pytest


@pytest.fixture
def request_and_search(requests_):
    request_id = requests_.create_request("catalog", "MF DOOM – Accordion", "mbid-1")
    requests_.add_slskd_search("s-1", request_id, "mf doom accordion")
    return request_id, "s-1"


@pytest.fixture
def download_id(downloads, request_and_search):
    request_id, search_id = request_and_search
    return downloads.insert_queued(
        request_id, search_id, "peer-a", "a/Accordion.flac", 100
    )


class TestQueueing:
    def test_insert_queued_records_row_and_first_attempt(
        self, downloads, download_id, db
    ):
        row = downloads.get(download_id)

        assert row["state"] == "queued"
        assert row["slskd_id"] is None
        assert downloads.tried_peers(download_id) == {"peer-a"}
        assert downloads.retry_count(download_id) == 0
        assert db.fetch_one("SELECT success FROM download_attempts") == {"success": 0}

    def test_mark_attempt_accepted_only_touches_latest_attempt(
        self, downloads, download_id, db
    ):
        downloads.move_to_peer(download_id, "peer-b", "b/Accordion.flac")
        downloads.mark_attempt_accepted(download_id)

        rows = db.fetch_all(
            "SELECT username, success FROM download_attempts ORDER BY id"
        )
        assert rows == [
            {"username": "peer-a", "success": 0},
            {"username": "peer-b", "success": 1},
        ]

    def test_unknown_search_id_is_rejected(self, downloads, request_and_search):
        import sqlite3

        request_id, _ = request_and_search
        with pytest.raises(sqlite3.IntegrityError):
            downloads.insert_queued(request_id, "cache-abc", "peer", "f.flac")


class TestRetry:
    def test_retry_keeps_one_row_and_remembers_every_peer(self, downloads, download_id):
        downloads.adopt("peer-a", "a/Accordion.flac", "slskd-1")
        downloads.set_state(download_id, "failed")

        downloads.move_to_peer(download_id, "peer-b", "b/Accordion.flac", 200)
        downloads.move_to_peer(download_id, "peer-c", "c/Accordion.flac", 300)

        row = downloads.get(download_id)
        assert (
            row["username"],
            row["state"],
            row["slskd_id"],
            row["completed_at"],
        ) == (
            "peer-c",
            "queued",
            None,
            None,
        )
        assert downloads.tried_peers(download_id) == {"peer-a", "peer-b", "peer-c"}
        assert downloads.retry_count(download_id) == 2

    def test_created_at_marks_the_start_of_the_chain(self, downloads, download_id, db):
        db.execute("UPDATE downloads SET created_at = 1 WHERE id = ?", (download_id,))

        downloads.move_to_peer(download_id, "peer-b", "b.flac")

        assert downloads.get(download_id)["created_at"] == 1


class TestAdoption:
    def test_adopt_links_slskd_id_to_the_queued_row(self, downloads, download_id):
        adopted = downloads.adopt("peer-a", "a/Accordion.flac", "slskd-1")

        assert adopted["id"] == download_id
        assert downloads.get_by_slskd_id("slskd-1")["id"] == download_id

    def test_transfer_the_app_did_not_start_is_not_adopted(
        self, downloads, download_id
    ):
        assert downloads.adopt("stranger", "x.flac", "slskd-9") is None
        assert downloads.get_by_slskd_id("slskd-9") is None

    def test_already_adopted_row_is_not_matched_again(self, downloads, download_id):
        downloads.adopt("peer-a", "a/Accordion.flac", "slskd-1")

        assert downloads.adopt("peer-a", "a/Accordion.flac", "slskd-2") is None

    def test_adopted_active_lists_only_reported_in_flight_rows(
        self, downloads, download_id, request_and_search
    ):
        request_id, search_id = request_and_search
        downloads.insert_queued(request_id, search_id, "peer-x", "x.flac")
        downloads.adopt("peer-a", "a/Accordion.flac", "slskd-1")

        assert [r["id"] for r in downloads.adopted_active()] == [download_id]

    def test_fail_stale_unadopted_uses_latest_attempt_time(
        self, downloads, download_id, db
    ):
        db.execute("UPDATE download_attempts SET attempted_at = 1")
        downloads.move_to_peer(download_id, "peer-b", "b.flac")  # fresh attempt now

        assert downloads.fail_stale_unadopted(60) == []

        db.execute("UPDATE download_attempts SET attempted_at = 1")
        assert [r["id"] for r in downloads.fail_stale_unadopted(60)] == [download_id]
        assert downloads.get(download_id)["state"] == "failed"


class TestStateAndHandoff:
    def test_set_state_returns_previous_and_stamps_terminal(
        self, downloads, download_id
    ):
        assert downloads.set_state(download_id, "downloading") == "queued"
        assert downloads.get(download_id)["completed_at"] is None

        assert downloads.set_state(download_id, "failed") == "downloading"
        assert downloads.get(download_id)["completed_at"] is not None

    def test_set_state_on_unknown_row_returns_none(self, downloads):
        assert downloads.set_state(999, "failed") is None

    def test_completion_hands_the_file_to_file_management(
        self, downloads, download_id, files
    ):
        downloads.set_state(download_id, "completed")

        [placed] = files.pending()
        assert (placed["source"], placed["username"], placed["filename"]) == (
            "catalog",
            "peer-a",
            "a/Accordion.flac",
        )
        assert files.get(placed["id"])["mb_recording_id"] == "mbid-1"

    def test_handoff_fires_once(self, downloads, download_id, files):
        downloads.set_state(download_id, "completed")
        downloads.set_state(download_id, "completed")

        assert len(files.pending()) == 1

    def test_slskd_downloads_get_no_mb_provenance(self, requests_, downloads, files):
        request_id = requests_.get_or_create_slskd_request("q")
        requests_.add_slskd_search("s-9", request_id, "q")
        download_id = downloads.insert_queued(request_id, "s-9", "peer", "f.flac")

        downloads.set_state(download_id, "completed")

        [placed] = files.pending()
        assert placed["source"] == "slskd"
        assert files.get(placed["id"])["mb_recording_id"] is None

    def test_deleting_handed_off_download_keeps_file_and_drops_attempts(
        self, downloads, download_id, files, db
    ):
        downloads.set_state(download_id, "completed")

        assert [r["id"] for r in downloads.handed_off()] == [download_id]
        assert downloads.delete([download_id]) == 1

        assert len(files.pending()) == 1
        assert db.fetch_all("SELECT * FROM download_attempts") == []


class TestManualGate:
    def test_manual_download_holds_the_gate(self, downloads, download_id):
        assert downloads.has_active_manual_downloads() is True

    def test_rec_download_does_not_hold_the_gate(self, requests_, downloads):
        request_id = requests_.create_request("rec", "r", "mbid-r")
        requests_.add_slskd_search("s-r", request_id, "r")
        downloads.insert_queued(request_id, "s-r", "peer", "r.flac")

        assert downloads.has_active_manual_downloads() is False

    def test_long_queued_row_stops_holding_the_gate(self, downloads, download_id, db):
        db.execute(
            "UPDATE download_attempts SET attempted_at = ?", (int(time.time()) - 3600,)
        )

        assert downloads.has_active_manual_downloads(queued_grace_seconds=600) is False

        downloads.set_state(download_id, "downloading")
        assert downloads.has_active_manual_downloads(queued_grace_seconds=600) is True
