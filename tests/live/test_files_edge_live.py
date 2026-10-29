"""Files service edge cases on the test stack (#103): replacement, collision,
tagged multi-disc layout, odd names, verification failures, expired session.

Each scenario copies a real FLAC from the download run into its own download
folder, so the originals stay. Everything lands under "PV Edge …" artists in
the test library and is removed again at the end (then Navidrome rescans).

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_files_edge_live.py -v -s
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from app.clients.navidrome import NavidromeClient
from app.config import FilesSettings
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FileRecord, FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")


@dataclass
class Env:
    files: FilesService
    db: Database
    nd: NavidromeClient
    music: Path
    downloads: Path
    library: Path
    flacs: list[Path]
    uid: int
    user: str
    password: str = field(repr=False)  # keep it out of failure output
    n: int = 0

    def song(self, title: str, artist: str, duration: int | None = None) -> int:
        with self.db.transaction() as conn:
            return SongsStore().create(conn, SongFields(
                title, artist, query_title=title, main_artist=artist, duration_s=duration,
            )).id  # fmt: skip

    def download(self, song_id: int, src: Path, name: str | None = None) -> int:
        """Copy ``src`` into a fresh download folder and hand it off like slskd."""
        self.n += 1
        rel = f"post-vinyl/edge-{os.getpid()}-{self.n}/1/{name or src.name}"
        (self.downloads / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self.downloads / rel)
        with self.db.transaction() as conn:
            req = RequestsStore().create(conn, self.uid, "catalog", 0, song_id=song_id)
            d = DownloadsStore().create(conn, req.id, "slskd", 0)
            DownloadsStore().complete(conn, d.id, rel, 0)
            rec = FilesStore().for_song(conn, song_id)
        assert rec is not None
        return rec.id

    def rec(self, fid: int) -> FileRecord:
        with self.db.transaction() as conn:
            r = FilesStore().get(conn, fid)
        assert r is not None
        return r

    def through(self, fid: int) -> FileRecord:
        assert self.files.verify(fid), self.rec(fid).verification
        assert self.files.move(fid)
        assert self.files.index(fid), f"not listed: {self.rec(fid).file_location}"
        return self.rec(fid)

    def nd_session(self):
        return self.nd.login(self.user, self.password)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    music = Path(os.environ["MUSIC_HOST_DIR"])
    settings = FilesSettings(str(music))
    downloads, library = (
        music / settings.slskd_downloads_dir,
        music / settings.library_dir,
    )
    flacs = sorted(p for p in (downloads / "post-vinyl").rglob("*.flac")
                   if not p.parent.parent.name.startswith("edge-"))  # fmt: skip
    assert len(flacs) >= 2, "run tests/live/test_slskd_download_live.py first"
    db = Database(tmp_path_factory.mktemp("pv") / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd = NavidromeClient(NAVIDROME_URL)
    user, password = os.environ["NAVIDROME_USERNAME"], os.environ["NAVIDROME_PASSWORD"]
    with db.transaction() as conn:
        uid = (
            UsersStore().upsert_on_login(conn, user, box.encrypt(password), True, 0).id
        )
    e = Env(FilesService(db, nd, box, settings), db, nd, music, downloads, library,
            flacs, uid, user, password)  # fmt: skip
    yield e
    for d in library.glob(
        "PV Edge*"
    ):  # tidy the test library, then let Navidrome forget
        shutil.rmtree(d)
    for d in (downloads / "post-vinyl").glob(f"edge-{os.getpid()}-*"):
        shutil.rmtree(d)
    nd.start_scan(e.nd_session())


def test_replacement_deletes_old_copy_and_relinks(env: Env) -> None:
    sid = env.song("Replace Me", "PV Edge Replace")
    fid = env.download(sid, env.flacs[0])
    first = env.through(fid)
    old_path, old_id = env.music / first.file_location, first.navidrome_id
    assert old_path.is_file()
    env.download(sid, env.flacs[1])  # a different, "better" copy of the same song
    assert (
        env.rec(fid).navidrome_id == old_id and env.rec(fid).file_state == "downloaded"
    )
    second = env.through(fid)
    print(f"old {old_id} at {first.file_location} → new {second.navidrome_id} at "
          f"{second.file_location}")  # fmt: skip
    # Navidrome 0.64.2 keeps the song id for a file at the same path (live
    # 2026-10-29): the new copy took the old one's place, so links survive
    assert second.file_location == first.file_location
    assert second.navidrome_id == old_id
    new = env.nd.get_song(env.nd_session(), old_id or "")
    assert new.size == (env.music / second.file_location).stat().st_size
    assert new.size != env.flacs[0].stat().st_size  # Navidrome sees the new file
    assert (
        len(list(old_path.parent.glob("*.flac"))) == 1
    )  # the old copy is gone, no " (2)"


def test_collision_with_a_file_pv_did_not_put_there(env: Env) -> None:
    sid = env.song("Collide", "PV Edge Collide")
    foreign = env.library / "PV Edge Collide" / "Collide.flac"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(env.flacs[1], foreign)
    before = foreign.stat().st_size
    rec = env.through(env.download(sid, env.flacs[0]))
    assert rec.file_location == "library/PV Edge Collide/Collide (2).flac"
    assert foreign.stat().st_size == before  # untouched


def test_tagged_multi_disc_layout_with_unicode(env: Env) -> None:
    sid = env.song("ignored", "PV Edge ignored")
    fid = env.download(sid, env.flacs[0])
    assert env.files.verify(fid)
    path = env.downloads / env.rec(fid).file_location
    tags = FLAC(path)
    tags.delete()
    tags.update({"title": "Café – 夜の歌", "albumartist": "PV Edge Ünïcødé",
                 "artist": "PV Edge Ünïcødé", "album": "Ålbum: Part 2/3",
                 "date": "2024-05-01", "tracknumber": "7", "discnumber": "2",
                 "disctotal": "2"})  # fmt: skip
    tags.save()
    with env.db.transaction(write=True) as conn:  # what tagging (#104) will do
        FilesStore().set_state(conn, fid, "tagged", int(time.time()))
    assert env.files.move(fid)
    assert env.files.index(fid)
    rec = env.rec(fid)
    print("tagged:", rec.file_location)
    assert rec.file_location == (
        "library/PV Edge Ünïcødé/Ålbum: Part 2_3 (2024)/Disc 2/07 Café – 夜の歌.flac"
    )


def test_odd_names_are_made_safe_and_indexed(env: Env) -> None:
    title = "AC/DC: T.N.T.... " + "x" * 300
    sid = env.song(title, "PV Edge ..Odd/Name")
    rec = env.through(env.download(sid, env.flacs[0]))
    print("odd:", rec.file_location)
    parts = Path(rec.file_location).parts
    assert all(len(p) <= 125 and "/" not in p and not p.startswith(".") for p in parts)


@pytest.mark.parametrize("kind", ["truncated", "text", "duration"])
def test_verification_failures(env: Env, kind: str) -> None:
    sid = env.song(
        f"Bad {kind}", "PV Edge Bad", duration=30 if kind == "duration" else None
    )
    fid = env.download(sid, env.flacs[0], f"bad-{kind}.flac")
    path = env.downloads / env.rec(fid).file_location
    if kind == "truncated":
        path.write_bytes(path.read_bytes()[:2000])
    elif kind == "text":
        path.write_text("<html>not a flac</html>")
    ok = env.files.verify(fid)
    print(f"{kind}: verify={ok}, verification={env.rec(fid).verification}")
    assert not ok and env.rec(fid).file_state == "downloaded"
    assert not env.files.move(fid)


def test_expired_session_relogs_in(env: Env) -> None:
    sid = env.song("Session", "PV Edge Session")
    fid = env.download(sid, env.flacs[0])
    assert env.files.verify(fid) and env.files.move(fid)
    assert env.files._session is not None or env.files.index(fid)
    if env.files._session is not None:
        env.files._session.jwt = "expired"
        env.files._session.subsonic_token = "expired"
        assert env.files.index(fid)
