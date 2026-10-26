"""list_searches must not report a finished slskd search as still searching."""

from datetime import datetime
from unittest.mock import Mock

import requests

from app.services.interfaces.search import SearchJob
from app.services.search import SlskdSearch


class _Slskd:
    url = "http://slskd:5030"
    api_key = "test-api-key"


class _Config:
    slskd = _Slskd()


def _job(search_id: str, status: str = "searching") -> SearchJob:
    return SearchJob(
        search_id=search_id,
        query="radiohead creep",
        artist=None,
        created_at=datetime.now(),
        status=status,
    )


def _meta(is_complete: bool) -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {"id": "s1", "isComplete": is_complete}
    return resp


def test_list_refreshes_a_searching_job_from_slskd():
    search = SlskdSearch(_Config())
    search._searches["s1"] = _job("s1")
    search.session = Mock()
    search.session.get.return_value = _meta(is_complete=True)

    listed = search.list_searches()

    assert listed[0].status == "completed"
    search.session.get.assert_called_once()


def test_list_keeps_cached_status_when_slskd_is_unreachable():
    search = SlskdSearch(_Config())
    search._searches["s1"] = _job("s1")
    search.session = Mock()
    search.session.get.side_effect = requests.exceptions.ConnectionError("slskd down")

    listed = search.list_searches()

    assert len(listed) == 1
    assert listed[0].status == "searching"


def test_list_does_not_refetch_finished_jobs():
    search = SlskdSearch(_Config())
    search._searches["s1"] = _job("s1", status="completed")
    search.session = Mock()

    search.list_searches()

    search.session.get.assert_not_called()
