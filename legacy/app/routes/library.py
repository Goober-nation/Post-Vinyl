"""
Library API routes.

Endpoints:
    POST /api/library/scan              -> Trigger library scan (200)
    GET  /api/playlists                  -> List all playlists
    POST /api/playlists/{playlist_id}/sync -> Sync playlist from Navidrome

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_library_service
from app.logging_config import get_logger
from app.services.library import LibraryService, PlaylistDetail, PlaylistInfo, Song

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["library"])

LibraryServiceDep = Annotated[LibraryService, Depends(get_library_service)]


# ============================================================================
# Request/Response models
# ============================================================================


class ScanResponse(BaseModel):
    """Response shape for POST /api/library/scan."""

    scan_triggered: bool


class PlaylistResponse(BaseModel):
    """Response shape for a playlist summary."""

    playlist_id: str
    name: str
    song_count: int
    duration: int
    public: bool
    owner: str | None
    comment: str | None
    created: str | None
    changed: str | None


class SongResponse(BaseModel):
    """Response shape for a song in a playlist."""

    song_id: str
    title: str
    artist: str
    album: str
    duration: int
    size: int
    bitrate: int | None
    track_number: int | None
    year: int | None
    genre: str | None
    rating: int
    starred: bool
    mbid: str | None


class PlaylistSyncResponse(BaseModel):
    """Response shape for POST /api/playlists/{id}/sync."""

    playlist_id: str
    name: str
    song_count: int
    songs: list[SongResponse]


# ============================================================================
# Serialization helpers
# ============================================================================


def _playlist_to_dict(p: PlaylistInfo) -> dict:
    """Serialize PlaylistInfo to dict."""
    return {
        "playlist_id": p.playlist_id,
        "name": p.name,
        "song_count": p.song_count,
        "duration": p.duration,
        "public": p.public,
        "owner": p.owner,
        "comment": p.comment,
        "created": p.created,
        "changed": p.changed,
    }


def _song_to_dict(song: Song) -> dict:
    """Serialize Song to dict."""
    return {
        "song_id": song.song_id,
        "title": song.title,
        "artist": song.artist,
        "album": song.album,
        "duration": song.duration,
        "size": song.size,
        "bitrate": song.bitrate,
        "track_number": song.track_number,
        "year": song.year,
        "genre": song.genre,
        "rating": song.rating,
        "starred": song.starred,
        "mbid": song.mbid,
    }


def _detail_to_dict(d: PlaylistDetail) -> dict:
    """Serialize PlaylistDetail to dict."""
    return {
        "playlist_id": d.playlist_id,
        "name": d.name,
        "song_count": len(d.songs),
        "songs": [_song_to_dict(s) for s in d.songs],
    }


# ============================================================================
# Endpoints
# ============================================================================


@router.post("/library/scan", response_model=ScanResponse)
def trigger_scan(
    library_service: LibraryServiceDep,
) -> dict:
    """Trigger a Navidrome library scan."""
    logger.info("POST /api/library/scan")
    triggered = library_service.trigger_scan()
    return {"scan_triggered": triggered}


@router.get("/playlists", response_model=list[PlaylistResponse])
def list_playlists(
    library_service: LibraryServiceDep,
) -> list[dict]:
    """List all playlists in Navidrome library."""
    playlists = library_service.list_playlists()
    return [_playlist_to_dict(p) for p in playlists]


@router.post("/playlists/{playlist_id}/sync", response_model=PlaylistSyncResponse)
def sync_playlist(
    playlist_id: str,
    library_service: LibraryServiceDep,
) -> dict:
    """
    Sync playlist contents from Navidrome.

    Re-pulls the current playlist contents from the Navidrome API.
    """
    logger.info(f"POST /api/playlists/{playlist_id}/sync")
    return _detail_to_dict(library_service.get_playlist_detail(playlist_id))
