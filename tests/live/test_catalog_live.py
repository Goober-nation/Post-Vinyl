"""Catalog service on the real MusicBrainz and Deezer (#95).

Only public catalogs, no test stack needed:

    PV_LIVE=1 pytest tests/live/test_catalog_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.clients.deezer import DeezerClient
from app.clients.musicbrainz import MusicBrainzClient
from app.config import load_sources
from app.services.catalog import CatalogService
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.songs import SongsStore
from app.stores.users import UsersStore

pytestmark = pytest.mark.skipif(os.environ.get("PV_LIVE") != "1", reason="PV_LIVE=1")


@pytest.fixture
def setup(tmp_path: Path) -> tuple[CatalogService, SourcePreferences, Database, int]:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "enc", False, 0).id
    prefs = SourcePreferences(db, load_sources(Path("post-vinyl.example.yaml")))
    svc = CatalogService(db, prefs, [MusicBrainzClient(), DeezerClient()])
    return svc, prefs, db, uid


def test_search_resolve_and_user_order(
    setup: tuple[CatalogService, SourcePreferences, Database, int],
) -> None:
    svc, prefs, db, uid = setup
    res = svc.search_tracks(uid, "Feather", "Nujabes")
    assert res.source == "musicbrainz" and res.items
    for t in res.items:
        print(" ", t.score, t.title, [(r.status, r.primary_type) for r in t.releases])
    pick = res.items[0]
    # #98: the studio recording first, the "N0ms Bootleg" last (MB scores it 100)
    assert pick.title == "Feather" and "Bootleg" in res.items[-1].title
    song = svc.resolve(uid, "musicbrainz", pick.id)
    with db.transaction() as conn:
        assert SongsStore().identities(conn, song.id) == {"musicbrainz": pick.id}
    print("musicbrainz pick:", pick.title, "/", pick.artist, "→ song", song)

    prefs.set_for_user(uid, "catalog", ["deezer"], [])
    res = svc.search_tracks(uid, "Feather", "Nujabes")
    assert res.source == "deezer" and res.items
    dz_song = svc.resolve(uid, "deezer", res.items[0].id)
    assert dz_song.id != song.id  # only the pick is stored; linking is tagging's job
    print("deezer pick:", res.items[0].title, "→ song", dz_song)

    albums = svc.search_albums(uid, "Modal Soul", "Nujabes")
    editions = svc.album_editions(uid, albums.source or "", albums.items[0].id)
    tracks = svc.edition_tracks(uid, albums.source or "", editions[0].id)
    assert any("Feather" in t.title for t in tracks)
