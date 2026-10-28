"""In-memory CatalogSource for tests of code built on catalog clients."""

from __future__ import annotations

from app.clients.catalog import (
    Album,
    Artist,
    CatalogNotFound,
    CatalogSource,
    Edition,
    Track,
)


class FakeCatalog(CatalogSource):
    """Fill the dicts; searches match case-insensitive substrings."""

    def __init__(self, source: str = "fake") -> None:
        self.source = source
        self.tracks: dict[str, Track] = {}
        self.artists: dict[str, Artist] = {}
        self.albums: dict[str, Album] = {}
        self.albums_by_artist: dict[str, list[str]] = {}
        self.editions: dict[str, list[Edition]] = {}  # by album id
        self.tracklists: dict[str, list[Track]] = {}  # by edition id

    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        hits = [
            t
            for t in self.tracks.values()
            if title.lower() in t.title.lower()
            and (artist is None or artist.lower() in t.artist.lower())
        ]
        return hits[:limit]

    def search_artists(self, name: str, limit: int = 25) -> list[Artist]:
        hits = [a for a in self.artists.values() if name.lower() in a.name.lower()]
        return hits[:limit]

    def search_albums(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Album]:
        hits = [
            a
            for a in self.albums.values()
            if title.lower() in a.title.lower()
            and (artist is None or artist.lower() in a.artist.lower())
        ]
        return hits[:limit]

    def get_track(self, track_id: str) -> Track:
        if track_id not in self.tracks:
            raise CatalogNotFound(track_id)
        return self.tracks[track_id]

    def tracks_by_isrc(self, isrc: str) -> list[Track]:
        hits = [t for t in self.tracks.values() if isrc in t.isrcs]
        if not hits:
            raise CatalogNotFound(isrc)
        return hits

    def artist_albums(self, artist_id: str) -> list[Album]:
        return [self.albums[a] for a in self.albums_by_artist.get(artist_id, [])]

    def album_editions(self, album_id: str) -> list[Edition]:
        return self.editions.get(album_id, [])

    def edition_tracks(self, edition_id: str) -> list[Track]:
        if edition_id not in self.tracklists:
            raise CatalogNotFound(edition_id)
        return self.tracklists[edition_id]
