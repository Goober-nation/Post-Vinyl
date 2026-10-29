"""In-memory CatalogService for tests of code built on the catalog service."""

from __future__ import annotations

from app.clients.catalog import Album, Artist, Edition, Track
from app.services.catalog import CatalogServiceBase, Results
from app.services.sources import UnknownSource
from app.stores.songs import Song


class FakeCatalogService(CatalogServiceBase):
    """Fill ``tracks`` (by (source, id)); searches match title substrings."""

    def __init__(self) -> None:
        self.tracks: dict[tuple[str, str], Track] = {}
        self.songs: dict[tuple[str, str], Song] = {}

    def search_tracks(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Track]:
        hits = [t for t in self.tracks.values() if title.lower() in t.title.lower()]
        return Results(hits[0].source if hits else None, hits, 0)

    def search_albums(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Album]:
        return Results(None, [], 0)

    def search_artists(self, user_id: int, name: str) -> Results[Artist]:
        return Results(None, [], 0)

    def artist_albums(self, user_id: int, source: str, artist_id: str) -> list[Album]:
        return []

    def album_editions(self, user_id: int, source: str, album_id: str) -> list[Edition]:
        return []

    def edition_tracks(self, user_id: int, source: str, edition_id: str) -> list[Track]:
        return []

    def resolve(self, user_id: int, source: str, track_id: str) -> Song:
        key = (source, track_id)
        if key not in self.tracks:
            raise UnknownSource(f"no track {key}")
        if key not in self.songs:
            t = self.tracks[key]
            self.songs[key] = Song(
                len(self.songs) + 1, t.title, t.artist, None, None, None, None, None
            )
        return self.songs[key]
