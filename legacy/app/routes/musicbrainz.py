"""
MusicBrainz API routes — search & discovery (Phase 6.8).

Endpoints:
    GET  /api/musicbrainz/search/recordings?title=&artist=&limit=  -> recordings
    GET  /api/musicbrainz/search/albums?title=&artist=&limit=      -> release groups
    GET  /api/musicbrainz/search/artists?name=&limit=              -> artists
    GET  /api/musicbrainz/artists/{mbid}/albums?limit=             -> release groups
    GET  /api/musicbrainz/albums/{mbid}/tracks                     -> recordings
    POST /api/musicbrainz/recordings/{mbid}/download               -> start resolve job (202)
    POST /api/musicbrainz/albums/{mbid}/download                   -> start resolve job (202)

The frontend is built against these exact field names — do not rename them.
MB transport/rate-limit errors propagate to the global handler (503); a
malformed/empty MBID raises `MusicBrainzNotFoundError` (404).
"""

import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.config import Config
from app.db.database import Database
from app.dependencies import (
    get_config,
    get_db_or_none,
    get_download_service,
    get_event_hub,
    get_musicbrainz_service,
    get_search_service,
)
from app.exceptions import MusicBrainzNotFoundError
from app.logging_config import get_logger
from app.services.interfaces.download import DownloadService
from app.services.interfaces.musicbrainz import (
    MBArtist,
    MBRecording,
    MBReleaseGroup,
    MusicBrainzService,
)
from app.services.interfaces.search import SearchService
from app.sse import EventHub
from app.workers.mb_resolver import start_resolve_job

logger = get_logger(__name__)

router = APIRouter(prefix="/api/musicbrainz", tags=["musicbrainz"])

MusicBrainzServiceDep = Annotated[MusicBrainzService, Depends(get_musicbrainz_service)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
DownloadServiceDep = Annotated[DownloadService, Depends(get_download_service)]
ConfigDep = Annotated[Config, Depends(get_config)]
DbDep = Annotated[Database | None, Depends(get_db_or_none)]
EventHubDep = Annotated[EventHub, Depends(get_event_hub)]


# ============================================================================
# Response models
# ============================================================================


class RecordingResult(BaseModel):
    mbid: str
    title: str
    artist: str
    artist_credit: str
    album: str | None
    year: int | None
    length_ms: int | None
    score: int | None
    #: The release-group MBID of the recording's canonical release, for the
    #: frontend to fetch cover art from the Cover Art Archive. None when the
    #: recording has no release with a group.
    cover_mbid: str | None = None
    #: Number of releases containing this recording. A catalog-size proxy, not
    #: a listener/popularity count.
    release_count: int = 0


class RecordingSearchResponse(BaseModel):
    results: list[RecordingResult]


class AlbumResult(BaseModel):
    mbid: str
    title: str
    artist: str
    primary_type: str | None
    year: int | None
    #: Number of releases/pressings in the release group. A catalog-size
    #: proxy, not a listener/popularity count.
    release_count: int = 0


class AlbumSearchResponse(BaseModel):
    results: list[AlbumResult]


class ArtistResult(BaseModel):
    mbid: str
    name: str
    sort_name: str | None
    disambiguation: str | None
    score: int | None


class ArtistSearchResponse(BaseModel):
    results: list[ArtistResult]


class UnifiedSearchResponse(BaseModel):
    artist: ArtistResult | None
    albums: list[AlbumResult]
    recordings: list[RecordingResult]


class TrackResult(BaseModel):
    mbid: str
    title: str
    artist: str
    length_ms: int | None


class TrackListResponse(BaseModel):
    results: list[TrackResult]


class DownloadJobResponse(BaseModel):
    started: bool
    job_id: str


# ============================================================================
# Serialization helpers
# ============================================================================


def _recording_to_dict(rec: MBRecording) -> dict:
    """Serialize an MBRecording to the search-result shape.

    `artist` is the primary/album artist (no featuring clause); `album`/
    `year` come from the best (canonical studio) release, or None.
    """
    best = rec.best_release
    return {
        "mbid": rec.mbid,
        "title": rec.title,
        "artist": rec.artist,
        "artist_credit": rec.artist_credit,
        "album": best.title if best else None,
        "year": best.year if best else None,
        "length_ms": rec.length_ms,
        "score": rec.score,
        "cover_mbid": best.release_group_mbid if best else None,
        "release_count": len(rec.releases),
    }


def _release_group_to_dict(group: MBReleaseGroup) -> dict:
    return {
        "mbid": group.mbid,
        "title": group.title,
        "artist": group.artist,
        "primary_type": group.primary_type,
        "year": group.year,
        "release_count": group.release_count or 0,
    }


def _artist_to_dict(artist: MBArtist) -> dict:
    return {
        "mbid": artist.mbid,
        "name": artist.name,
        "sort_name": artist.sort_name,
        "disambiguation": artist.disambiguation,
        "score": artist.score,
    }


def _track_to_dict(rec: MBRecording) -> dict:
    return {
        "mbid": rec.mbid,
        "title": rec.title,
        "artist": rec.artist,
        "length_ms": rec.length_ms,
    }


def _require_valid_mbid(mbid: str) -> None:
    """Reject an empty or malformed MBID with a 404 (MusicBrainzNotFoundError)."""
    try:
        uuid.UUID(mbid)
    except (ValueError, AttributeError):
        raise MusicBrainzNotFoundError("mbid", mbid)


def _official_only(config: Config) -> bool:
    """Whether the MB search should be restricted to official releases.

    Reads the `musicbrainz.search_official_only` knob (default True): search
    results are filtered to official albums/singles/EPs, hiding mixtapes,
    bootlegs, live albums, compilations, DJ-mixes, demos and remixes.
    """
    return bool(
        getattr(getattr(config, "musicbrainz", None), "search_official_only", True)
    )


def _normalise_search_text(value: str) -> str:
    """Make title/artist comparisons insensitive to case and punctuation."""
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _query_variants(query: str, artist: str | None) -> list[tuple[str, str | None]]:
    """Return the broad query plus every title-first artist split.

    The UI has one field, so ``damn kendrick lamar`` must be able to become
    ``title=damn, artist=kendrick lamar`` without requiring a delimiter. The
    broad query remains first so an ambiguous input still has a fallback.
    """
    query = query.strip()
    artist = (artist or "").strip() or None
    if artist:
        return [(query, artist)]

    words = query.split()
    variants: list[tuple[str, str | None]] = [(query, None)]
    # Avoid turning an unusually long free-form sentence into a burst of
    # MusicBrainz requests. Normal music searches are comfortably below this.
    if 1 < len(words) <= 8:
        seen = set(variants)
        preferred = [
            (1, False),
            (len(words) - 1, True),
            (len(words) - 1, False),
            (1, True),
        ]
        remaining = [
            (split_at, reverse)
            for split_at in range(2, len(words) - 1)
            for reverse in (False, True)
        ]
        for split_at, reverse in [*preferred, *remaining]:
            forward = (" ".join(words[:split_at]), " ".join(words[split_at:]))
            variant = (forward[1], forward[0]) if reverse else forward
            if variant not in seen:
                variants.append(variant)
                seen.add(variant)
    return variants


def _candidate_match_key(
    query_title: str,
    query_artist: str | None,
    candidate_title: str,
    candidate_artist: str,
    score: int | None,
) -> tuple[int, int, int, int, int]:
    """Rank an item for one title/artist interpretation."""
    title = _normalise_search_text(query_title)
    artist = _normalise_search_text(query_artist or "")
    result_title = _normalise_search_text(candidate_title)
    result_artist = _normalise_search_text(candidate_artist)
    return (
        int(bool(title) and title == result_title),
        int(bool(artist) and artist == result_artist),
        int(bool(title) and title in result_title),
        int(bool(artist) and artist in result_artist),
        score or 0,
    )


def _adaptive_recordings(
    service: MusicBrainzService,
    query: str,
    artist: str | None,
    limit: int,
    official_only: bool,
) -> list[MBRecording]:
    best: dict[str, tuple[tuple[int, int, int, int, int], MBRecording]] = {}
    for title, artist_part in _query_variants(query, artist):
        exact_match = False
        for recording in service.search_recording(
            title,
            artist=artist_part,
            limit=limit,
            official_only=official_only,
        ):
            key = _candidate_match_key(
                title,
                artist_part,
                recording.title,
                recording.artist,
                recording.score,
            )
            previous = best.get(recording.mbid)
            if previous is None or key > previous[0]:
                best[recording.mbid] = (key, recording)
            exact_match = exact_match or (
                (key[0] == 1 and key[1] == 1) or (key[1] == 1 and key[2] == 1)
            )
        if exact_match:
            break
    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)
    return [recording for _, recording in ranked[:limit]]


def _adaptive_albums(
    service: MusicBrainzService,
    query: str,
    artist: str | None,
    limit: int,
    official_only: bool,
) -> list[MBReleaseGroup]:
    best: dict[str, tuple[tuple[int, int, int, int, int], MBReleaseGroup]] = {}
    for title, artist_part in _query_variants(query, artist):
        exact_match = False
        for group in service.search_release_group(
            title,
            artist=artist_part,
            limit=limit,
            official_only=official_only,
        ):
            key = _candidate_match_key(
                title,
                artist_part,
                group.title,
                group.artist,
                group.score,
            )
            previous = best.get(group.mbid)
            if previous is None or key > previous[0]:
                best[group.mbid] = (key, group)
            exact_match = exact_match or (
                (key[0] == 1 and key[1] == 1) or (key[1] == 1 and key[2] == 1)
            )
        if exact_match:
            break
    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)
    return [group for _, group in ranked[:limit]]


def _prominence_key(query: str, title: str, artist: str, count: int, score: int | None):
    """Sort by catalog prominence without burying an exact match."""
    query_text = _normalise_search_text(query)
    query_words = set(query_text.split())
    title_text = _normalise_search_text(title)
    artist_words = _normalise_search_text(artist).split()
    return (
        int(bool(title_text) and title_text in query_text),
        int(bool(artist_words) and all(word in query_words for word in artist_words)),
        count,
        score or 0,
    )


def _sort_recordings(
    recordings: list[MBRecording], query: str, sort: Literal["relevance", "prominence"]
) -> list[MBRecording]:
    if sort == "relevance":
        return recordings
    return sorted(
        recordings,
        key=lambda recording: _prominence_key(
            query,
            recording.title,
            recording.artist,
            len(recording.releases),
            recording.score,
        ),
        reverse=True,
    )


def _sort_albums(
    groups: list[MBReleaseGroup], query: str, sort: Literal["relevance", "prominence"]
) -> list[MBReleaseGroup]:
    if sort == "relevance":
        return groups
    return sorted(
        groups,
        key=lambda group: _prominence_key(
            query,
            group.title,
            group.artist,
            group.release_count or 0,
            group.score,
        ),
        reverse=True,
    )


def _artist_from_results(
    query: str,
    groups: list[MBReleaseGroup],
    recordings: list[MBRecording],
) -> str | None:
    """Find the artist whose name is represented in the one-field query."""
    query_words = set(_normalise_search_text(query).split())
    candidates: dict[str, tuple[int, int, int, str]] = {}
    for title, artist, score in [
        *((group.title, group.artist, group.score) for group in groups),
        *(
            (recording.title, recording.artist, recording.score)
            for recording in recordings
        ),
    ]:
        artist_words = _normalise_search_text(artist).split()
        if not artist_words or not all(word in query_words for word in artist_words):
            continue
        title_match = int(
            bool(_normalise_search_text(title))
            and _normalise_search_text(title) in _normalise_search_text(query)
        )
        key = (title_match, len(artist_words), score or 0, artist)
        previous = candidates.get(artist)
        if previous is None or key > previous:
            candidates[artist] = key
    if not candidates:
        return None
    return max(candidates.items(), key=lambda item: item[1])[0]


def _same_artist(value: str, artist: str) -> bool:
    return _normalise_search_text(value) == _normalise_search_text(artist)


def _is_exact_query_match(query: str, title: str, artist: str) -> bool:
    query_text = _normalise_search_text(query)
    query_words = set(query_text.split())
    title_text = _normalise_search_text(title)
    artist_words = _normalise_search_text(artist).split()
    title_matches = bool(title_text) and title_text in query_text
    artist_matches = bool(artist_words) and all(
        word in query_words for word in artist_words
    )
    return title_matches and (artist_matches or len(query_words) == 1)


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/search", response_model=UnifiedSearchResponse)
def search_musicbrainz(
    musicbrainz_service: MusicBrainzServiceDep,
    config: ConfigDep,
    query: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    sort: Literal["relevance", "prominence"] = Query("relevance"),
) -> dict:
    """Search artists, albums, and recordings from one free-form query.

    The result is intentionally grouped for the frontend: a matched artist
    first, then albums and recordings associated with the query. An artist is
    inferred from the best title/artist match; a plain artist query falls back
    to MusicBrainz's direct artist search.
    """
    logger.info("GET /api/musicbrainz/search: query=%r sort=%s", query, sort)
    official_only = _official_only(config)
    groups = _adaptive_albums(musicbrainz_service, query, None, limit, official_only)
    group_artist = _artist_from_results(query, groups, [])
    exact_album = any(
        _is_exact_query_match(query, group.title, group.artist) for group in groups
    )
    recordings = (
        []
        if exact_album
        else _adaptive_recordings(
            musicbrainz_service, query, None, limit, official_only
        )
    )
    artist_name = _artist_from_results(query, groups, recordings) or group_artist
    artist_result = None
    if artist_name:
        artist_matches = musicbrainz_service.search_artist(artist_name, limit=5)
        artist_result = next(
            (
                artist
                for artist in artist_matches
                if _same_artist(artist.name, artist_name)
            ),
            artist_matches[0] if artist_matches else None,
        )
        groups = [
            group for group in groups if _same_artist(group.artist, artist_name)
        ] or groups
        recordings = [
            recording
            for recording in recordings
            if _same_artist(recording.artist, artist_name)
        ] or recordings
    else:
        artist_matches = musicbrainz_service.search_artist(query, limit=5)
        artist_result = next(
            (
                artist
                for artist in artist_matches
                if _normalise_search_text(artist.name) == _normalise_search_text(query)
            ),
            artist_matches[0]
            if not groups and not recordings and artist_matches
            else None,
        )

    groups = _sort_albums(groups, query, sort)
    recordings = _sort_recordings(recordings, query, sort)
    return {
        "artist": _artist_to_dict(artist_result) if artist_result else None,
        "albums": [_release_group_to_dict(group) for group in groups],
        "recordings": [_recording_to_dict(recording) for recording in recordings],
    }


@router.get("/search/recordings", response_model=RecordingSearchResponse)
def search_recordings(
    musicbrainz_service: MusicBrainzServiceDep,
    config: ConfigDep,
    title: str = Query(..., min_length=1),
    artist: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    sort: Literal["relevance", "prominence"] = Query("relevance"),
) -> dict:
    """Search recordings from one adaptive title/artist query.

    Official-only by default (`musicbrainz.search_official_only`): results are
    filtered to recordings with at least one official release. With no
    separate artist parameter, the query is tried as a broad title and as
    every title-first split, e.g. ``damn kendrick lamar``.
    """
    logger.info(
        "GET /api/musicbrainz/search/recordings: query=%r artist=%r sort=%s",
        title,
        artist,
        sort,
    )
    recordings = _adaptive_recordings(
        musicbrainz_service,
        title,
        artist,
        limit,
        _official_only(config),
    )
    recordings = _sort_recordings(recordings, title, sort)
    return {"results": [_recording_to_dict(r) for r in recordings]}


@router.get("/search/albums", response_model=AlbumSearchResponse)
def search_albums(
    musicbrainz_service: MusicBrainzServiceDep,
    config: ConfigDep,
    title: str = Query(..., min_length=1),
    artist: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    sort: Literal["relevance", "prominence"] = Query("relevance"),
) -> dict:
    """Search release groups from one adaptive title/artist query."""
    logger.info(
        "GET /api/musicbrainz/search/albums: query=%r artist=%r sort=%s",
        title,
        artist,
        sort,
    )
    groups = _adaptive_albums(
        musicbrainz_service,
        title,
        artist,
        limit,
        _official_only(config),
    )
    groups = _sort_albums(groups, title, sort)
    return {"results": [_release_group_to_dict(g) for g in groups]}


@router.get("/search/artists", response_model=ArtistSearchResponse)
def search_artists(
    musicbrainz_service: MusicBrainzServiceDep,
    name: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Search artists by name."""
    logger.info("GET /api/musicbrainz/search/artists: name=%r", name)
    artists = musicbrainz_service.search_artist(name, limit=limit)
    return {"results": [_artist_to_dict(a) for a in artists]}


@router.get("/artists/{mbid}/albums", response_model=AlbumSearchResponse)
def artist_albums(
    mbid: str,
    musicbrainz_service: MusicBrainzServiceDep,
    config: ConfigDep,
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    """An artist's release groups (discography)."""
    logger.info("GET /api/musicbrainz/artists/%s/albums", mbid)
    groups = musicbrainz_service.browse_artist_release_groups(
        mbid, limit=limit, official_only=_official_only(config)
    )
    return {"results": [_release_group_to_dict(g) for g in groups]}


@router.get("/albums/{mbid}/tracks", response_model=TrackListResponse)
def album_tracks(
    mbid: str,
    musicbrainz_service: MusicBrainzServiceDep,
) -> dict:
    """The ordered track list of a release group's canonical release."""
    logger.info("GET /api/musicbrainz/albums/%s/tracks", mbid)
    recordings = musicbrainz_service.lookup_release_group_tracks(mbid)
    return {"results": [_track_to_dict(r) for r in recordings]}


@router.post(
    "/recordings/{mbid}/download",
    response_model=DownloadJobResponse,
    status_code=202,
)
def download_recording(
    mbid: str,
    musicbrainz_service: MusicBrainzServiceDep,
    search_service: SearchServiceDep,
    download_service: DownloadServiceDep,
    config: ConfigDep,
    db: DbDep,
    event_hub: EventHubDep,
) -> dict:
    """Start a resolve-and-queue job for one recording."""
    logger.info("POST /api/musicbrainz/recordings/%s/download", mbid)
    _require_valid_mbid(mbid)
    job_id = start_resolve_job(
        "recording",
        mbid,
        musicbrainz_service,
        search_service,
        download_service,
        config,
        db,
        event_hub,
    )
    return {"started": True, "job_id": job_id}


@router.post(
    "/albums/{mbid}/download",
    response_model=DownloadJobResponse,
    status_code=202,
)
def download_album(
    mbid: str,
    musicbrainz_service: MusicBrainzServiceDep,
    search_service: SearchServiceDep,
    download_service: DownloadServiceDep,
    config: ConfigDep,
    db: DbDep,
    event_hub: EventHubDep,
) -> dict:
    """Start a resolve-and-queue job for a whole album's tracks."""
    logger.info("POST /api/musicbrainz/albums/%s/download", mbid)
    _require_valid_mbid(mbid)
    job_id = start_resolve_job(
        "album",
        mbid,
        musicbrainz_service,
        search_service,
        download_service,
        config,
        db,
        event_hub,
    )
    return {"started": True, "job_id": job_id}
