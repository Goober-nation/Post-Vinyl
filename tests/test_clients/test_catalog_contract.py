"""One contract for every CatalogSource, so callers can swap catalogs (#81, #82).

The fake always runs. MusicBrainz and Deezer run live with ``PV_LIVE=1``
(real network calls, rate-limited, ~30 s).
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from app.clients.catalog import (
    Album,
    Artist,
    CatalogNotFound,
    CatalogSource,
    Edition,
    Track,
)
from app.clients.catalog_fake import FakeCatalog

LIVE = os.environ.get("PV_LIVE") == "1"
TITLE, ARTIST, ALBUM = "Kickstart My Heart", "Mötley Crüe", "Dr. Feelgood"
UNKNOWN = {
    "musicbrainz": "3f1b6a2e-9c1d-4c2a-8e1f-2b7d5a9c0e11",
    "deezer": "999999999999",
}


def fake_catalog() -> FakeCatalog:
    fake = FakeCatalog()
    song = Track("fake", "t1", TITLE, ARTIST, ("a1",), 282.0, ("USEE10000001",), "")
    fake.tracks["t1"] = song
    fake.artists["a1"] = Artist("fake", "a1", ARTIST, "")
    fake.albums["al1"] = Album("fake", "al1", ALBUM, ARTIST, "Album", (), "1989")
    fake.albums_by_artist["a1"] = ["al1"]
    fake.editions["al1"] = [Edition("fake", "e1", ALBUM, "1989", "", "", ("CD",), 1)]
    fake.tracklists["e1"] = [replace(song, disc=1, position=1)]
    return fake


def live(name: str) -> object:
    return pytest.param(
        name, marks=pytest.mark.skipif(not LIVE, reason="set PV_LIVE=1 for live")
    )


@pytest.fixture(params=["fake", live("musicbrainz"), live("deezer")])
def catalog(request: pytest.FixtureRequest) -> CatalogSource:
    if request.param == "musicbrainz":
        from app.clients.musicbrainz import MusicBrainzClient

        return MusicBrainzClient()
    if request.param == "deezer":
        from app.clients.deezer import DeezerClient

        return DeezerClient()
    return fake_catalog()


def find_songs(catalog: CatalogSource) -> list[Track]:
    # MusicBrainz returns tied hits in varying order: fetch all of them
    hits = catalog.search_tracks(TITLE, ARTIST, limit=100)
    exact = [t for t in hits if t.title.lower() == TITLE.lower() and ARTIST in t.artist]
    assert exact, f"{catalog.source}: {TITLE} not in {[t.title for t in hits]}"
    return exact


def test_track_round_trip(catalog: CatalogSource) -> None:
    found = find_songs(catalog)
    assert all(t.source == catalog.source for t in found)
    # Not every catalog entry has an ISRC (many MusicBrainz recordings don't),
    # and search ranking varies between calls, so look through every exact hit
    # until one has ISRCs; a well-known song must have some.
    song = None
    for hit in found:
        full = catalog.get_track(hit.id)
        assert full.id == hit.id
        if full.isrcs:
            song = full
            break
    assert song, f"{catalog.source}: no ISRC on {[t.id for t in found]}"
    assert song.duration_s and 200 < song.duration_s < 700
    by_isrc = catalog.tracks_by_isrc(song.isrcs[0])
    assert any(t.title.lower() == TITLE.lower() for t in by_isrc)


def test_browse_artist_to_tracklist(catalog: CatalogSource) -> None:
    artist = next(a for a in catalog.search_artists(ARTIST, 5) if a.name == ARTIST)
    albums = catalog.artist_albums(artist.id)
    album = next(a for a in albums if a.title == ALBUM and a.primary_type == "Album")
    editions = catalog.album_editions(album.id)
    assert editions and all(e.source == catalog.source for e in editions)
    edition = max(editions, key=lambda e: e.track_count)
    tracks = catalog.edition_tracks(edition.id)
    assert len(tracks) == edition.track_count
    assert all(t.disc and t.position for t in tracks)
    assert any(t.title.lower() == TITLE.lower() for t in tracks)


def test_search_albums_finds_album(catalog: CatalogSource) -> None:
    assert any(a.title == ALBUM for a in catalog.search_albums(ALBUM, ARTIST, 10))


def test_unknown_id_is_not_found(catalog: CatalogSource) -> None:
    with pytest.raises(CatalogNotFound):
        catalog.get_track(UNKNOWN.get(catalog.source, "missing"))
