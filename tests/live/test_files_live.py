"""Files service on the test stack (#103): files the download run left in slskd's
downloads folder → verify → move into the library → Navidrome scan → link.
Moves real files inside the test MUSIC_HOST_DIR (from downloads into library).

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_files_live.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.clients.navidrome import NavidromeClient
from app.config import FilesSettings
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
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
HOW_MANY = 3


def test_move_scan_link(tmp_path: Path) -> None:
    music = Path(os.environ["MUSIC_HOST_DIR"])
    settings = FilesSettings(str(music))
    downloads = music / settings.slskd_downloads_dir
    found = sorted(p for p in (downloads / "post-vinyl").rglob("*") if p.is_file())
    assert found, "run tests/live/test_slskd_download_live.py first"
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd = NavidromeClient(NAVIDROME_URL)
    user, password = os.environ["NAVIDROME_USERNAME"], os.environ["NAVIDROME_PASSWORD"]
    assert nd.login(user, password).is_admin, "the test Navidrome user must be an admin"
    with db.transaction() as conn:
        uid = (
            UsersStore().upsert_on_login(conn, user, box.encrypt(password), True, 0).id
        )
    files = FilesService(db, nd, box, settings)

    ids = []
    for i, path in enumerate(found[:HOW_MANY]):
        rel = str(path.relative_to(downloads))
        artist, title = f"Live Test {i}", path.stem  # no tags: lands as Artist/Title
        with db.transaction() as conn:
            song = SongsStore().create(conn, SongFields(title, artist, query_title=title,
                                                        main_artist=artist))  # fmt: skip
            req = RequestsStore().create(conn, uid, "catalog", 0, song_id=song.id)
            d = DownloadsStore().create(conn, req.id, "slskd", 0)
            DownloadsStore().complete(conn, d.id, rel, 0)
            rec = FilesStore().for_song(conn, song.id)
        assert rec is not None
        ids.append(rec.id)

    for fid in ids:
        assert files.verify(fid), f"file {fid} failed verification"
        assert files.move(fid)
    for fid in ids:
        assert files.index(fid), f"file {fid} not listed after the scan"
        with db.transaction() as conn:
            rec = FilesStore().get(conn, fid)
        assert rec is not None
        song = nd.get_song(nd.login(user, password), rec.navidrome_id or "")
        print(f"file {fid}: {rec.file_state}, {rec.file_location} → Navidrome {song.id} "
              f"({song.path}, {song.duration:.0f} s)")  # fmt: skip
        assert rec.file_state == "indexed" and (music / rec.file_location).is_file()
        assert song.path == str(Path(rec.file_location).relative_to("library"))
