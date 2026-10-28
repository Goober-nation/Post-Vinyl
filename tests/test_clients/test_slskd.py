"""slskd client parsing and error mapping (#78). Shapes copied from real slskd
responses (2026-10 corpus), with usernames and paths replaced."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from app.clients.slskd import SlskdBusy, SlskdClient, SlskdError

RESPONSE = {
    "fileCount": 2,
    "hasFreeUploadSlot": True,
    "lockedFileCount": 1,
    "queueLength": 0,
    "token": 300,
    "uploadSpeed": 5009893,
    "username": "peer-a",
    "files": [
        {
            "bitDepth": 24,
            "code": 1,
            "extension": "flac",
            "filename": "Music\\Artist\\Album (1989)\\05 - Artist - Song.flac",
            "length": 282,
            "sampleRate": 44100,
            "size": 62162312,
            "isLocked": False,
        },
        {
            "code": 1,
            "extension": "lrc",
            "filename": "Music\\Artist\\Album (1989)\\05 - Artist - Song.lrc",
            "size": 2315,
            "isLocked": False,
        },
    ],
    "lockedFiles": [
        {
            "bitRate": 160,
            "code": 1,
            "extension": "",
            "filename": "@@share\\Artist\\Hits\\04 Song.mp3",
            "length": 285,
            "size": 5714702,
            "isLocked": True,
        }
    ],
}


class FakeResp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self) -> Any:
        return self._body


def client_returning(
    monkeypatch: pytest.MonkeyPatch, resp: FakeResp | Exception
) -> tuple[SlskdClient, list[tuple[str, str, dict]]]:
    client = SlskdClient("http://slskd:5030/", "key")
    calls: list[tuple[str, str, dict]] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResp:
        calls.append((method, url, kwargs))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(client._session, "request", request)
    return client, calls


def test_responses_keep_every_file_and_length(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, [RESPONSE]))
    [peer] = client.get_responses("abc")
    assert calls[0][1] == "http://slskd:5030/api/v0/searches/abc/responses"
    assert peer.username == "peer-a" and peer.has_free_slot
    assert [f.length for f in peer.files] == [282, None, 285]
    assert [f.locked for f in peer.files] == [False, False, True]
    assert peer.files[2].bitrate == 160 and peer.files[0].bit_depth == 24


def test_start_search_sends_no_file_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(200, {"id": "s1"}))
    assert client.start_search("song artist", 600, 30) == "s1"
    body = calls[0][2]["json"]
    assert body["searchTimeout"] == 30000 and body["fileLimit"] >= 100000


def test_app_state(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"server": {"isLoggedIn": True}, "distributedNetwork": {"hasParent": False}}
    client, _ = client_returning(monkeypatch, FakeResp(200, body))
    state = client.app_state()
    assert state.logged_in and not state.has_parent


@pytest.mark.parametrize(
    ("resp", "error"),
    [
        (FakeResp(409, "busy"), SlskdBusy),
        (FakeResp(429, "slow down"), SlskdBusy),
        (FakeResp(500, "boom"), SlskdError),
        (requests.ConnectionError("refused"), SlskdError),
    ],
)
def test_errors_map_to_one_type(
    monkeypatch: pytest.MonkeyPatch, resp: FakeResp | Exception, error: type
) -> None:
    client, _ = client_returning(monkeypatch, resp)
    with pytest.raises(error):
        client.get_search("abc")
