"""Deezer client: errors inside HTTP 200, retries, paging, parsing (#82).
Shapes copied from live Deezer responses (2026-10-28), values trimmed."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import deezer
from app.clients.catalog import CatalogBusy, CatalogError, CatalogNotFound
from app.clients.deezer import DeezerClient

TRACK = {
    "id": 3135556,
    "title": "Kickstart My Heart (Live)",
    "title_short": "Kickstart My Heart",
    "title_version": "(Live)",
    "isrc": "USBY29900215",
    "duration": 283,
    "disk_number": 1,
    "track_position": 14,
    "artist": {"id": 1234, "name": "Mötley Crüe"},
    "contributors": [
        {"id": 1234, "name": "Mötley Crüe", "role": "Main"},
        {"id": 99, "name": "Guest", "role": "Featured"},
    ],
}


def deezer_error(code: int) -> dict:
    return {"error": {"type": "DataException", "message": "x", "code": code}}


class FakeResp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self) -> Any:
        return self._body


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(deezer.time, "sleep", sleeps.append)
    return sleeps


def client_returning(
    monkeypatch: pytest.MonkeyPatch, *resps: FakeResp
) -> tuple[DeezerClient, list[tuple[str, dict]]]:
    client = DeezerClient()
    calls: list[tuple[str, dict]] = []
    queue = list(resps)

    def get(url: str, params: dict, timeout: float) -> FakeResp:
        calls.append((url, dict(params)))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(client._http, "get", get)
    return client, calls


def test_track_parse_splits_version() -> None:
    track = deezer.parse_track(TRACK)
    assert track.id == "3135556" and track.title == "Kickstart My Heart"
    assert track.disambiguation == "(Live)" and track.duration_s == 283.0
    assert track.artist_ids == ("1234", "99") and track.isrcs == ("USBY29900215",)
    assert (track.disc, track.position) == (1, 14)


def test_search_result_without_contributors() -> None:
    raw = {"id": 1, "title": "T", "artist": {"id": 7, "name": "A"}, "duration": 0}
    track = deezer.parse_track(raw)
    assert track.artist_ids == ("7",) and track.duration_s is None and track.isrcs == ()


def test_search_sends_plain_query(monkeypatch: pytest.MonkeyPatch) -> None:
    # the artist:"…" field is broken at Deezer; never send it
    client, calls = client_returning(monkeypatch, FakeResp({"data": [TRACK]}))
    client.search_tracks("Kickstart My Heart", "Mötley Crüe")
    assert calls[0][1]["q"] == "Kickstart My Heart Mötley Crüe"
    assert calls[0][0].endswith("/search/track")


@pytest.mark.parametrize(
    ("code", "error"), [(800, CatalogNotFound), (500, CatalogError)]
)
def test_error_inside_http_200(
    monkeypatch: pytest.MonkeyPatch, code: int, error: type
) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(deezer_error(code)))
    with pytest.raises(error):
        client.get_track("1")


def test_quota_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    client, calls = client_returning(
        monkeypatch, FakeResp(deezer_error(4)), FakeResp(TRACK)
    )
    assert client.get_track("1").id == "3135556"
    assert len(calls) == 2 and 2.0 in no_waiting


def test_quota_every_time_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(deezer_error(700)))
    with pytest.raises(CatalogBusy):
        client.get_track("1")
    assert len(calls) == 3


def test_http_error_is_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp("bad gateway", 502))
    with pytest.raises(CatalogError):
        client.get_track("1")


def test_isrc_lookup_is_a_list_of_one(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(TRACK))
    assert [t.id for t in client.tracks_by_isrc("USBY29900215")] == ["3135556"]
    assert calls[0][0].endswith("/track/isrc:USBY29900215")


def test_artist_albums_pages_and_types(monkeypatch: pytest.MonkeyPatch) -> None:
    album = {"id": 5, "title": "Best Of", "record_type": "compile"}
    page1 = {"data": [album] * 100, "total": 101, "next": "…"}
    page2 = {"data": [album], "total": 101}
    client, calls = client_returning(
        monkeypatch,
        FakeResp({"id": 1234, "name": "Mötley Crüe"}),
        FakeResp(page1),
        FakeResp(page2),
    )
    albums = client.artist_albums("1234")
    assert len(albums) == 101 and [c[1].get("index") for c in calls[1:]] == [0, 100]
    assert albums[0].artist == "Mötley Crüe"
    assert (albums[0].primary_type, albums[0].secondary_types) == (
        "Album",
        ("Compilation",),
    )


def test_album_has_one_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "id": 9,
        "title": "Dr. Feelgood",
        "release_date": "1989-09-01",
        "nb_tracks": 20,
    }
    client, _ = client_returning(monkeypatch, FakeResp(raw))
    [edition] = client.album_editions("9")
    assert (edition.id, edition.track_count, edition.date) == ("9", 20, "1989-09-01")


def test_limiter_spaces_calls(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    now = [1000.0]
    monkeypatch.setattr(deezer.time, "monotonic", lambda: now[0])
    deezer._wait_turn()
    now[0] += 0.04
    deezer._wait_turn()
    assert no_waiting[-1] == pytest.approx(0.06)


def test_unknown_record_type_keeps_empty_type() -> None:
    album = deezer.parse_album({"id": 1, "title": "T", "record_type": "audiobook"})
    assert album.primary_type == "" and album.secondary_types == ()


def test_album_artist_from_search_item_wins() -> None:
    raw = {"id": 1, "title": "T", "artist": {"name": "From item"}}
    assert deezer.parse_album(raw, "Fallback").artist == "From item"


def test_paging_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # a "next" link on an empty page must not loop forever
    client, calls = client_returning(
        monkeypatch,
        FakeResp({"data": [{"id": 1, "title": "T"}], "next": "…"}),
        FakeResp({"data": [], "next": "…"}),
    )
    assert len(client.edition_tracks("9")) == 1 and len(calls) == 2


def test_connection_error_is_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DeezerClient()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise deezer.requests.ConnectionError("refused")

    monkeypatch.setattr(client._http, "get", boom)
    with pytest.raises(CatalogError):
        client.search_artists("x")


def test_search_without_artist_is_title_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp({"data": []}))
    assert client.search_albums("Dr. Feelgood") == []
    assert calls[0][1] == {"q": "Dr. Feelgood", "limit": 25}


def test_search_artists_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"data": [{"id": 1234, "name": "Mötley Crüe"}]}
    client, _ = client_returning(monkeypatch, FakeResp(body))
    [artist] = client.search_artists("Mötley Crüe")
    assert (artist.id, artist.name, artist.source) == ("1234", "Mötley Crüe", "deezer")
