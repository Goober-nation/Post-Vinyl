"""ListenBrainz client: parsing, rate-limit headers, auth, batching (#83).
Shapes copied from live ListenBrainz responses (2026-10-28), values trimmed."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients import listenbrainz
from app.clients.listenbrainz import (
    ListenBrainzAuthError,
    ListenBrainzBusy,
    ListenBrainzClient,
    ListenBrainzError,
)
from app.clients.listenbrainz_fake import FakeListenBrainzClient

MBID = "c0ffee00-0000-4000-8000-000000000001"
META = {
    "recording": {"name": "Kickstart My Heart", "length": 283000, "isrcs": None},
    "artist": {
        "artists": [
            {"name": "A", "join_phrase": " feat. ", "artist_mbid": "a1"},
            {"name": "B", "join_phrase": "", "artist_mbid": "a2"},
        ]
    },
    "release": {
        "name": "Dr. Feelgood",
        "mbid": "rel1",
        "release_group_mbid": "rg1",
        "year": 1989,
    },
    "tag": {
        "recording": [{"tag": "rock", "count": 1}, {"tag": "hard rock", "count": 3}],
        "artist": [{"tag": "glam metal", "count": 9}],
    },
}


class FakeResp:
    def __init__(
        self, status: int, body: Any = None, headers: dict | None = None
    ) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> Any:
        return self._body


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(listenbrainz.time, "sleep", sleeps.append)
    return sleeps


def client_returning(
    monkeypatch: pytest.MonkeyPatch, *resps: FakeResp
) -> tuple[ListenBrainzClient, list[tuple[str, str, dict]]]:
    client = ListenBrainzClient()
    calls: list[tuple[str, str, dict]] = []
    queue = list(resps)

    def request(method: str, url: str, **kwargs: Any) -> FakeResp:
        calls.append((method, url, kwargs))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(client._http, "request", request)
    return client, calls


def test_metadata_parse() -> None:
    meta = listenbrainz.parse_metadata(MBID, META)
    assert meta.artist == "A feat. B" and meta.artist_mbids == ("a1", "a2")
    assert meta.length_s == 283.0 and meta.release is not None
    assert (meta.release.name, meta.release.year) == ("Dr. Feelgood", 1989)
    assert meta.recording_tags == ("hard rock", "rock")  # most used first
    assert meta.artist_tags == ("glam metal",) and meta.release_group_tags == ()


def test_metadata_with_missing_parts() -> None:
    meta = listenbrainz.parse_metadata(MBID, {"recording": {"name": "T"}})
    assert meta.length_s is None and meta.release is None and meta.artist == ""


def test_metadata_unknown_mbids_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, {MBID: META}))
    result = client.recording_metadata([MBID, "unknown"])
    assert list(result) == [MBID]
    assert calls[0][0] == "POST" and calls[0][2]["json"]["inc"] == "artist release tag"


def test_metadata_batches_at_1000(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, {}))
    client.recording_metadata([f"m{i}" for i in range(2001)])
    assert [len(c[2]["json"]["recording_mbids"]) for c in calls] == [1000, 1000, 1]


def test_lookup_sends_token_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    match = {"recording_mbid": MBID, "recording_name": "x", "metadata": META}
    client, calls = client_returning(monkeypatch, FakeResp(200, match))
    meta = client.lookup_recording("A", "Kickstart My Heart", "tok")
    assert meta is not None and meta.title == "Kickstart My Heart"
    assert calls[0][2]["headers"] == {"Authorization": "Token tok"}


def test_lookup_without_metadata_block(monkeypatch: pytest.MonkeyPatch) -> None:
    match = {
        "recording_mbid": MBID,
        "recording_name": "Song",
        "artist_credit_name": "A feat. B",
        "artist_mbids": ["a1", "a2"],
    }
    client, _ = client_returning(monkeypatch, FakeResp(200, match))
    meta = client.lookup_recording("A", "Song", "tok")
    assert meta is not None and (meta.title, meta.artist) == ("Song", "A feat. B")


def test_lookup_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(200, {}))
    assert client.lookup_recording("nobody", "nothing", "tok") is None


def test_lookup_name_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, {}))
    with pytest.raises(ListenBrainzError):
        client.lookup_recording("a" * 200, "t" * 51, "tok")
    assert calls == []


def test_bad_token_is_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(401, {"code": 401}))
    with pytest.raises(ListenBrainzAuthError):
        client.lookup_recording("A", "T", "bad")


def test_validate_token_invalid_is_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"code": 200, "message": "Token invalid.", "valid": False}
    client, _ = client_returning(monkeypatch, FakeResp(200, body))
    check = client.validate_token("bad")
    assert not check.valid and check.user_name is None


def test_waits_when_window_is_used_up(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "7"}
    client, _ = client_returning(monkeypatch, FakeResp(200, {}, headers))
    client.recording_metadata([MBID])
    assert no_waiting == [7.0]


def test_429_waits_then_retries(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    client, calls = client_returning(
        monkeypatch,
        FakeResp(429, "slow", {"X-RateLimit-Reset-In": "3"}),
        FakeResp(200, {MBID: META}),
    )
    assert MBID in client.recording_metadata([MBID])
    assert len(calls) == 2 and no_waiting == [3.0]


def test_429_every_time_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(429, "slow"))
    with pytest.raises(ListenBrainzBusy):
        client.recording_metadata([MBID])
    assert len(calls) == 3


def test_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ListenBrainzClient()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise listenbrainz.requests.ConnectionError("refused")

    monkeypatch.setattr(client._http, "request", boom)
    with pytest.raises(ListenBrainzError):
        client.validate_token("tok")


def test_fake_needs_a_known_token() -> None:
    fake = FakeListenBrainzClient()
    fake.recordings[MBID] = listenbrainz.parse_metadata(MBID, META)
    fake.tokens["tok"] = "user"
    assert fake.lookup_recording("a feat. b", "KICKSTART MY HEART", "tok") is not None
    assert fake.validate_token("tok").user_name == "user"
    with pytest.raises(ListenBrainzAuthError):
        fake.lookup_recording("A", "T", "bad")
