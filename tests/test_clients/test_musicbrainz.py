"""MusicBrainz client: parsing, query quoting, 503 backoff, paging (#81).
Shapes copied from live MusicBrainz responses (2026-10-28), values trimmed."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import musicbrainz
from app.clients.catalog import CatalogBusy, CatalogError, CatalogNotFound
from app.clients.musicbrainz import MusicBrainzClient

CREDIT = [
    {"name": "A", "joinphrase": " feat. ", "artist": {"id": "a1"}},
    {"name": "B", "joinphrase": "", "artist": {"id": "a2"}},
]
RECORDING = {
    "id": "r1",
    "title": "Song",
    "length": 282400,
    "disambiguation": "",
    "artist-credit": CREDIT,
    "isrcs": ["USEE10001994"],
}


class FakeResp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        return self._body


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(musicbrainz.time, "sleep", sleeps.append)
    return sleeps


def client_returning(
    monkeypatch: pytest.MonkeyPatch, *resps: FakeResp
) -> tuple[MusicBrainzClient, list[tuple[str, dict]]]:
    client = MusicBrainzClient()
    calls: list[tuple[str, dict]] = []
    queue = list(resps)

    def get(url: str, params: dict, timeout: float) -> FakeResp:
        calls.append((url, dict(params)))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(client._http, "get", get)
    return client, calls


def test_recording_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, RECORDING))
    track = client.get_track("r1")
    assert calls[0][1]["inc"] == "artist-credits+isrcs"
    assert track.artist == "A feat. B" and track.artist_ids == ("a1", "a2")
    assert track.duration_s == 282.4 and track.isrcs == ("USEE10001994",)


def test_search_quotes_phrases(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"recordings": [RECORDING]}
    client, calls = client_returning(monkeypatch, FakeResp(200, body))
    client.search_tracks('Say "Hi" \\ AC/DC', "AC/DC")
    assert calls[0][1]["query"] == (
        'recording:"Say \\"Hi\\" \\\\ AC/DC" AND artist:"AC/DC"'
    )


def test_user_agent_names_this_project() -> None:
    ua = MusicBrainzClient()._http.headers["User-Agent"]
    assert ua.startswith("PostVinyl/") and "Goober-nation/Post-Vinyl" in ua


def test_503_backs_off_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    client, calls = client_returning(
        monkeypatch, FakeResp(503), FakeResp(200, RECORDING)
    )
    assert client.get_track("r1").id == "r1"
    assert len(calls) == 2 and 2.0 in no_waiting


def test_503_every_time_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(503))
    with pytest.raises(CatalogBusy):
        client.get_track("r1")
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("status", "error"), [(404, CatalogNotFound), (400, CatalogError)]
)
def test_errors(monkeypatch: pytest.MonkeyPatch, status: int, error: type) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(status, {"error": "x"}))
    with pytest.raises(error):
        client.get_track("not-a-uuid")


def test_browse_reads_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    group = {"id": "g", "title": "T", "primary-type": "Album"}
    page1 = {"release-groups": [group] * 100, "release-group-count": 101}
    page2 = {"release-groups": [group], "release-group-count": 101}
    client, calls = client_returning(
        monkeypatch, FakeResp(200, page1), FakeResp(200, page2)
    )
    assert len(client.artist_albums("a1")) == 101
    assert [c[1]["offset"] for c in calls] == [0, 100]


def test_edition_tracks_keep_disc_and_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = {
        "media": [
            {"position": 1, "tracks": [{"position": 1, "recording": RECORDING}]},
            {"position": 2, "tracks": [{"position": 1, "recording": RECORDING}]},
        ]
    }
    client, _ = client_returning(monkeypatch, FakeResp(200, release))
    tracks = client.edition_tracks("rel1")
    assert [(t.disc, t.position) for t in tracks] == [(1, 1), (2, 1)]
