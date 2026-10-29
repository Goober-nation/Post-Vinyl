"""Tagging on real downloads (#104): MusicBrainz pick → real downloaded file →
tag (MusicBrainz + Cover Art Archive, ListenBrainz for a song without an MBID)
→ read the tags back → files service moves it → test Navidrome indexes it.

Copies files the download run left in slskd's downloads folder; everything
lands under the songs' real artist folders in the test library and is removed
again at the end.

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_tagging_live.py -v -s
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import mutagen
import pytest

from app.clients.coverart import CoverArtClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.navidrome import NavidromeClient
from app.config import FilesSettings
from app.services.catalog import CatalogService
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
from app.services.sources import SourcePreferences
from app.services.tagging import TaggingService
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")
# (title, artist, a word every downloaded copy's file name has)
SONGS = [("Roygbiv", "Boards of Canada", "roygbiv"), ("Teardrop", "Massive Attack", "teardrop"),
         ("Xtal", "Aphex Twin", "xtal")]  # fmt: skip
NO_MBID = ("Windowlicker", "Aphex Twin", "windowlicker")  # found through ListenBrainz


def test_tag_move_index(tmp_path: Path) -> None:
    music = Path(os.environ["MUSIC_HOST_DIR"])
    settings = FilesSettings(str(music))
    downloads, library = (
        music / settings.slskd_downloads_dir,
        music / settings.library_dir,
    )
    have = [p for p in (downloads / "post-vinyl").rglob("*") if p.is_file()
            and p.suffix.lower() in (".flac", ".mp3", ".m4a", ".ogg", ".opus")]  # fmt: skip
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd = NavidromeClient(NAVIDROME_URL)
    user, password = os.environ["NAVIDROME_USERNAME"], os.environ["NAVIDROME_PASSWORD"]
    with db.transaction() as conn:
        uid = (
            UsersStore().upsert_on_login(conn, user, box.encrypt(password), True, 0).id
        )
        if os.environ.get("LISTENBRAINZ_TOKEN"):
            UsersStore().set_lb_token(
                conn, uid, box.encrypt(os.environ["LISTENBRAINZ_TOKEN"])
            )
    mb = MusicBrainzClient()
    catalog = CatalogService(
        db, SourcePreferences(db, {"catalog": ("musicbrainz",)}), [mb]
    )
    files = FilesService(db, nd, box, settings)
    tagging = TaggingService(
        db, mb, ListenBrainzClient(), CoverArtClient(), box, settings
    )

    jobs: list[tuple[str, int]] = []
    for n, (title, artist, word) in enumerate([*SONGS, NO_MBID]):
        src = next((p for p in have if word in p.name.lower()), None)
        if src is None:
            print(f"skip {artist} – {title}: no downloaded copy")
            continue
        if (title, artist, word) == NO_MBID:
            with db.transaction() as conn:  # like a Soulseek-tab song: no identities
                song = SongsStore().create(conn, SongFields(
                    title, artist, query_title=title, main_artist=artist))  # fmt: skip
        else:
            hit = catalog.search_tracks(uid, title, artist).items[0]
            song = catalog.resolve(uid, "musicbrainz", hit.id)
        rel = f"post-vinyl/tagtest-{os.getpid()}-{n}/1/{src.name}"
        (downloads / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, downloads / rel)
        with db.transaction() as conn:
            req = RequestsStore().create(conn, uid, "catalog", 0, song_id=song.id)
            d = DownloadsStore().create(conn, req.id, "slskd", 0)
            DownloadsStore().complete(conn, d.id, rel, 0)
            rec = FilesStore().for_song(conn, song.id)
        assert rec is not None
        jobs.append((f"{artist} – {title}", rec.id))

    placed: list[Path] = []
    try:
        for name, fid in jobs:
            assert files.verify(fid), name
            ok = tagging.tag(fid)
            with db.transaction() as conn:
                rows = FilesStore().tagging_for_file(conn, fid)
                rec = FilesStore().get(conn, fid)
            assert rec is not None
            print(f"{name}: tag={ok} {rows[-1].status} {rows[-1].error or ''} "
                  f"release={rows[-1].mb_release_id}")  # fmt: skip
            if ok:
                back = mutagen.File(downloads / rec.file_location, easy=True)
                t = back.tags or {}
                print(f"  tags: {t.get('artist')} | {t.get('album')} | {t.get('date')} | "
                      f"track {t.get('tracknumber')} | mbid {t.get('musicbrainz_trackid')} | "
                      f"cover {rows[-1].tags_written and rows[-1].tags_written.get('cover')}")  # fmt: skip
                assert t.get("musicbrainz_trackid") and t.get("album")
            assert files.move(fid)
            with db.transaction() as conn:
                rec = FilesStore().get(conn, fid)
            assert rec is not None
            placed.append(music / rec.file_location)
            assert files.index(fid), f"{name}: not listed"
            with db.transaction() as conn:
                rec = FilesStore().get(conn, fid)
            assert rec is not None
            s = nd.get_song(nd.login(user, password), rec.navidrome_id or "")
            print(
                f"  library: {rec.file_location}\n  Navidrome: {s.artist} | {s.album} | {s.title}"
            )
    finally:
        for p in placed:
            if p.exists():
                p.unlink()
            for parent in (p.parent, p.parent.parent, p.parent.parent.parent):
                if parent != library and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
        for d in (downloads / "post-vinyl").glob(f"tagtest-{os.getpid()}-*"):
            shutil.rmtree(d)
        nd.start_scan(nd.login(user, password))
