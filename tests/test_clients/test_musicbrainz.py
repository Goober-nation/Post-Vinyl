"""MusicBrainz client: parsing, query quoting, 503 backoff, paging (#81).
Shapes copied from live MusicBrainz responses (2026-10-28), values trimmed."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import musicbrainz
from app.clients.catalog import CatalogBusy, CatalogError, CatalogNotFound, ReleaseRef
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


def test_recording_with_missing_fields() -> None:
    # MB omits length and artist ids on some recordings; nothing may crash
    raw = {"id": "r2", "title": "T", "artist-credit": [{"name": "Anon"}]}
    track = musicbrainz.parse_recording(raw)
    assert track.duration_s is None and track.artist == "Anon"
    assert track.artist_ids == () and track.isrcs == () and track.disambiguation == ""


def test_browse_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # a count that never gets reached must not loop forever
    group = {"id": "g", "title": "T"}
    page1 = {"release-groups": [group], "release-group-count": 5}
    page2 = {"release-groups": [], "release-group-count": 5}
    client, calls = client_returning(
        monkeypatch, FakeResp(200, page1), FakeResp(200, page2)
    )
    assert len(client.artist_albums("a1")) == 1 and len(calls) == 2


def test_connection_error_is_catalog_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MusicBrainzClient()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise musicbrainz.requests.ConnectionError("refused")

    monkeypatch.setattr(client._http, "get", boom)
    with pytest.raises(CatalogError):
        client.search_artists("x")


def test_limiter_spaces_calls(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    now = [1000.0]
    monkeypatch.setattr(musicbrainz.time, "monotonic", lambda: now[0])
    musicbrainz._wait_turn()
    now[0] += 0.3
    musicbrainz._wait_turn()
    assert no_waiting[-1] == pytest.approx(0.7)


def test_limiter_is_shared_by_instances(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    monkeypatch.setattr(musicbrainz.time, "monotonic", lambda: 2000.0)
    first, _ = client_returning(monkeypatch, FakeResp(200, RECORDING))
    second, _ = client_returning(monkeypatch, FakeResp(200, RECORDING))
    first.get_track("r1")
    second.get_track("r1")
    assert no_waiting == [pytest.approx(1.0)]


def test_unknown_isrc_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(404, {"error": "x"}))
    with pytest.raises(CatalogNotFound):
        client.tracks_by_isrc("XX0000000000")
    assert calls[0][0].endswith("/ws/2/isrc/XX0000000000")


def test_search_albums_and_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "release-groups": [
            {
                "id": "g1",
                "title": "Dr. Feelgood",
                "primary-type": "Album",
                "secondary-types": ["Live"],
                "first-release-date": "1989-08-28",
                "artist-credit": CREDIT,
            }
        ],
        "artists": [{"id": "a1", "name": "A", "disambiguation": "US band"}],
    }
    client, calls = client_returning(monkeypatch, FakeResp(200, body))
    [album] = client.search_albums("Dr. Feelgood", "A")
    [artist] = client.search_artists("A")
    assert calls[0][1]["query"] == 'releasegroup:"Dr. Feelgood" AND artist:"A"'
    assert album.secondary_types == ("Live",) and album.artist == "A feat. B"
    assert artist.disambiguation == "US band"


def test_edition_counts_tracks_over_media() -> None:
    raw = {
        "id": "e1",
        "title": "T",
        "media": [
            {"format": "CD", "track-count": 12},
            {"format": None, "track-count": 4},
        ],
    }
    edition = musicbrainz.parse_release(raw)
    assert edition.formats == ("CD", "") and edition.track_count == 16
    assert edition.date == "" and edition.status == ""


def test_fake_catalog_behaves_like_a_source() -> None:
    from app.clients.catalog_fake import FakeCatalog

    fake = FakeCatalog()
    track = musicbrainz.parse_recording(RECORDING)
    fake.tracks[track.id] = track
    assert fake.search_tracks("song", "a feat") == [track]
    assert fake.tracks_by_isrc("USEE10001994") == [track]
    with pytest.raises(CatalogNotFound):
        fake.get_track("missing")


def test_search_hit_keeps_score_date_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape as in a live /recording search response (2026-10-29, #98)."""
    hit = {
        **RECORDING,
        "score": 77,
        "first-release-date": "2005-11-11",
        "releases": [
            {"status": "Official", "release-group": {"primary-type": "Album"}},
            {
                "status": "Bootleg",
                "release-group": {
                    "primary-type": "Other",
                    "secondary-types": ["Mixtape/Street"],
                },
            },
            {},
        ],
    }
    client, _ = client_returning(monkeypatch, FakeResp(200, {"recordings": [hit]}))
    (track,) = client.search_tracks("x")
    assert track.score == 77 and track.first_release_date == "2005-11-11"
    assert track.releases == (
        ReleaseRef("Official", "Album", ()),
        ReleaseRef("Bootleg", "Other", ("Mixtape/Street",)),
        ReleaseRef("", "", ()),
    )
