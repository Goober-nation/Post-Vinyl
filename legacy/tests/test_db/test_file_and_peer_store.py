"""FileStore stages and PeerStore blocking (#67)."""

import time

import pytest


@pytest.fixture
def file_id(requests_, downloads, files):
    request_id = requests_.create_request("slskd", "q")
    requests_.add_slskd_search("s-1", request_id, "q")
    download_id = downloads.insert_queued(request_id, "s-1", "peer", "dir/track.flac")
    downloads.set_state(download_id, "completed")
    return files.pending()[0]["id"]


class TestFileStore:
    def test_moved_file_waits_for_navidrome_link(self, files, file_id):
        files.mark_moved(file_id, "new-system-tests/track (1).flac")

        assert files.pending() == []
        [unlinked] = files.unlinked_moved()
        assert unlinked["placed_path"] == "new-system-tests/track (1).flac"

        files.set_navidrome_id(file_id, "nd-1")
        assert files.unlinked_moved() == []
        assert files.get(file_id)["placed_path"] == "new-system-tests/track (1).flac"

    def test_unlinked_moved_can_be_limited_to_before_a_scan(self, files, file_id, db):
        files.mark_moved(file_id, "a.flac")
        db.execute(
            "UPDATE file_management SET downloaded_at = 100 WHERE id = ?", (file_id,)
        )

        assert files.unlinked_moved(moved_before=50) == []
        assert len(files.unlinked_moved(moved_before=200)) == 1

    def test_unindexed_is_terminal(self, files, file_id):
        files.mark_moved(file_id, "a.flac")
        files.mark_unindexed(file_id)

        assert files.unlinked_moved() == []
        assert files.get(file_id)["stage"] == "unindexed"

    def test_missing_timeout_uses_stored_download_time(self, files, file_id, db):
        assert files.missing_since(60) == []

        db.execute(
            "UPDATE file_management SET downloaded_at = ? WHERE id = ?",
            (int(time.time()) - 600, file_id),
        )
        assert [f["id"] for f in files.missing_since(60)] == [file_id]

        files.mark_missing(file_id)
        assert files.missing_since(60) == []


class TestPeerStore:
    def test_clean_peer_has_no_row(self, peers, db):
        assert peers.is_blocked("p") is False
        assert peers.failure_count("p") == 0
        assert db.fetch_all("SELECT * FROM peers") == []

    def test_reaching_threshold_blocks_once(self, peers):
        assert peers.record_failure("p", threshold=2) == (1, False)
        assert peers.is_blocked("p") is False
        assert peers.record_failure("p", threshold=2) == (2, True)
        assert peers.record_failure("p", threshold=2) == (3, False)
        assert peers.is_blocked("p") is True

    def test_expired_ban_deletes_the_row(self, peers, db):
        peers.record_failure("p", threshold=1)
        db.execute("UPDATE peers SET blocked_at = ?", (int(time.time()) - 3 * 86400,))

        assert peers.is_blocked("p", ban_seconds=2 * 86400) is False
        assert db.fetch_all("SELECT * FROM peers") == []

    def test_active_ban_is_kept(self, peers):
        peers.record_failure("p", threshold=1)

        assert peers.is_blocked("p", ban_seconds=2 * 86400) is True

    def test_unblock_all_deletes_only_blocked_peers(self, peers):
        peers.record_failure("blocked", threshold=1)
        peers.record_failure("warned", threshold=5)

        assert peers.unblock_all() == 1
        assert peers.is_blocked("blocked") is False
        assert peers.failure_count("warned") == 1
