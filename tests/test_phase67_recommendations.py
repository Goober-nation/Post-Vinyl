"""Focused tests for the Phase 6.7 recommendation fill mechanics."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import FreshPicksConfig
from app.db.database import Database
from app.db.recs_store import RecsStore
from app.exceptions import RecommendationFetchError
from app.services.recommendation import ListenBrainzRecs


class _Config:
    class ListenBrainz:
        enabled = True
        url = "https://api.listenbrainz.org"
        token = "token"
        username = "user"

    class Recs:
        comfort_zone_count = 2
        deep_cuts_count = 2

    def __init__(self):
        self.listenbrainz = self.ListenBrainz()
        self.recs = self.Recs()
        self.fresh_picks = FreshPicksConfig()
        self.fresh_picks.count = 1


def _database(tmp_path: Path) -> Database:
    class Paths:
        data_dir = str(tmp_path / "data")

    class Config:
        paths = Paths()

    database = Database(Config())
    database.initialize_schema()
    return database


def _response(payload: dict) -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload
    return response


def _comfort_payload(offset: int, last_updated: str, mbids: list[str]) -> dict:
    return {
        "payload": {
            "count": len(mbids),
            "offset": offset,
            "total_mbid_count": 4,
            "last_updated": last_updated,
            "mbids": [{"recording_mbid": mbid} for mbid in mbids],
        }
    }


def test_comfort_zone_paginates_and_persists_cursor(tmp_path):
    database = _database(tmp_path)
    try:
        store = RecsStore(database)
        service = ListenBrainzRecs(_Config())
        service.set_state_store(store)
        service._fetch_recording_metadata = lambda mbid: {
            "artist_name": "Artist",
            "track_name": mbid,
        }
        service.session.get = Mock(
            side_effect=[
                _response(_comfort_payload(0, "model-1", ["a", "b"])),
                _response(_comfort_payload(2, "model-1", ["c", "d"])),
            ]
        )

        first = service._fetch_comfort_zone(2)
        second = service._fetch_comfort_zone(2)

        assert [rec.mbid for rec in first] == ["a", "b"]
        assert [rec.mbid for rec in second] == ["c", "d"]
        calls = service.session.get.call_args_list
        assert [call.kwargs["params"]["offset"] for call in calls] == [0, 2]
        state = store.get_category_state("comfort_zone")
        assert state["offset"] == 0
        assert state["last_updated"] == "model-1"
        assert "repeats" in state["warning"]
    finally:
        database.close()


def test_comfort_zone_model_change_restarts_from_zero(tmp_path):
    database = _database(tmp_path)
    try:
        store = RecsStore(database)
        store.set_category_state(
            "comfort_zone",
            offset=2,
            last_updated="old-model",
            total_count=4,
            warning=None,
        )
        service = ListenBrainzRecs(_Config())
        service.set_state_store(store)
        service._fetch_recording_metadata = lambda mbid: {
            "artist_name": "Artist",
            "track_name": mbid,
        }
        service.session.get = Mock(
            side_effect=[
                _response(_comfort_payload(2, "new-model", ["stale"])),
                _response(_comfort_payload(0, "new-model", ["fresh-1", "fresh-2"])),
            ]
        )

        recs = service._fetch_comfort_zone(2)

        assert [rec.mbid for rec in recs] == ["fresh-1", "fresh-2"]
        calls = service.session.get.call_args_list
        assert [call.kwargs["params"]["offset"] for call in calls] == [2, 0]
        assert store.get_category_state("comfort_zone")["last_updated"] == "new-model"
    finally:
        database.close()


def test_comfort_zone_rejects_changed_payload_shape(tmp_path):
    database = _database(tmp_path)
    try:
        service = ListenBrainzRecs(_Config())
        service.session.get = Mock(return_value=_response({"payload": {}}))
        with pytest.raises(RecommendationFetchError):
            service._fetch_comfort_zone(1)
    finally:
        database.close()


def test_deep_cuts_ingests_each_uuid_and_serves_pool_in_batches(tmp_path):
    database = _database(tmp_path)
    try:
        store = RecsStore(database)
        service = ListenBrainzRecs(_Config())
        service.set_state_store(store)
        listing = {
            "playlists": [
                {
                    "playlist": {
                        "identifier": "https://listenbrainz.org/playlist/uuid-a",
                        "title": "Weekly A",
                    }
                }
            ]
        }
        tracks = {
            "playlist": {
                "track": [
                    {"creator": "A", "title": "one", "identifier": []},
                    {"creator": "A", "title": "two", "identifier": []},
                    {"creator": "A", "title": "three", "identifier": []},
                ]
            }
        }
        service.session.get = Mock(
            side_effect=[
                _response(listing),
                _response(tracks),
                _response(listing),
            ]
        )

        first = service._fetch_deep_cuts(2)
        second = service._fetch_deep_cuts(1)

        assert [rec.track for rec in first] == ["one", "two"]
        assert [rec.track for rec in second] == ["three"]
        assert len(service.session.get.call_args_list) == 3
        assert len(store.list_deep_cuts_pool(unserved_only=True)) == 0
    finally:
        database.close()


def test_deep_cuts_warns_when_pool_drains(tmp_path):
    database = _database(tmp_path)
    try:
        store = RecsStore(database)
        service = ListenBrainzRecs(_Config())
        service.set_state_store(store)
        listing = {
            "playlists": [
                {"playlist": {"identifier": "https://listenbrainz.org/playlist/uuid-a"}}
            ]
        }
        tracks = {"playlist": {"track": [{"creator": "A", "title": "one"}]}}
        service.session.get = Mock(
            side_effect=[_response(listing), _response(tracks), _response(listing)]
        )

        assert len(service._fetch_deep_cuts(1)) == 1
        assert service._fetch_deep_cuts(1) == []
        assert "Deep Cuts" in store.category_warnings()["deep_cuts"]
    finally:
        database.close()


def test_fresh_picks_skips_offset_filters_window_and_overfetches():
    config = _Config()
    config.fresh_picks.offset = 2
    config.fresh_picks.count = 1
    config.fresh_picks.search_buffer = 2
    config.fresh_picks.pull_window = "2d"
    service = ListenBrainzRecs(config)
    today = datetime.now(timezone.utc).date()
    releases = [
        {
            "artist_credit_name": "Skip",
            "release_name": "new-1",
            "release_date": str(today),
        },
        {
            "artist_credit_name": "Skip",
            "release_name": "new-2",
            "release_date": str(today),
        },
        {
            "artist_credit_name": "Keep",
            "release_name": "today",
            "release_date": str(today),
        },
        {
            "artist_credit_name": "Keep",
            "release_name": "yesterday",
            "release_date": str(today - timedelta(days=1)),
        },
        {
            "artist_credit_name": "Old",
            "release_name": "old",
            "release_date": str(today - timedelta(days=3)),
        },
    ]
    response = _response({"payload": {"releases": releases}})
    service.session.get = Mock(return_value=response)

    recs = service._fetch_fresh_picks(1)

    assert [rec.track for rec in recs] == ["today", "yesterday"]
    assert service.session.get.call_args.kwargs["params"] == {"days": 2}
