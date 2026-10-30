"""Catalog search and browsing (#107).

Searches go to the user's catalog sources in their order; ``source`` in the
answer says which one answered, and browsing stays on that source. Typed
fields are ``title`` and ``artist`` (``name`` for an artist search), as the
catalog service names them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query, Response

from app.clients.catalog import Album, Artist, Edition, Track
from app.dependencies import Core, CoreDep, UserDep
from app.services.catalog import Results

T = TypeVar("T")


def trimmed(results: Results[T], user_id: int, core: Core) -> Results[T]:
    """As many results as the user's setting asks for (#109)."""
    if core.settings is None:
        return results
    n = core.settings.get(user_id)["catalog.results_per_search"]
    return replace(results, items=results.items[:n])


router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/tracks")
def search_tracks(
    title: str, user: UserDep, core: CoreDep, artist: str | None = None
) -> Results[Track]:
    return trimmed(core.catalog.search_tracks(user.id, title, artist), user.id, core)


@router.get("/albums")
def search_albums(
    title: str, user: UserDep, core: CoreDep, artist: str | None = None
) -> Results[Album]:
    return trimmed(core.catalog.search_albums(user.id, title, artist), user.id, core)


@router.get("/artists")
def search_artists(name: str, user: UserDep, core: CoreDep) -> Results[Artist]:
    return trimmed(core.catalog.search_artists(user.id, name), user.id, core)


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


COVER_CACHE = "private, max-age=604800"  # a cover id names one image


@router.get("/{source}/covers/{cover_id:path}", response_class=Response,
            responses={200: {"content": {"image/*": {}}}, 404: {}})  # fmt: skip
def cover(
    source: str, cover_id: str, user: UserDep, core: CoreDep,
    size: int = Query(120, ge=32, le=600),
) -> Response:  # fmt: skip
    """A catalog result's cover by its ``cover_id``, from the source's own image
    lookup (#127); never stored; 404 when there is none."""
    art = core.catalog.cover(user.id, source, cover_id, size)
    if art is None:
        raise HTTPException(404, "no cover for this")
    kind = (
        "image/png" if art.startswith(b"\x89PNG") else "image/jpeg"
    )  # the archive keeps both
    return Response(art, media_type=kind, headers={"Cache-Control": COVER_CACHE})
