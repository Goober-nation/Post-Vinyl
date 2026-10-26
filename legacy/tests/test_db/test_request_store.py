"""RequestStore: origins, requests, slskd searches and pruning (#67)."""

import sqlite3
import time

import pytest


def _search(requests_, request_id, search_id="s-1", query="q"):
    requests_.add_slskd_search(search_id, request_id, query)
    return search_id


class TestOrigins:
    def test_catalog_search_creates_origin_and_type_row(self, requests_, db):
        origin_id = requests_.record_catalog_search("Madvillain", "album")

        assert db.fetch_one("SELECT kind FROM origins WHERE id = ?", (origin_id,)) == {
            "kind": "catalog_search"
        }
        assert requests_.list_catalog_searches()[0]["id"] == origin_id

    def test_repeat_catalog_search_reuses_row_and_bumps_last_used(self, requests_, db):
        first = requests_.record_catalog_search("Madvillain", "album")
        db.execute(
            "UPDATE catalog_searches SET last_used_at = 1 WHERE id = ?", (first,)
        )

        again = requests_.record_catalog_search("madvillain", "album")

        assert again == first
        assert len(requests_.list_catalog_searches()) == 1
        assert requests_.list_catalog_searches()[0]["last_used_at"] > 1

    def test_same_query_different_kind_is_a_separate_search(self, requests_):
        a = requests_.record_catalog_search("Madvillain", "album")
        b = requests_.record_catalog_search("Madvillain", "artist")

        assert a != b

    def test_deleting_origin_removes_type_row_and_unlinks_requests(self, requests_, db):
        origin_id = requests_.record_catalog_search("Madvillain", "album")
        request_id = requests_.create_request(
            "catalog", "MF DOOM – Accordion", "mbid-1", origin_id
        )

        assert requests_.delete_origin(origin_id) is True

        assert requests_.list_catalog_searches() == []
        request = requests_.get_request(request_id)
        assert request["origin_id"] is None
        assert request["mbid"] == "mbid-1"

    def test_request_with_unknown_origin_is_rejected(self, requests_):
        with pytest.raises(sqlite3.IntegrityError):
            requests_.create_request("catalog", "x", origin_id=999)

    def test_slskd_request_cannot_have_an_origin(self, requests_):
        origin_id = requests_.record_catalog_search("Madvillain", "album")

        with pytest.raises(sqlite3.IntegrityError):
            requests_.create_request("slskd", "x", origin_id=origin_id)


class TestRequests:
    def test_repeat_slskd_query_reuses_request(self, requests_, db):
        first = requests_.get_or_create_slskd_request("Madvillain")
        db.execute("UPDATE requests SET last_used_at = 1 WHERE id = ?", (first,))

        again = requests_.get_or_create_slskd_request("MADVILLAIN")

        assert again == first
        assert requests_.get_request(first)["last_used_at"] > 1

    def test_catalog_requests_are_never_collapsed(self, requests_):
        a = requests_.create_request("catalog", "same", "mbid-1")
        b = requests_.create_request("catalog", "same", "mbid-1")

        assert a != b

    def test_list_recent_filters_by_source_newest_first(self, requests_, db):
        old = requests_.get_or_create_slskd_request("old")
        new = requests_.get_or_create_slskd_request("new")
        requests_.create_request("catalog", "c")
        db.execute("UPDATE requests SET last_used_at = 1 WHERE id = ?", (old,))

        recent = requests_.list_recent_requests("slskd")

        assert [r["id"] for r in recent] == [new, old]


class TestSlskdSearches:
    def test_searches_belong_to_their_request_in_order(self, requests_):
        request_id = requests_.create_request("catalog", "t")
        requests_.add_slskd_search("s-1", request_id, "artist title")
        requests_.add_slskd_search("s-2", request_id, "title")

        assert [s["id"] for s in requests_.searches_for_request(request_id)] == [
            "s-1",
            "s-2",
        ]

    def test_re_adding_the_same_search_id_is_a_no_op(self, requests_):
        request_id = requests_.create_request("catalog", "t")
        requests_.add_slskd_search("s-1", request_id, "q")
        requests_.add_slskd_search("s-1", request_id, "q")

        assert len(requests_.searches_for_request(request_id)) == 1

    def test_search_cannot_be_claimed_by_another_request(self, requests_):
        owner = requests_.create_request("catalog", "t")
        other = requests_.create_request("catalog", "t")
        requests_.add_slskd_search("s-1", owner, "q")

        with pytest.raises(ValueError):
            requests_.add_slskd_search("s-1", other, "q")
        assert requests_.get_slskd_search("s-1")["request_id"] == owner


class TestDeletingRequests:
    def test_delete_cascades_to_searches_downloads_and_attempts(
        self, requests_, downloads, db
    ):
        request_id = requests_.create_request("slskd", "q")
        search_id = _search(requests_, request_id)
        download_id = downloads.insert_queued(request_id, search_id, "peer", "f.flac")
        downloads.set_state(download_id, "failed")

        assert requests_.delete_request(request_id) is True

        assert requests_.get_slskd_search(search_id) is None
        assert downloads.get(download_id) is None
        assert db.fetch_all("SELECT * FROM download_attempts") == []

    def test_delete_keeps_placed_file_records(self, requests_, downloads, files):
        request_id = requests_.create_request("catalog", "q", "mbid-1")
        search_id = _search(requests_, request_id)
        download_id = downloads.insert_queued(request_id, search_id, "peer", "f.flac")
        downloads.set_state(download_id, "completed")

        requests_.delete_request(request_id)

        [placed] = files.pending()
        assert files.get(placed["id"])["mb_recording_id"] == "mbid-1"

    @pytest.mark.parametrize("state", ["queued", "downloading"])
    def test_request_with_active_download_is_not_deleted(
        self, requests_, downloads, state
    ):
        request_id = requests_.create_request("slskd", "q")
        search_id = _search(requests_, request_id)
        download_id = downloads.insert_queued(request_id, search_id, "peer", "f.flac")
        downloads.set_state(download_id, state)

        assert requests_.delete_request(request_id) is False
        assert requests_.clear_requests() == 0
        assert requests_.get_request(request_id) is not None

    def test_clear_requests_by_source(self, requests_):
        requests_.get_or_create_slskd_request("a")
        catalog = requests_.create_request("catalog", "b")

        assert requests_.clear_requests("slskd") == 1
        assert [r["id"] for r in requests_.list_recent_requests()] == [catalog]

    def test_prune_deletes_only_requests_unused_for_the_age(self, requests_, db):
        old = requests_.get_or_create_slskd_request("old")
        fresh = requests_.get_or_create_slskd_request("fresh")
        db.execute(
            "UPDATE requests SET last_used_at = ? WHERE id = ?",
            (int(time.time()) - 31 * 86400, old),
        )

        assert requests_.prune_requests(30 * 86400) == 1
        assert requests_.get_request(old) is None
        assert requests_.get_request(fresh) is not None
