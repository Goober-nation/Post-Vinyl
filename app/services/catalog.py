"""Finding songs in the catalogs and turning a pick into a song row (#95).

Typed searches go to the user's catalog sources in their order (see
``services/sources.py``); the **first source that answers** with results is
shown, and errors or empty answers fall through to the next (user,
2026-10-29). Browsing (albums, editions, tracklists) stays on the source the
result came from, since ids are per source.

``resolve`` stores **only the pick**: the song row and the picked source's id
(user, 2026-10-29). Linking to the other sources and choosing a release is
left to tagging. ListenBrainz's name → MBID lookup is in the catalog list for
the request sources (Yandex, Postradio; milestones 9–10) and is skipped here.

Owns ``songs`` and ``song_identity``.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.clients.catalog import (
    Album,
    Artist,
    CatalogError,
    CatalogSource,
    Edition,
    Track,
)
from app.services.sources import SourcePreferences, UnknownSource
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.songs import Song, SongFields, SongsStore

log = logging.getLogger(__name__)

T = TypeVar("T")


class CatalogUnavailable(Exception):
    """Every catalog source the user has failed (or none is enabled)."""


@dataclass(frozen=True)
class Results(Generic[T]):
    source: str | None  # the source that answered; None when nothing was found
    items: list[T]
    origin_id: int  # the catalog_searches row, the origin of requests made from it


class CatalogServiceBase(ABC):
    @abstractmethod
    def search_tracks(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Track]: ...

    @abstractmethod
    def search_albums(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Album]: ...

    @abstractmethod
    def search_artists(self, user_id: int, name: str) -> Results[Artist]: ...

    @abstractmethod
    def artist_albums(
        self, user_id: int, source: str, artist_id: str
    ) -> list[Album]: ...

    @abstractmethod
    def album_editions(
        self, user_id: int, source: str, album_id: str
    ) -> list[Edition]: ...

    @abstractmethod
    def edition_tracks(
        self, user_id: int, source: str, edition_id: str
    ) -> list[Track]: ...

    @abstractmethod
    def resolve(self, user_id: int, source: str, track_id: str) -> Song:
        """The song for a picked track, created on first pick."""


class CatalogService(CatalogServiceBase):
    def __init__(
        self,
        db: Database,
        sources: SourcePreferences,
        catalogs: list[CatalogSource],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._sources = sources
        self._catalogs = {c.source: c for c in catalogs}
        self._clock = clock
        self._songs = SongsStore()
        self._requests = RequestsStore()

    def _user_catalogs(self, user_id: int) -> list[CatalogSource]:
        names = self._sources.for_user(user_id, "catalog")
        return [self._catalogs[n] for n in names if n in self._catalogs]

    def _catalog(self, user_id: int, source: str) -> CatalogSource:
        for catalog in self._user_catalogs(user_id):
            if catalog.source == source:
                return catalog
        raise UnknownSource(f"catalog source {source!r} is not enabled")

    def _search(
        self,
        user_id: int,
        query: str,
        kind: str,
        call: Callable[[CatalogSource], list[T]],
    ) -> Results[T]:
        with self._db.transaction() as conn:
            origin = self._requests.record_catalog_search(
                conn, user_id, query, kind, int(self._clock())
            )
        catalogs = self._user_catalogs(user_id)
        answered = False
        for catalog in catalogs:
            try:
                items = call(catalog)
            except CatalogError as e:
                log.warning("%s %s search failed: %s", catalog.source, kind, e)
                continue
            answered = True
            if items:
                return Results(catalog.source, items, origin.id)
        if catalogs and not answered:
            raise CatalogUnavailable(f"no catalog source answered the {kind} search")
        if not catalogs:
            raise CatalogUnavailable("no catalog source is enabled")
        return Results(None, [], origin.id)

    def search_tracks(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Track]:
        query = f"{artist} – {title}" if artist else title
        return self._search(
            user_id, query, "track", lambda c: c.search_tracks(title, artist)
        )

    def search_albums(
        self, user_id: int, title: str, artist: str | None = None
    ) -> Results[Album]:
        query = f"{artist} – {title}" if artist else title
        return self._search(
            user_id, query, "album", lambda c: c.search_albums(title, artist)
        )

    def search_artists(self, user_id: int, name: str) -> Results[Artist]:
        return self._search(user_id, name, "artist", lambda c: c.search_artists(name))

    def artist_albums(self, user_id: int, source: str, artist_id: str) -> list[Album]:
        return self._catalog(user_id, source).artist_albums(artist_id)

    def album_editions(self, user_id: int, source: str, album_id: str) -> list[Edition]:
        return self._catalog(user_id, source).album_editions(album_id)

    def edition_tracks(self, user_id: int, source: str, edition_id: str) -> list[Track]:
        return self._catalog(user_id, source).edition_tracks(edition_id)

    def resolve(self, user_id: int, source: str, track_id: str) -> Song:
        with self._db.transaction() as conn:
            known = self._songs.find_by_identity(conn, source, track_id)
        if known:
            return known
        track = self._catalog(user_id, source).get_track(track_id)
        fields = SongFields(
            title=track.title,
            artist_name=track.artist,
            duration_s=round(track.duration_s) if track.duration_s else None,
        )
        with self._db.transaction() as conn:
            return self._songs.find_or_create(conn, fields, {source: track.id})
