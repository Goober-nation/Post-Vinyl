"""Cover Art Archive client (#104): a release's front cover.

Checked live 2026-10-29: ``/release/{mbid}/front-500`` redirects to the image
(200, image/jpeg) and answers 404 when the release has none. The release's
``cover-art-archive.front`` flag on MusicBrainz says beforehand whether it has.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import requests

from app.clients.catalog import CatalogError


class CoverArtClientBase(ABC):
    @abstractmethod
    def front(self, release_id: str) -> bytes | None:
        """The front cover (500 px), or None when the release has none."""


class CoverArtClient(CoverArtClientBase):
    def __init__(
        self, base_url: str = "https://coverartarchive.org", timeout_s: float = 30
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._http = requests.Session()

    def front(self, release_id: str) -> bytes | None:
        url = f"{self._base}/release/{release_id}/front-500"
        try:
            resp = self._http.get(url, timeout=self._timeout)
        except requests.RequestException as e:
            raise CatalogError(f"GET {url}: {e}") from e
        if resp.status_code == 404:
            return None
        if not resp.ok or not resp.headers.get("Content-Type", "").startswith("image/"):
            raise CatalogError(f"GET {url}: HTTP {resp.status_code}")
        return resp.content


class FakeCoverArt(CoverArtClientBase):
    def __init__(self) -> None:
        self.covers: dict[str, bytes] = {}

    def front(self, release_id: str) -> bytes | None:
        return self.covers.get(release_id)
