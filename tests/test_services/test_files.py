"""Files service: verify, layout, move, index, replaced copies (#103)."""

from __future__ import annotations

import errno
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from app.clients.navidrome import Song as NdSong
from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import FilesSettings
from app.services.encryption import SecretBox, generate_key
from app.services.files import (
    FilesService,
    LayoutMismatch,
    LibraryFileNotFound,
    NoAdmin,
    free_path,
    layout,
    move_file,
    segment,
)
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


def cross_device(src: object, dst: object) -> None:
    raise OSError(errno.EXDEV, "cross-device link")


def test_move_across_mounts_copies_then_removes_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "rename", cross_device)
    src, target = tmp_path / "a.flac", tmp_path / "b.flac"
    src.write_bytes(b"audio")
    move_file(src, target)
    assert target.read_bytes() == b"audio"
    assert not src.exists()
    assert not (tmp_path / "b.flac.partial").exists()


def test_failed_copy_across_mounts_keeps_the_source_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(*_: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "rename", cross_device)
    monkeypatch.setattr(shutil, "copyfileobj", disk_full)
    src, target = tmp_path / "a.flac", tmp_path / "b.flac"
    src.write_bytes(b"audio")
    with pytest.raises(OSError):
        move_file(src, target)
    assert src.read_bytes() == b"audio"
    assert [p.name for p in tmp_path.iterdir()] == ["a.flac"]


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


def test_a_cut_off_file_whose_header_claims_the_full_length_fails(
    tmp_path: Path,
) -> None:
    """Live #114: an MP3 cut to 4 KB still read as 306 s from its Xing header."""
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/x.wav")  # the song is 175 s
    frame = b"\xff\xfb\x90\x64"  # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz
    xing = (
        frame + b"\x00" * 32 + b"Xing" + struct.pack(">III", 3, 6699, 6699 * 417)
    )  # 175 s
    path = c.music / "downloads/complete/soulseek/post-vinyl/1/1/x.wav"
    path.write_bytes(xing.ljust(417, b"\x00") + (frame + b"\x00" * 413) * 3)
    assert not c.svc.verify(fid) and c.file(fid).verification == "truncated"


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


# ------------------------------------------------------------ Navidrome's root


def wide_song(c: Case, rel: str = "library/A/B/1.wav") -> NdSong:
    """A song as Navidrome reports it when its root is the music folder."""
    song = NdSong("nd-1", rel, str(c.music), "t", "a", "", 1, 1, "wav", False)
    c.nd.songs[song.id] = song
    return song


def test_file_found_with_navidrome_at_the_music_folder(tmp_path: Path) -> None:
    c = Case(tmp_path)
    wav(c.music / "library/A/B/1.wav", 1)
    assert c.svc.library_path(wide_song(c)) == (c.music / "library/A/B/1.wav").resolve()


def test_the_reported_root_wins_over_a_same_named_file(tmp_path: Path) -> None:
    c = Case(tmp_path)
    wav(c.music / "library/x.wav", 1)
    wav(c.music / "library/library/x.wav", 1)
    assert (
        c.svc.library_path(wide_song(c, "library/x.wav"))
        == (c.music / "library/x.wav").resolve()
    )


@pytest.mark.parametrize("rel", ["downloads/x.wav", "../../outside.wav"])
def test_wider_root_still_stays_inside_the_library(tmp_path: Path, rel: str) -> None:
    c = Case(tmp_path)
    wav(c.music / "downloads/x.wav", 1)
    wav(tmp_path / "outside.wav", 1)
    with pytest.raises(LibraryFileNotFound):
        c.svc.library_path(wide_song(c, rel))


def test_index_finds_the_file_under_a_wider_root(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    c.svc.verify(fid)
    c.svc.move(fid)
    rel = c.file(fid).file_location
    c.nd.pending.append(NdSong("nd-1", rel, str(c.music), "Feather", "Nujabes", "",
                               175, 1, "wav", False))  # fmt: skip
    assert c.svc.index(fid) and c.file(fid).navidrome_id == "nd-1"


def test_replaced_copy_is_deleted_under_a_wider_root(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download("post-vinyl/1/1/a.wav")
    old = c.music / "library/Old/old.wav"
    wav(old, 1)
    wide_song(c, "library/Old/old.wav")
    with c.db.transaction() as conn:
        FilesStore().set_navidrome_id(conn, fid, "nd-1", NOW)
    c.svc.verify(fid)
    c.svc.move(fid)
    assert not old.exists()


def test_layout_check_names_mounts_that_disagree(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.svc.check_layout()  # an empty library has nothing to disagree on
    wide_song(c, "elsewhere/x.wav")
    with pytest.raises(LayoutMismatch):
        c.svc.check_layout()
    wav(c.music / "library/ok.wav", 1)
    c.nd.songs["nd-2"] = NdSong("nd-2", "ok.wav", "/music/library", "t", "a", "", 1, 1,
                                "wav", False)  # fmt: skip
    c.svc.check_layout()  # one missing file isn't a mismatch


def test_layout_check_waits_for_setup(tmp_path: Path) -> None:
    Case(tmp_path, admin=False).svc.check_layout()


def test_wider_root_adds_downloads_to_ndignore_keeping_its_lines(
    tmp_path: Path,
) -> None:
    c = Case(tmp_path)
    (c.music / ".ndignore").parent.mkdir(parents=True, exist_ok=True)
    (c.music / ".ndignore").write_text("# mine\n/complete/")
    wide_song(c)
    c.svc.scan()
    c.svc.scan()
    assert (c.music / ".ndignore").read_text() == "# mine\n/complete/\n/downloads/\n"


def test_first_scan_of_an_empty_navidrome_still_writes_ndignore(
    tmp_path: Path,
) -> None:
    c = Case(tmp_path)
    c.nd.pending.append(NdSong("nd-1", "library/a.wav", str(c.music), "t", "a", "", 1,
                               1, "wav", False))  # fmt: skip
    (c.music).mkdir(parents=True)
    c.svc.scan()
    assert (c.music / ".ndignore").read_text() == "/downloads/\n"


def test_library_root_writes_no_ndignore(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.nd.songs["nd-1"] = NdSong("nd-1", "x.wav", "/music/library", "t", "a", "", 1, 1,
                                "wav", False)  # fmt: skip
    c.svc.scan()
    assert not (c.music / ".ndignore").exists()


def test_prune_clears_leftovers_but_keeps_audio_and_other_files(
    tmp_path: Path,
) -> None:
    c = Case(tmp_path)
    junk = c.music / "library/A/B"
    junk.mkdir(parents=True)
    (junk / "cover.jpg").write_bytes(b"x")
    (junk / ".DS_Store").write_bytes(b"x")
    (junk / "._a.flac").write_bytes(b"x")
    c.svc._prune(junk)
    assert not (c.music / "library/A").exists() and (c.music / "library").is_dir()
    kept = c.music / "library/C"
    kept.mkdir()
    (kept / "cover.jpg").write_bytes(b"x")
    (kept / "booklet.pdf").write_bytes(b"x")
    c.svc._prune(kept)
    assert (kept / "cover.jpg").is_file()


# ------------------------------------------------------------ stars across a retag


def test_stars_and_ratings_follow_a_song_to_its_new_id(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.nd.users["bob"] = ("bobpw", False)
    with c.db.transaction(write=True) as conn:
        UsersStore().upsert_on_login(conn, "bob", c.box.encrypt("bobpw"), False, NOW)
    wav(c.music / "library/old/a.wav", 1)
    c.nd.songs["old"] = NdSong("old", "old/a.wav", "/music/library", "t", "a", "", 1, 1,
                               "wav", False)  # fmt: skip
    c.nd.user_marks[("admin", "old")] = (True, 0)
    c.nd.user_marks[("bob", "old")] = (False, 4)
    target = "a/t.wav"
    c.nd.pending.append(NdSong("new", target, "/music/library", "t", "a", "", 1, 1,
                               "wav", False))  # fmt: skip
    new = c.svc.re_place("old", "t", "a")
    assert new is not None and new.id == "new"
    assert c.nd.user_marks[("admin", "new")] == (True, 0)
    assert c.nd.user_marks[("bob", "new")] == (False, 4)


def test_a_user_whose_login_fails_doesnt_stop_the_retag(tmp_path: Path) -> None:
    c = Case(tmp_path)
    wav(c.music / "library/old/a.wav", 1)
    c.nd.songs["old"] = NdSong("old", "old/a.wav", "/music/library", "t", "a", "", 1, 1,
                               "wav", False)  # fmt: skip
    c.nd.user_marks[("admin", "old")] = (True, 0)  # bob's stored secret is junk
    c.nd.pending.append(NdSong("new", "a/t.wav", "/music/library", "t", "a", "", 1, 1,
                               "wav", False))  # fmt: skip
    assert c.svc.re_place("old", "t", "a") is not None
    assert c.nd.user_marks[("admin", "new")] == (True, 0)
