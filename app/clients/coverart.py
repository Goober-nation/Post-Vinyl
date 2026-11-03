"""Cover Art Archive client (#104): a release's front cover.

Checked live 2026-10-29: ``/release/{mbid}/front-500`` redirects to the image
(200, image/jpeg) and answers 404 when the release has none. The release's
``cover-art-archive.front`` flag on MusicBrainz says beforehand whether it has.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

import requests

from app.clients.catalog import CatalogError

# what thumb() takes: a release or a release group (#127)
_THUMB_PATH = re.compile(r"(release|release-group)/[0-9a-f-]{36}")
_SIZES = (250, 500, 1200)  # the thumbnail sizes the archive keeps


class CoverArtClientBase(ABC):
    @abstractmethod
    def front(self, release_id: str) -> bytes | None:
        """The front cover (500 px), or None when the release has none."""

    @abstractmethod
    def thumb(self, cover_id: str, size: int) -> bytes | None:
        """``release/<mbid>`` or ``release-group/<mbid>``'s front cover at the
        smallest kept size ≥ ``size``; None when it has none (#127)."""


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

    def thumb(self, cover_id: str, size: int) -> bytes | None:
        # checked live 2026-10-31: /release-group/{id}/front-250 and
        # /release/{id}/front-250 redirect to the image; 404 when none
        if not _THUMB_PATH.fullmatch(cover_id):
            return None
        kept = next((s for s in _SIZES if s >= size), _SIZES[-1])
        url = f"{self._base}/{cover_id}/front-{kept}"
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

    def thumb(self, cover_id: str, size: int) -> bytes | None:
        return self.covers.get(cover_id)
