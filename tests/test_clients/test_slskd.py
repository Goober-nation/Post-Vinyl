"""slskd client parsing and error mapping (#78). Shapes copied from real slskd
responses (2026-10 corpus), with usernames and paths replaced."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from app.clients.slskd import SlskdBusy, SlskdClient, SlskdError, SlskdNotFound

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
        (FakeResp(404, "gone"), SlskdNotFound),
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


DOWNLOADS = [
    {
        "username": "peer a",
        "directories": [
            {
                "directory": "Music\\Artist\\Album",
                "fileCount": 2,
                "files": [
                    {
                        "id": "t1",
                        "username": "peer a",
                        "filename": "Music\\Artist\\Album\\01 Song.flac",
                        "state": "InProgress",
                        "size": 1000,
                        "bytesTransferred": 400,
                        "averageSpeed": 1234.56,
                    },
                    {
                        "id": "t2",
                        "username": "peer a",
                        "filename": "Music\\Artist\\Album\\02 Other.flac",
                        "state": "Queued, Remotely",
                        "size": 2000,
                        "bytesTransferred": 0,
                        "averageSpeed": 0,
                    },
                ],
            }
        ],
    }
]


def test_list_downloads_flattens_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(200, DOWNLOADS))
    t1, t2 = client.list_downloads()
    assert (t1.id, t1.username, t1.state, t1.bytes_transferred) == (
        "t1",
        "peer a",
        "InProgress",
        400,
    )
    assert t1.average_speed == 1234 and t2.state == "Queued, Remotely"


def batch_body(failures: list[dict]) -> dict:
    queued = [] if failures else [DOWNLOADS[0]["directories"][0]["files"][1]]
    return {"batch": {"id": "b1", "transfers": queued}, "failures": failures}


def test_queue_download_returns_transfer_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(201, batch_body([])))
    result = client.queue_download("peer a", [("Music\\01 Song.flac", 1000)], "s1")
    method, url, kwargs = calls[0]
    assert (method, url) == (
        "POST",
        "http://slskd:5030/api/v0/transfers/downloads/batches",
    )
    assert kwargs["json"]["files"] == [
        {"filename": "Music\\01 Song.flac", "size": 1000}
    ]
    assert kwargs["json"]["searchId"] == "s1" and kwargs["timeout"] >= 45
    assert [t.id for t in result.transfers] == ["t2"] and result.failures == {}


def test_queue_download_all_failed_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # slskd answers 200 (not an error status) when every file failed to queue
    body = batch_body([{"filename": "a.flac", "message": "already in progress"}])
    client, _ = client_returning(monkeypatch, FakeResp(200, body))
    result = client.queue_download("peer a", [("a.flac", 10)])
    assert result.transfers == [] and result.failures == {
        "a.flac": "already in progress"
    }


def test_rejected_transfer_keeps_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "id": "t9",
        "filename": "a.flac",
        "state": "Completed, Rejected",
        "size": 10,
        "bytesTransferred": 0,
        "averageSpeed": 0,
        "exception": "Transfer rejected: Banned",
    }
    client, _ = client_returning(monkeypatch, FakeResp(200, raw))
    transfer = client.get_download("peer a", "t9")
    assert transfer.username == "peer a"
    assert transfer.exception == "Transfer rejected: Banned"


def test_cancel_download_forget(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = client_returning(monkeypatch, FakeResp(204, None))
    client.cancel_download("peer a", "t1", forget=True)
    _, url, kwargs = calls[0]
    assert url.endswith("/transfers/downloads/peer%20a/t1")
    assert kwargs["params"] == {"remove": "true"}


def test_fake_tracks_transfers() -> None:
    from app.clients.slskd_fake import FakeSlskdClient

    fake = FakeSlskdClient()
    fake.queue_download("peer a", [("a.flac", 10)])
    [t] = fake.list_downloads()
    fake.cancel_download("peer a", t.id)
    assert fake.get_download("peer a", t.id).state == "Completed, Cancelled"
    fake.cancel_download("peer a", t.id, forget=True)
    assert fake.list_downloads() == []
