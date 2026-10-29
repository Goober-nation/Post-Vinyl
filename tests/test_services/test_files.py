"""Files service: verify, layout, move, index, replaced copies (#103)."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from app.clients.navidrome import Song as NdSong
from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import FilesSettings
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService, NoAdmin, free_path, layout, segment
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import Song, SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
SONG = Song(1, "Feather", "Nujabes featuring Cise Starr & Akin from CYNE", "Feather",
            "Nujabes", None, None, None, None, 175)  # fmt: skip


def wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * int(8000 * seconds))


# ------------------------------------------------------------ layout


def test_layout_from_known_tags() -> None:
    tags = {"title": ["Feather"], "albumartist": ["Nujabes"], "album": ["Modal Soul"],
            "date": ["2005-11-11"], "tracknumber": ["4/14"], "discnumber": ["1/1"]}  # fmt: skip
    assert layout(tags, SONG, "flac", tagged=True) == Path(
        "Nujabes/Modal Soul (2005)/04 Feather.flac"
    )


def test_disc_folder_only_for_multi_disc_releases() -> None:
    tags = {"title": ["Song"], "artist": ["A"], "album": ["Box"], "date": ["1999"],
            "tracknumber": ["3"], "discnumber": ["2/3"]}  # fmt: skip
    assert layout(tags, SONG, "mp3", tagged=True) == Path(
        "A/Box (1999)/Disc 2/03 Song.mp3"
    )
    tags["discnumber"] = ["2"]
    tags["disctotal"] = ["2"]
    assert layout(tags, SONG, "mp3", tagged=True) == Path(
        "A/Box (1999)/Disc 2/03 Song.mp3"
    )


def test_untagged_file_uses_the_songs_normalized_names_only() -> None:
    """A peer's own tags are not known to be true: they're ignored."""
    peer_tags = {
        "title": ["feather (nujabes remix) [www.site.ru]"],
        "album": ["Best Of"],
    }
    assert layout(peer_tags, SONG, "mp3", tagged=False) == Path("Nujabes/Feather.mp3")


def test_no_year_and_unsafe_names() -> None:
    tags = {"title": ["AC/DC: T.N.T."], "artist": ["../AC/DC"], "album": [".hidden"]}
    rel = layout(tags, SONG, "flac", tagged=True)
    assert ".." not in rel.parts and all("/" not in p for p in rel.parts)
    assert rel == Path("_AC_DC/hidden/AC_DC: T.N.T.flac")
    assert segment("", "Unknown") == "Unknown" and len(segment("x" * 500, "u")) == 120


def test_free_path_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "Song.mp3"
    assert free_path(target) == target
    target.write_text("x")
    (tmp_path / "Song (2).mp3").write_text("x")
    assert free_path(target) == tmp_path / "Song (3).mp3"


# ------------------------------------------------------------ the service


class Case:
    def __init__(self, tmp_path: Path, admin: bool = True) -> None:
        self.music = tmp_path / "music"
        self.settings = FilesSettings(str(self.music))
        self.db = Database(tmp_path / "pv.db")
        self.db.migrate()
        self.box = SecretBox(generate_key())
        self.nd = FakeNavidromeClient({"admin": ("pw", True)})
        with self.db.transaction() as conn:
            if admin:
                UsersStore().upsert_on_login(
                    conn, "admin", self.box.encrypt("pw"), True, NOW
                )
            uid = UsersStore().upsert_on_login(conn, "bob", "x", False, NOW).id
            self.song = SongsStore().create(conn, SongFields(
                "Feather", "Nujabes featuring Cise Starr & Akin from CYNE",
                query_title="Feather", main_artist="Nujabes", duration_s=175,
            ))  # fmt: skip
            self.req = (
                RequestsStore()
                .create(conn, uid, "catalog", NOW, song_id=self.song.id)
                .id
            )
        self.svc = FilesService(self.db, self.nd, self.box, self.settings, lambda: NOW,
                                lambda s: None)  # fmt: skip

    def download(self, rel: str, seconds: float = 175) -> int:
        wav(self.music / "downloads/complete/soulseek" / rel, seconds)
        with self.db.transaction() as conn:
            d = DownloadsStore().create(conn, self.req, "slskd", NOW)
            DownloadsStore().complete(conn, d.id, rel, NOW)
            f = FilesStore().for_song(conn, self.song.id)
        assert f is not None
        return f.id

    def file(self, fid: int):
        with self.db.transaction() as conn:
            return FilesStore().get(conn, fid)

    def navidrome_scans(self, fid: int) -> None:
        """What a real scan does: the moved file shows up in Navidrome's index."""
        rec = self.file(fid)
        rel = str(Path(rec.file_location).relative_to("library"))
        self.nd.pending.append(NdSong(f"nd-{len(self.nd.songs) + 1}", rel, "/music/library",
                                      "Feather", "Nujabes", "", 175, 1, "wav", False))  # fmt: skip


def test_verify_then_move_then_index(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/04 Feather.wav")
    assert c.svc.verify(fid) and c.file(fid).file_state == "verified"
    assert c.file(fid).verification == "success"
    assert c.svc.move(fid)
    rec = c.file(fid)
    assert (
        rec.file_state == "moved" and rec.file_location == "library/Nujabes/Feather.wav"
    )
    assert (c.music / rec.file_location).is_file()
    assert not (
        c.music / "downloads/complete/soulseek/post-vinyl/1/1/04 Feather.wav"
    ).exists()
    c.navidrome_scans(fid)
    assert c.svc.index(fid)
    rec = c.file(fid)
    assert (
        rec.file_state == "indexed" and rec.navidrome_id == "nd-1" and c.nd.scans == 1
    )


@pytest.mark.parametrize(("seconds", "error"), [(60, "duration"), (None, "not_found")])
def test_failed_verification_keeps_the_file_downloaded(
    tmp_path: Path, seconds: float | None, error: str
) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/x.wav", seconds or 175)
    if seconds is None:
        (c.music / "downloads/complete/soulseek/post-vinyl/1/1/x.wav").unlink()
    assert not c.svc.verify(fid)
    rec = c.file(fid)
    assert rec.file_state == "downloaded" and rec.verification == error
    assert not c.svc.move(fid)  # only verified/tagged files move


def test_not_audio_fails(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/x.wav")
    (c.music / "downloads/complete/soulseek/post-vinyl/1/1/x.wav").write_text(
        "not audio"
    )
    assert not c.svc.verify(fid) and c.file(fid).verification == "not_audio"


def test_never_overwrites_another_file(tmp_path: Path) -> None:
    c = Case(tmp_path)
    other = c.music / "library/Nujabes/Feather.wav"
    wav(other, 1)
    fid = c.download("post-vinyl/1/1/x.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    assert c.file(fid).file_location == "library/Nujabes/Feather (2).wav"
    assert other.stat().st_size == 8044  # untouched


def test_replacement_deletes_the_old_copy_first(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    c.navidrome_scans(fid)
    c.svc.index(fid)
    old = c.music / c.file(fid).file_location
    c.download("post-vinyl/2/5/b.wav")  # a better copy: the trigger keeps navidrome_id
    assert c.file(fid).navidrome_id == "nd-1" and c.file(fid).file_state == "downloaded"
    c.svc.verify(fid)
    c.svc.move(fid)
    rec = c.file(fid)
    assert rec.file_location == "library/Nujabes/Feather.wav"  # its old place, now free
    assert old == c.music / rec.file_location and old.is_file()
    assert rec.navidrome_id is None  # relinked by the next index


def test_old_copy_outside_the_library_is_kept(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    outside = tmp_path / "elsewhere.wav"
    wav(outside, 1)
    c.nd.songs["nd-9"] = NdSong("nd-9", "../../elsewhere.wav", "/music/library", "x", "y",
                                "", 1, 1, "wav", False)  # fmt: skip
    with c.db.transaction() as conn:
        FilesStore().set_navidrome_id(conn, fid, "nd-9", NOW)
    c.svc.verify(fid)
    c.svc.move(fid)
    assert outside.is_file()


def test_not_listed_yet_stays_moved(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    assert not c.svc.index(fid) and c.file(fid).file_state == "moved"


def test_navidrome_needs_an_admin(tmp_path: Path) -> None:
    c = Case(tmp_path, admin=False)
    fid = c.download("post-vinyl/1/1/a.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    with pytest.raises(NoAdmin):
        c.svc.index(fid)


def test_expired_session_logs_in_again(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    c.navidrome_scans(fid)
    c.svc.index(fid)  # logged in once
    with c.db.transaction() as conn:
        FilesStore().set_state(conn, fid, "moved", NOW)
    c.nd.users["admin"] = ("pw", True)
    assert c.svc._session is not None
    c.svc._session.username = "gone"  # the fake rejects it: like an expired session
    assert c.svc.index(fid)
