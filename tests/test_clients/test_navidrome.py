"""Navidrome client: Subsonic error mapping, token refresh, exact path match
(#80). Shapes copied from Navidrome 0.64.2 responses, values replaced."""

from __future__ import annotations

from typing import Any

import pytest

from app.clients.navidrome import (
    NavidromeAuthError,
    NavidromeClient,
    NavidromeError,
    NavidromeForbidden,
    NavidromeNotFound,
    Session,
)
from app.clients.navidrome_fake import FakeNavidromeClient


class FakeResp:
    def __init__(
        self,
        status: int,
        body: Any = None,
        headers: dict | None = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status
        self.content = content
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> Any:
        return self._body


def client_returning(
    monkeypatch: pytest.MonkeyPatch, resp: FakeResp
) -> tuple[NavidromeClient, list[tuple[str, str, dict]]]:
    client = NavidromeClient("http://navidrome:4533/")
    calls: list[tuple[str, str, dict]] = []

    def request(method: str, url: str, **kwargs: Any) -> FakeResp:
        calls.append((method, url, kwargs))
        return resp

    monkeypatch.setattr(client._http, "request", request)
    return client, calls


def session() -> Session:
    return Session("admin", True, "jwt-1", "tok", "salt")


def subsonic_failed(code: int) -> dict:
    return {
        "subsonic-response": {
            "status": "failed",
            "error": {"code": code, "message": "x"},
        }
    }


def song(path: str, song_id: str = "s1") -> dict:
    return {
        "id": song_id,
        "libraryPath": "/music",
        "path": path,
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "duration": 282.4,
        "size": 1000,
        "suffix": "flac",
        "missing": False,
    }


def test_login_keeps_subsonic_token(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "id": "u1",
        "username": "admin",
        "isAdmin": True,
        "token": "jwt-1",
        "subsonicToken": "tok",
        "subsonicSalt": "salt",
    }
    client, calls = client_returning(monkeypatch, FakeResp(200, body))
    s = client.login("admin", "pw")
    assert calls[0][1] == "http://navidrome:4533/auth/login"
    assert (s.jwt, s.subsonic_token, s.subsonic_salt, s.is_admin) == (
        "jwt-1",
        "tok",
        "salt",
        True,
    )


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (40, NavidromeAuthError),
        (50, NavidromeForbidden),
        (70, NavidromeNotFound),
        (0, NavidromeError),
    ],
)
def test_subsonic_failed_with_http_200_raises(
    monkeypatch: pytest.MonkeyPatch, code: int, error: type
) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(200, subsonic_failed(code)))
    with pytest.raises(error):
        client.scan_status(session())


def test_subsonic_sends_token_not_password(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = {
        "subsonic-response": {
            "status": "ok",
            "scanStatus": {"scanning": True, "lastScan": "0001-01-01T00:00:00Z"},
        }
    }
    client, calls = client_returning(monkeypatch, FakeResp(200, ok))
    status = client.start_scan(session(), full=True)
    params = calls[0][2]["params"]
    assert params["t"] == "tok" and params["s"] == "salt" and "p" not in params
    assert params["fullScan"] == "true"
    assert status.scanning and status.last_scan is None and status.error == ""


def test_native_call_keeps_refreshed_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = FakeResp(200, song("A/B/01.flac"), {"X-ND-Authorization": "jwt-2"})
    client, calls = client_returning(monkeypatch, resp)
    s = session()
    found = client.get_song(s, "s1")
    assert calls[0][2]["headers"]["X-ND-Authorization"] == "Bearer jwt-1"
    assert s.jwt == "jwt-2" and found.duration == 282.4


def test_find_by_path_ignores_prefix_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [song("A/B/01.flac.bak", "s0"), song("A/B/01.flac", "s1")]
    client, _ = client_returning(monkeypatch, FakeResp(200, rows))
    found = client.find_song_by_path(session(), "A/B/01.flac")
    assert found is not None and found.id == "s1"


def test_find_by_path_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(200, [song("A/B/01.flac.bak")]))
    assert client.find_song_by_path(session(), "A/B/01.flac") is None


def test_expired_session_is_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, FakeResp(401, "unauthorized"))
    with pytest.raises(NavidromeAuthError):
        client.get_song(session(), "s1")


def test_fake_first_admin_only_once() -> None:
    fake = FakeNavidromeClient()
    fake.create_admin("admin", "pw")
    with pytest.raises(NavidromeForbidden):
        fake.create_admin("other", "pw")
    with pytest.raises(NavidromeAuthError):
        fake.login("admin", "wrong")


def test_cover_art_is_the_image_navidrome_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = FakeResp(200, None, {"Content-Type": "image/jpeg"}, b"\xff\xd8img")
    client, calls = client_returning(monkeypatch, resp)
    cover = client.cover_art(session(), "s1", 80)
    assert (cover.data, cover.content_type) == (b"\xff\xd8img", "image/jpeg")
    params = calls[0][2]["params"]
    assert (params["id"], params["size"], params["t"]) == ("s1", 80, "tok")


def test_cover_art_unknown_id_is_a_json_error_with_http_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0.64.2, live 2026-10-31: no artwork -> 200, application/json, code 70
    resp = FakeResp(200, subsonic_failed(70), {"Content-Type": "application/json"})
    client, _ = client_returning(monkeypatch, resp)
    with pytest.raises(NavidromeNotFound):
        client.cover_art(session(), "nope", 80)
