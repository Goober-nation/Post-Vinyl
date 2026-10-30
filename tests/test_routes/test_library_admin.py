"""File management, admin and credentials routes (#112).

Real services and SQLite, real FLAC files in a temp library, fake MusicBrainz /
ListenBrainz / Cover Art, and a Navidrome fake that indexes the library folder
on each scan (the same path keeps its id, as Navidrome 0.64.2 does).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from mutagen.flac import FLAC

from app.clients.catalog import Track
from app.clients.catalog_fake import FakeCatalog
from app.clients.coverart import FakeCoverArt
from app.clients.listenbrainz import ListenBrainzError, RecordingMetadata, TokenCheck
from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.clients.musicbrainz_fake import FakeMusicBrainzTags
from app.clients.navidrome import ScanStatus, Session, Song
from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import FilesSettings, Settings
from app.dependencies import Core
from app.main import create_app
from app.services.admin import AdminService
from app.services.auth import AuthService
from app.services.catalog_fake import FakeCatalogService
from app.services.credentials import CredentialsService
from app.services.dispatch_fake import FakeDownloadDispatch
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
from app.services.library import LibraryService
from app.services.requests import RequestService
from app.services.sources import SourcePreferences
from app.services.tagging import TaggingService
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore
from tests.test_routes.test_catalog_requests import ORIGIN, SAME, logged_in
from tests.test_services.test_tagging import COVER, REC, flac, info, release

NOW = 1_800_000_000
HAND_ADDED = "Unknown Artist/feather nujabes.flac"  # a peer's name, untagged


class IndexingNavidrome(FakeNavidromeClient):
    """start_scan indexes every audio file under the library, id = its path."""

    def __init__(self, users: dict[str, tuple[str, bool]], library: Path) -> None:
        super().__init__(users)
        self.library = library
        self.after_scan: Callable[[], None] | None = None  # runs mid-rename

    def start_scan(self, session: Session, full: bool = False) -> ScanStatus:
        self.songs = {}
        for p in sorted(self.library.rglob("*.flac")):
            rel = str(p.relative_to(self.library))
            tags: Any = FLAC(p).tags or {}
            title, artist, album = (
                (tags.get(k) or [""])[0] for k in ("title", "artist", "album")
            )
            self.songs[nd_id(rel)] = Song(
                nd_id(rel), rel, "/music/library", title or p.stem, artist, album,
                175.0, p.stat().st_size, "flac", False,
            )  # fmt: skip
        if self.after_scan:
            self.after_scan()
        return super().start_scan(session, full)


def nd_id(rel: str) -> str:
    """Opaque like Navidrome's own, and stable per path."""
    return hashlib.md5(rel.encode()).hexdigest()


class LB(FakeListenBrainzClient):
    def __init__(self) -> None:
        super().__init__()
        self.tokens = {"good": "alice_lb"}
        self.recordings[REC] = RecordingMetadata(REC, "Feather", 175.0, "Nujabes",
                                                 (), None, (), (), ())  # fmt: skip
        self.down = False
        self.lookups = 0

    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        self.lookups += 1
        return super().lookup_recording(artist, title, token)

    def validate_token(self, token: str) -> TokenCheck:
        if self.down:
            raise ListenBrainzError("GET /validate-token: timed out")
        return super().validate_token(token)


@dataclass
class Env:
    alice: TestClient  # admin
    bob: TestClient
    db: Database
    library: Path
    nd: IndexingNavidrome
    lb: LB
    prefs: SourcePreferences
    box: SecretBox
    alice_id: int
    files: FilesService


@pytest.fixture
def env(tmp_path: Path) -> Env:
    music = tmp_path / "music"
    settings = FilesSettings(str(music))
    library = music / settings.library_dir
    library.mkdir(parents=True)
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd = IndexingNavidrome({"alice": ("pw", True), "bob": ("pw", False)}, library)
    auth = AuthService(db, nd, box)
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz", "listenbrainz")})
    mb, lb, caa = FakeMusicBrainzTags(), LB(), FakeCoverArt()
    mb.recordings[REC] = info()
    mb.releases["modal-2005"] = release()
    caa.covers["modal-2005"] = COVER
    search = FakeCatalog("musicbrainz")
    search.tracks[REC] = Track(
        "musicbrainz", REC, "Feather", "Nujabes", (), 175.0, (), ""
    )
    files = FilesService(db, nd, box, settings, lambda: NOW, lambda s: None)
    tagging = TaggingService(db, mb, lb, caa, box, settings, lambda: NOW,
                             search=search, sources=prefs)  # fmt: skip
    core = Core(
        FakeCatalogService(), RequestService(db), FakeDownloadDispatch(),
        library=LibraryService(db, files, tagging, box), admin=AdminService(db),
        credentials=CredentialsService(db, lb, box, prefs),
    )  # fmt: skip
    app = create_app(Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN), auth, core)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    (library / "Unknown Artist").mkdir()
    flac(library / HAND_ADDED)
    nd.start_scan(nd.login("alice", "pw"))
    with db.transaction() as conn:
        u = UsersStore().get_by_username(conn, "alice")
    assert u is not None
    return Env(alice, bob, db, library, nd, lb, prefs, box, u.id, files)


def song_id(env: Env) -> str:
    [item] = env.alice.get("/api/library/files").json()["items"]
    sid: str = item["id"]
    return sid


def track_by_us(env: Env) -> int:
    """Make the hand-added file look like a download Post-Vinyl indexed."""
    with env.db.transaction(write=True) as conn:
        song = SongsStore().create(conn, SongFields("feather", "nujabes"))
        r = RequestsStore().create(conn, env.alice_id, "catalog", NOW, song_id=song.id)
        d = DownloadsStore().create(conn, r.id, "slskd", NOW)
        DownloadsStore().complete(conn, d.id, "x.flac", NOW)
        rec = FilesStore().for_song(conn, song.id)
        assert rec is not None
        FilesStore().set_location(conn, rec.id, f"library/{HAND_ADDED}", NOW)
        FilesStore().set_navidrome_id(conn, rec.id, nd_id(HAND_ADDED), NOW)
        FilesStore().set_state(conn, rec.id, "indexed", NOW)
    return rec.id


# ------------------------------------------------------------ the list


def test_the_whole_library_is_listed_with_our_records_where_we_have_them(
    env: Env,
) -> None:
    page = env.bob.get("/api/library/files").json()  # any user may look
    assert page["total"] == 1 and page["items"][0]["file_id"] is None  # hand-added
    fid = track_by_us(env)
    [item] = env.bob.get("/api/library/files", params={"q": "feather"}).json()["items"]
    assert (item["file_id"], item["file_state"]) == (fid, "indexed")
    assert env.bob.get("/api/library/files", params={"q": "zzz"}).json()["total"] == 0


# ------------------------------------------------------------ covers (#126)


def test_a_cover_comes_from_navidrome_and_is_never_stored(env: Env) -> None:
    sid = song_id(env)
    assert env.bob.get(f"/api/library/files/{sid}/cover").status_code == 404  # none
    env.nd.covers[sid] = b"\xff\xd8jpeg"
    r = env.bob.get(f"/api/library/files/{sid}/cover", params={"size": 80})
    assert (r.status_code, r.content, r.headers["content-type"]) == (
        200, b"\xff\xd8jpeg", "image/jpeg")  # fmt: skip
    assert r.headers["cache-control"].startswith("private")  # behind the login
    assert (
        env.bob.get(f"/api/library/files/{sid}/cover", params={"size": 5}).status_code
        == 422
    )
    assert env.bob.get("/api/library/files/nope/cover").status_code == 404
    with env.db.transaction() as conn:  # nothing written for it
        assert conn.execute("SELECT COUNT(*) FROM file_management").fetchone()[0] == 0


def test_a_download_carries_its_library_id_once_indexed(env: Env) -> None:
    fid = track_by_us(env)
    with env.db.transaction() as conn:
        rec = conn.execute(
            "SELECT * FROM file_management WHERE id = ?", (fid,)
        ).fetchone()
        [row] = DownloadsStore().queue_page(conn, None, None, 10, 0)
    assert row.library_id == rec["navidrome_id"] == nd_id(HAND_ADDED)


# ------------------------------------------------------------ rename


def test_rename_suggests_via_listenbrainz_then_apply_retags_and_moves(env: Env) -> None:
    fid = track_by_us(env)
    with env.db.transaction(write=True) as conn:
        UsersStore().set_lb_token(conn, env.alice_id, env.box.encrypt("good"))
    sid = song_id(env)
    r = env.alice.post(f"/api/library/files/{sid}/rename",
                       json={"title": "Feather", "artist": "Nujabes"}, headers=SAME)  # fmt: skip
    s = r.json()
    assert s["match"] and s["via"] == "listenbrainz" and s["release_id"] == "modal-2005"
    assert s["tags"]["album"] == "Modal Soul" and s["has_cover"]
    r = env.alice.post(f"/api/library/files/{sid}/apply",
                       json={"recording_id": REC, "release_id": "modal-2005"}, headers=SAME)  # fmt: skip
    new_rel = "Nujabes/Modal Soul (2005)/04 Feather.flac"
    assert r.status_code == 200 and r.json()["file"]["id"] == nd_id(new_rel)
    assert FLAC(env.library / new_rel)["album"] == ["Modal Soul"]
    assert not (env.library / "Unknown Artist").exists()  # emptied folder removed
    with env.db.transaction() as conn:
        rec = FilesStore().get(conn, fid)
    assert rec is not None
    assert (rec.file_location, rec.navidrome_id) == (
        f"library/{new_rel}",
        nd_id(new_rel),
    )


def test_rename_falls_back_to_musicbrainz_and_respects_sources(env: Env) -> None:
    sid = song_id(env)
    body = {"title": "Feather", "artist": "Nujabes"}
    r = env.alice.post(f"/api/library/files/{sid}/rename", json=body, headers=SAME)
    assert r.json()["via"] == "musicbrainz"  # no token: ListenBrainz skipped
    with env.db.transaction(write=True) as conn:
        UsersStore().set_lb_token(conn, env.alice_id, env.box.encrypt("good"))
    env.prefs.set_for_user(env.alice_id, "catalog", [], ["listenbrainz"])
    r = env.alice.post(f"/api/library/files/{sid}/rename", json=body, headers=SAME)
    assert r.json()["via"] == "musicbrainz" and env.lb.lookups == 0  # off: not asked
    env.prefs.set_for_user(env.alice_id, "catalog", [], ["musicbrainz"])
    r = env.alice.post(f"/api/library/files/{sid}/rename", json=body, headers=SAME)
    assert r.status_code == 409  # no tag set without MusicBrainz
    r = env.alice.post(f"/api/library/files/{sid}/apply",
                       json={"recording_id": REC, "release_id": "modal-2005"}, headers=SAME)  # fmt: skip
    assert r.status_code == 409


def test_no_match_then_strip_moves_by_the_new_names(env: Env) -> None:
    sid = song_id(env)
    path = env.library / HAND_ADDED
    peer = FLAC(path)
    peer["comment"] = "ripped by someone"
    peer.save()
    r = env.alice.post(f"/api/library/files/{sid}/rename",
                       json={"title": "Nothing Like It", "artist": "Nobody"}, headers=SAME)  # fmt: skip
    assert r.json() == {"match": False, "recording_id": None, "release_id": None,
                        "via": None, "tags": None, "has_cover": False}  # fmt: skip
    r = env.alice.post(f"/api/library/files/{sid}/strip",
                       json={"title": "Nothing Like It", "artist": "Nobody"}, headers=SAME)  # fmt: skip
    assert r.json()["file"]["id"] == nd_id("Nobody/Nothing Like It.flac")
    assert not (FLAC(env.library / "Nobody/Nothing Like It.flac").tags or {})


def test_the_missing_check_waits_for_a_rename_in_progress(env: Env) -> None:
    """Live #114: the hourly check ran during a rename's rescan, saw the old id
    gone and marked the just-renamed file missing."""
    file_id, sid = track_by_us(env), song_id(env)
    check = threading.Thread(target=env.files.check_missing, args=(file_id,))

    def check_now() -> None:  # the old id is gone from Navidrome by now
        env.nd.after_scan = None
        check.start()
        check.join(timeout=0.5)  # without the lock it finishes here

    env.nd.after_scan = check_now
    r = env.alice.post(f"/api/library/files/{sid}/strip",
                       json={"title": "Feather", "artist": "Nujabes"}, headers=SAME)  # fmt: skip
    assert r.status_code == 200
    check.join()
    with env.db.transaction() as conn:
        rec = FilesStore().get(conn, file_id)
    assert rec is not None and rec.file_state == "indexed"
    assert rec.navidrome_id == nd_id("Nujabes/Feather.flac")


def test_apply_refuses_a_release_that_isnt_the_recordings(env: Env) -> None:
    r = env.alice.post(f"/api/library/files/{song_id(env)}/apply",
                       json={"recording_id": REC, "release_id": "someone-else"}, headers=SAME)  # fmt: skip
    assert r.status_code == 422
    assert (env.library / HAND_ADDED).exists()  # untouched


# ------------------------------------------------------------ delete


def test_delete_is_admin_only_and_marks_our_record(env: Env) -> None:
    fid = track_by_us(env)
    sid = song_id(env)
    assert env.bob.delete(f"/api/library/files/{sid}", headers=SAME).status_code == 403
    assert env.bob.post(f"/api/library/files/{sid}/rename",
                        json={"title": "a", "artist": "b"}, headers=SAME).status_code == 403  # fmt: skip
    assert (
        env.alice.delete(f"/api/library/files/{sid}", headers=SAME).status_code == 204
    )
    assert not (env.library / "Unknown Artist").exists()
    with env.db.transaction() as conn:
        rec = FilesStore().get(conn, fid)
    assert rec is not None and rec.file_state == "deleted"
    assert env.alice.get("/api/library/files").json()["total"] == 0  # rescanned
    assert (
        env.alice.delete(f"/api/library/files/{sid}", headers=SAME).status_code == 404
    )


def test_a_path_outside_the_library_is_never_touched(env: Env, tmp_path: Path) -> None:
    outside = flac(tmp_path / "outside.flac")
    env.nd.songs["evil"] = Song("evil", "../../outside.flac", "/music/library", "x", "y",
                                "", 1.0, 1, "flac", False)  # fmt: skip
    assert env.alice.delete("/api/library/files/evil", headers=SAME).status_code == 404
    r = env.alice.post("/api/library/files/evil/strip", json={"title": "a", "artist": "b"},
                       headers=SAME)  # fmt: skip
    assert r.status_code == 404 and outside.exists()


# ------------------------------------------------------------ admin + credentials


def test_peers_and_users_are_admin_only_and_show_no_secrets(env: Env) -> None:
    with env.db.transaction(write=True) as conn:
        SlskdStore().record_peer_failure(conn, "slowpoke")
        SlskdStore().block_peer(conn, "banned", NOW)
    assert env.bob.get("/api/admin/peers").status_code == 403
    peers = env.alice.get("/api/admin/peers").json()
    assert [p["username"] for p in peers] == ["banned", "slowpoke"]
    assert env.alice.delete("/api/admin/peers/banned", headers=SAME).json() == {
        "cleared": 1
    }
    assert env.alice.delete("/api/admin/peers", headers=SAME).json() == {"cleared": 1}
    users = env.alice.get("/api/admin/users").json()
    assert [u["username"] for u in users] == ["alice", "bob"]
    assert all("nd_password_enc" not in u and "lb_token_enc" not in u for u in users)


def test_listenbrainz_token_is_checked_before_it_is_stored(env: Env) -> None:
    def put(token: str | None) -> Response:
        return env.bob.put("/api/me/credentials", json={"listenbrainz_token": token},
                           headers=SAME)  # fmt: skip

    r = put("good")
    assert r.status_code == 200 and r.json() == {"listenbrainz": True,
                                                 "listenbrainz_user": "alice_lb"}  # fmt: skip
    assert put("bad").status_code == 422
    env.lb.down = True
    assert put("good").status_code == 503
    assert env.bob.get("/api/me/credentials").json()["listenbrainz"]  # old one kept
    with env.db.transaction() as conn:
        bob = UsersStore().get_by_username(conn, "bob")
    assert bob is not None and bob.lb_token_enc and "good" not in bob.lb_token_enc
    env.lb.down = False
    env.prefs.set_for_user(bob.id, "catalog", [], ["listenbrainz"])
    assert put("good").status_code == 409  # checking needs ListenBrainz on
    assert put(None).json() == {"listenbrainz": False, "listenbrainz_user": None}
