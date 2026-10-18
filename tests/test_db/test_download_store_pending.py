"""DownloadStore.insert_pending and import_pending (#67).

insert_pending creates the request, search and download rows for a file
about to be queued. import_pending is a pure lookup against file_management.
"""

import pytest


def _rows(db, sql, params=()):
    return db.fetch_all(sql, params)


class TestInsertPending:
    def test_creates_request_search_download_and_attempt(self, downloads, db):
        download_id = downloads.insert_pending("s-1", "peer-a", "a/Track.flac", 123)

        assert isinstance(download_id, int)

        request = _rows(db, "SELECT * FROM requests")
        assert len(request) == 1
        assert request[0]["source"] == "slskd"
        assert request[0]["mbid"] is None
        assert request[0]["query"] is None
        assert request[0]["origin_id"] is None
        assert request[0]["created_at"] == request[0]["last_used_at"]

        search = _rows(db, "SELECT * FROM slskd_searches")
        assert len(search) == 1
        assert search[0]["id"] == "s-1"
        assert search[0]["request_id"] == request[0]["id"]
        assert search[0]["query"] == ""

        download = downloads.get(download_id)
        assert download["state"] == "queued"
        assert download["request_id"] == request[0]["id"]
        assert download["search_id"] == "s-1"
        assert download["username"] == "peer-a"
        assert download["filename"] == "a/Track.flac"
        assert download["size"] == 123
        assert download["slskd_id"] is None

        attempts = _rows(db, "SELECT download_id, username, success FROM download_attempts")
        assert attempts == [{"download_id": download_id, "username": "peer-a", "success": 0}]

    def test_is_rec_sets_rec_source_and_stores_mbid(self, downloads, db):
        downloads.insert_pending(
            "s-rec", "peer-a", "a/Track.flac", None, is_rec=True, mb_recording_id="mbid-9"
        )

        request = db.fetch_one("SELECT source, mbid FROM requests")
        assert request == {"source": "rec", "mbid": "mbid-9"}

    def test_default_source_is_slskd(self, downloads, db):
        downloads.insert_pending("s-1", "peer-a", "a/Track.flac")

        assert db.fetch_one("SELECT source FROM requests") == {"source": "slskd"}

    def test_second_call_reuses_search_and_keeps_its_request_id(self, downloads, db):
        first = downloads.insert_pending("s-1", "peer-a", "a/One.flac")
        first_request = downloads.get(first)["request_id"]

        second = downloads.insert_pending("s-1", "peer-b", "b/Two.flac")

        assert first != second
        searches = _rows(db, "SELECT id, request_id FROM slskd_searches")
        assert searches == [{"id": "s-1", "request_id": first_request}]
        # Each call creates its own request; the search still belongs to the first.
        assert db.fetch_one("SELECT COUNT(*) AS n FROM requests")["n"] == 2
        assert downloads.get(second)["request_id"] != first_request

    def test_single_call_writes_one_download_and_one_attempt(self, downloads, db):
        downloads.insert_pending("s-1", "peer-a", "a/One.flac")

        assert db.fetch_one("SELECT COUNT(*) AS n FROM downloads")["n"] == 1
        assert db.fetch_one("SELECT COUNT(*) AS n FROM download_attempts")["n"] == 1


class TestImportPending:
    @pytest.fixture
    def add_file(self, db):
        def add(username, filename, stage):
            db.execute(
                "INSERT INTO file_management (source, username, filename, stage, downloaded_at) "
                "VALUES ('slskd', ?, ?, ?, 0)",
                (username, filename, stage),
            )

        return add

    def test_false_when_no_row(self, downloads):
        assert downloads.import_pending("peer-a", "a/Track.flac") is False

    def test_true_for_pending_row(self, downloads, add_file):
        add_file("peer-a", "a/Track.flac", "pending")

        assert downloads.import_pending("peer-a", "a/Track.flac") is True

    @pytest.mark.parametrize("stage", ["moved", "missing", "unindexed"])
    def test_false_when_stage_is_not_pending(self, downloads, add_file, stage):
        add_file("peer-a", "a/Track.flac", stage)

        assert downloads.import_pending("peer-a", "a/Track.flac") is False

    def test_does_not_change_the_row(self, downloads, add_file, db):
        add_file("peer-a", "a/Track.flac", "pending")

        downloads.import_pending("peer-a", "a/Track.flac")

        assert db.fetch_one("SELECT stage FROM file_management")["stage"] == "pending"

    def test_other_peer_or_file_does_not_match(self, downloads, add_file):
        add_file("peer-a", "a/Track.flac", "pending")

        assert downloads.import_pending("peer-b", "a/Track.flac") is False
        assert downloads.import_pending("peer-a", "a/Other.flac") is False


class TestForeignKeys:
    def test_deleting_request_cascades_to_downloads_and_attempts(self, downloads, db):
        download_id = downloads.insert_pending("s-1", "peer-a", "a/Track.flac")
        request_id = downloads.get(download_id)["request_id"]

        assert db.fetch_one("PRAGMA foreign_keys")["foreign_keys"] == 1

        db.execute("DELETE FROM requests WHERE id = ?", (request_id,))

        assert downloads.get(download_id) is None
        assert db.fetch_one("SELECT COUNT(*) AS n FROM download_attempts")["n"] == 0
        assert db.fetch_one("SELECT COUNT(*) AS n FROM slskd_searches")["n"] == 0
