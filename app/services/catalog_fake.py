"""In-memory CatalogService that behaves like the real one (#95).

Fill ``catalogs`` with one FakeCatalog (or any CatalogSource) per source and
set ``enabled`` to the user's sources in order (one list for every user).
Same rules as ``services/catalog.py``: the first enabled source that answers
with results wins, errors fall through, no enabled source or all failing
raises CatalogUnavailable, each distinct (query, kind) gets one origin id,
typed fields are trimmed and an empty one refused, track hits are ordered by
``core/catalog_order.py``,
browse and resolve refuse a source that isn't enabled, and a known pick is
returned without a catalog call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.clients.catalog import (
    Album,
    Artist,
    CatalogError,
    CatalogSource,
    Edition,
    Track,
)
from app.services.catalog import (
    NO_SOURCES,
    SEARCH_LIMIT,
    CatalogServiceBase,
    CatalogUnavailable,
    Results,
    order_tracks,
    search_query,
)
from app.services.sources import UnknownSource
from app.stores.songs import Song

T = TypeVar("T")


class FakeCatalogService(CatalogServiceBase):
    def __init__(self) -> None:
        self.catalogs: dict[str, CatalogSource] = {}
        self.enabled: list[str] = []
        self.origins: dict[tuple[int, str, str], int] = {}  # (user, query, kind) → id
        self.songs: dict[tuple[str, str], Song] = {}  # (source, id) → song

    def _catalog(self, source: str) -> CatalogSource:
        if source not in self.enabled or source not in self.catalogs:
            raise UnknownSource(f"catalog source {source!r} is not enabled")
        return self.catalogs[source]

    def _search(
        self,
        user_id: int,
        query: str,
        kind: str,
        call: Callable[[CatalogSource], list[T]],
    ) -> Results[T]:
        catalogs = [self.catalogs[s] for s in self.enabled if s in self.catalogs]
        if not catalogs:
            raise CatalogUnavailable(NO_SOURCES)
        key = (user_id, query.lower(), kind)
        origin = self.origins.setdefault(key, len(self.origins) + 1)
        answered = False
        for catalog in catalogs:
            try:
                items = call(catalog)
            except CatalogError:
                continue
            answered = True
            if items:
                return Results(catalog.source, items, origin)
        if not answered:
            raise CatalogUnavailable(f"no catalog source answered the {kind} search")
        return Results(None, [], origin)

    def search_tracks(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Track]:
        title, artist, query = search_query(title, artist)
        return self._search(
            user_id,
            query,
            "track",
            lambda c: order_tracks(
                title, c.search_tracks(title, artist, limit=SEARCH_LIMIT)
            ),
        )

    def search_albums(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Album]:
        title, artist, query = search_query(title, artist)
        return self._search(
            user_id, query, "album", lambda c: c.search_albums(title, artist)
        )

    def search_artists(self, user_id: int, name: str) -> Results[Artist]:
        name, _, query = search_query(name, None)
        return self._search(user_id, query, "artist", lambda c: c.search_artists(name))

    def artist_albums(self, user_id: int, source: str, artist_id: str) -> list[Album]:
        return self._catalog(source).artist_albums(artist_id)

    def album_editions(self, user_id: int, source: str, album_id: str) -> list[Edition]:
        return self._catalog(source).album_editions(album_id)

    def edition_tracks(self, user_id: int, source: str, edition_id: str) -> list[Track]:
        return self._catalog(source).edition_tracks(edition_id)

    def resolve(self, user_id: int, source: str, track_id: str) -> Song:
        if (source, track_id) in self.songs:
            return self.songs[(source, track_id)]
        t = self._catalog(source).get_track(track_id)
        duration = round(t.duration_s) if t.duration_s else None
        song = Song(
            len(self.songs) + 1, t.title, t.artist, None, None, None, None, duration
        )
        self.songs[(source, t.id)] = song
        return song
