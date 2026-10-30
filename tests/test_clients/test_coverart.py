"""Cover Art Archive client: thumbnails for catalog covers (#127)."""

from __future__ import annotations

import pytest

from app.clients.coverart import CoverArtClient

RG = "48140466-cff6-3222-bd55-63c27e43190d"


class Resp:
    def __init__(
        self, status: int, kind: str = "image/jpeg", content: bytes = b"img"
    ) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {"Content-Type": kind}
        self.content = content


def client_returning(
    monkeypatch: pytest.MonkeyPatch, resp: Resp
) -> tuple[CoverArtClient, list[str]]:
    client = CoverArtClient()
    urls: list[str] = []

    def get(url: str, timeout: float) -> Resp:
        urls.append(url)
        return resp

    monkeypatch.setattr(client._http, "get", get)
    return client, urls


@pytest.mark.parametrize(("size", "kept"), [(80, 250), (300, 500), (600, 1200)])
def test_thumb_asks_the_smallest_kept_size_that_fits(
    monkeypatch: pytest.MonkeyPatch, size: int, kept: int
) -> None:
    client, urls = client_returning(monkeypatch, Resp(200))
    assert client.thumb(f"release-group/{RG}", size) == b"img"
    assert urls == [f"https://coverartarchive.org/release-group/{RG}/front-{kept}"]


def test_thumb_none_when_the_archive_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_returning(monkeypatch, Resp(404, "text/html"))
    assert client.thumb(f"release/{RG}", 120) is None


def test_thumb_refuses_anything_but_a_release_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, urls = client_returning(monkeypatch, Resp(200))
    assert client.thumb("artist/x", 120) is None and client.thumb("../x", 120) is None
    assert urls == []
