"""Catalog search and browsing (#107).

Searches go to the user's catalog sources in their order; ``source`` in the
answer says which one answered, and browsing stays on that source. Typed
fields are ``title`` and ``artist`` (``name`` for an artist search), as the
catalog service names them.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.clients.catalog import Album, Artist, Edition, Track
from app.dependencies import CoreDep, UserDep
from app.services.catalog import Results

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/tracks")
def search_tracks(
    title: str, user: UserDep, core: CoreDep, artist: str | None = None
) -> Results[Track]:
    return core.catalog.search_tracks(user.id, title, artist)


@router.get("/albums")
def search_albums(
    title: str, user: UserDep, core: CoreDep, artist: str | None = None
) -> Results[Album]:
    return core.catalog.search_albums(user.id, title, artist)


@router.get("/artists")
def search_artists(name: str, user: UserDep, core: CoreDep) -> Results[Artist]:
    return core.catalog.search_artists(user.id, name)


@router.get("/{source}/artists/{artist_id}/albums")
def artist_albums(
    source: str, artist_id: str, user: UserDep, core: CoreDep
) -> list[Album]:
    return core.catalog.artist_albums(user.id, source, artist_id)


@router.get("/{source}/albums/{album_id}/editions")
def album_editions(
    source: str, album_id: str, user: UserDep, core: CoreDep
) -> list[Edition]:
    return core.catalog.album_editions(user.id, source, album_id)


@router.get("/{source}/editions/{edition_id}/tracks")
def edition_tracks(
    source: str, edition_id: str, user: UserDep, core: CoreDep
) -> list[Track]:
    return core.catalog.edition_tracks(user.id, source, edition_id)
