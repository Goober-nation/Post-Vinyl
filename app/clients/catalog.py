"""Generic interface over music catalogs (MusicBrainz, Deezer, …) (#81).

Callers that only need "find a song / browse an artist" use this, so the
catalog behind it can be swapped. Albums follow MusicBrainz's model: an
album (release group) has editions (releases), and tracks belong to an
edition; a catalog with one tracklist per album returns one edition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class CatalogError(Exception):
    """A catalog call failed: connection error, bad input or unexpected status."""


class CatalogNotFound(CatalogError):
    """Unknown id or ISRC."""


class CatalogBusy(CatalogError):
    """The catalog is rate-limiting us; retry later."""


@dataclass(frozen=True)
class ReleaseRef:
    """A release a search hit appears on (MusicBrainz); plain data for ordering."""

    status: str  # "Official", "Bootleg", … ("" when unknown)
    primary_type: str  # release group's "Album", "Single", … ("" when unknown)
    secondary_types: tuple[str, ...]  # "Compilation", "Live", "Mixtape/Street", …


@dataclass(frozen=True)
class Track:
    source: str  # "musicbrainz", "deezer", …
    id: str  # the source's id for the song (MB: recording MBID)
    title: str
    # credited artist line as displayed, e.g. "A feat. B" (MusicBrainz);
    # Deezer gives the main artist only, featured ones only in artist_ids
    artist: str
    artist_ids: tuple[str, ...]
    duration_s: float | None
    isrcs: tuple[str, ...]  # empty when the call doesn't return them
    disambiguation: str  # e.g. "live", "radio edit"; "" when none
    # the credit as parts: (name, join phrase after it), e.g. (("A", " feat. "),
    # ("B", "")); core/normalize picks the main artist from them
    credits: tuple[tuple[str, str], ...] = ()
    # only from edition_tracks(): disc number and track number on that disc
    disc: int | None = None
    position: int | None = None
    # only from searches, where the source gives them (MusicBrainz)
    score: int | None = None
    first_release_date: str = ""  # "YYYY[-MM[-DD]]" or ""
    releases: tuple[ReleaseRef, ...] = ()
    video: bool = False  # MusicBrainz marks music-video recordings
    cover_id: str = ""  # what the source's cover lookup takes (#127); "" none


@dataclass(frozen=True)
class Artist:
    source: str
    id: str
    name: str
    disambiguation: str


@dataclass(frozen=True)
class Album:
    source: str
    id: str
    title: str
    artist: str
    primary_type: str  # "Album", "EP", "Single", … ("" when unknown)
    secondary_types: tuple[str, ...]  # "Live", "Compilation", …
    first_release_date: str  # "YYYY[-MM[-DD]]" or ""
    cover_id: str = ""  # what the source's cover lookup takes (#127); "" none


@dataclass(frozen=True)
class Edition:
    source: str
    id: str
    title: str
    date: str
    country: str
    status: str  # "Official", "Bootleg", … ("" when unknown)
    formats: tuple[str, ...]  # per medium, e.g. ("CD", "CD")
    track_count: int
    cover_id: str = ""  # what the source's cover lookup takes (#127); "" none


class CatalogSource(ABC):
    source: str

    @abstractmethod
    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]: ...

    @abstractmethod
    def search_artists(self, name: str, limit: int = 25) -> list[Artist]: ...

    @abstractmethod
    def search_albums(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Album]: ...

    @abstractmethod
    def get_track(self, track_id: str) -> Track:
        """One song with its ISRCs."""

    @abstractmethod
    def tracks_by_isrc(self, isrc: str) -> list[Track]:
        """Songs carrying ``isrc``; usually one, sometimes several."""

    @abstractmethod
    def artist_albums(self, artist_id: str) -> list[Album]:
        """Every album, EP and single of the artist (all pages)."""

    @abstractmethod
    def album_editions(self, album_id: str) -> list[Edition]: ...

    @abstractmethod
    def edition_tracks(self, edition_id: str) -> list[Track]:
        """The edition's tracklist in order, with ISRCs."""
