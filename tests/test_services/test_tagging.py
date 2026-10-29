"""Tagging: release choice, tags, writer, service flow (#104)."""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import COMM, ID3, TALB, TXXX, USLT

from app.clients.coverart import FakeCoverArt
from app.clients.listenbrainz import RecordingMetadata
from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.clients.musicbrainz import (
    RecordingInfo,
    RecordingRelease,
    ReleaseInfo,
    ReleaseTrack,
)
from app.clients.musicbrainz_fake import FakeMusicBrainzTags
from app.config import FilesSettings
from app.core.release_choice import choose_release
from app.services.encryption import SecretBox, generate_key
from app.services.files import layout
from app.services.sources import SourcePreferences
from app.services.tagging import TagFailed, TaggingService, build_tags, write_tags
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import Song, SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
REC = "5fffe791-c8fd-440a-b301-d511d8b9c31f"
COVER = b"\xff\xd8\xff\xe0fake-jpeg"


def rel(id: str, date: str, status: str = "Official", kind: str = "Album",
        sec: tuple[str, ...] = (), title: str = "") -> RecordingRelease:  # fmt: skip
    return RecordingRelease(id, title or id, date, status, "JP", f"rg-{id}", kind, sec)


def pick(releases: Sequence[RecordingRelease]) -> str | None:
    chosen = choose_release(releases)
    return chosen.id if chosen else None


# Feather's real releases (MusicBrainz, 2026-10-29)
FEATHER = [
    rel("modal-2005", "2005-11-11", title="Modal Soul"),
    rel("boot-2013", "2013-06-18", "Bootleg", sec=("Compilation",)),
    rel("free-2014", "2014-12-17", sec=("Compilation",)),
    rel("modal-2017", "2017-12-13"),
    rel("vinyl-2020", "2020-02-26"),
]


# ------------------------------------------------------------ release choice


def test_feather_tags_as_the_2005_album() -> None:
    assert pick(FEATHER) == "modal-2005"


def test_album_first_then_ep_then_single() -> None:
    single, album = rel("s", "2005-01-01", kind="Single"), rel("a", "2005-06-01")
    assert pick([single, album]) == "a"  # an earlier lead single still loses
    assert pick([single, rel("e", "2006-01-01", kind="EP")]) == "e"
    assert pick([single]) == "s"


def test_fallbacks_and_dates() -> None:
    ost = rel("ost", "1999", sec=("Soundtrack",))
    assert pick([ost, rel("boot", "1990", "Bootleg")]) == "ost"  # any official
    assert pick([rel("boot", "1990", "Bootleg")]) is None
    assert pick([rel("x", ""), rel("y", "2001")]) == "y"  # unknown dates last


# ------------------------------------------------------------ tags


def info(isrcs: tuple[str, ...] = ("JPH491605000",)) -> RecordingInfo:
    credits = (
        ("Nujabes", " featuring "),
        ("Cise Starr", " & "),
        ("Akin from CYNE", ""),
    )
    return RecordingInfo(REC, "Feather", credits, ("a1", "a2", "a3"), 175.0, isrcs,
                         tuple(FEATHER))  # fmt: skip


def release(rid: str = "modal-2005", discs: int = 1) -> ReleaseInfo:
    tracks = (ReleaseTrack("t-other", "rec-x", "1", 1, 1),
              ReleaseTrack("t-feather", REC, "4", 4, discs))  # fmt: skip
    per_disc = {d: 14 for d in range(1, discs + 1)}
    return ReleaseInfo(rid, "Modal Soul", "2005-11-11", f"rg-{rid}", "2005-11-11",
                       "Nujabes", ("a1",), per_disc, tracks, True)  # fmt: skip


def test_build_tags() -> None:
    tags = build_tags(info(), release(discs=2))
    assert tags == {
        "title": "Feather",
        "artist": "Nujabes featuring Cise Starr & Akin from CYNE",
        "albumartist": "Nujabes", "album": "Modal Soul",
        "date": "2005-11-11", "originaldate": "2005-11-11",
        "tracknumber": 4, "tracktotal": 14, "discnumber": 2, "disctotal": 2,
        "musicbrainz_trackid": REC, "musicbrainz_releasetrackid": "t-feather",
        "musicbrainz_albumid": "modal-2005", "musicbrainz_releasegroupid": "rg-modal-2005",
        "musicbrainz_artistid": ["a1", "a2", "a3"], "musicbrainz_albumartistid": ["a1"],
        "isrc": "JPH491605000",
    }  # fmt: skip


def test_isrc_only_when_exactly_one_and_recording_must_be_on_the_release() -> None:
    assert "isrc" not in build_tags(info(("A", "B")), release())
    assert "isrc" not in build_tags(info(()), release())
    other = ReleaseInfo("r", "X", "", "", "", "", (), {1: 1},
                        (ReleaseTrack("t", "rec-x", "1", 1, 1),), False)  # fmt: skip
    with pytest.raises(TagFailed, match="not_on_release"):
        build_tags(info(), other)


# ------------------------------------------------------------ writer


def flac(path: Path) -> Path:
    """The smallest valid FLAC: the marker and a STREAMINFO block (44.1 kHz, 16 bit)."""
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6
    rate_ch_bits = (44100 << 12) | (1 << 9) | (15 << 4)  # 20 bits rate, 3 ch-1, 5 bps-1
    info += struct.pack(">I", rate_ch_bits)[0:4] + b"\x00\x00\x00\x00" + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info[:34])
    return path


def mp3(path: Path) -> Path:
    """Silent MPEG-1 Layer III frames (128 kbit/s, 44.1 kHz)."""
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    path.write_bytes(frame * 40)
    return path


def test_flac_keeps_replaygain_and_lyrics_replaces_the_rest(tmp_path: Path) -> None:
    p = flac(tmp_path / "a.flac")
    peer = FLAC(p)
    peer["album"] = "WRONG [www.site.ru]"
    peer["comment"] = "ripped by someone"
    peer["replaygain_track_gain"] = "-6.5 dB"
    peer["lyrics"] = "la la"
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/png", b"peer art"
    peer.add_picture(pic)
    peer.save()
    write_tags(p, build_tags(info(), release()), COVER)
    out = FLAC(p)
    assert out["album"] == ["Modal Soul"] and "comment" not in out
    assert out["replaygain_track_gain"] == ["-6.5 dB"] and out["lyrics"] == ["la la"]
    assert out["musicbrainz_artistid"] == ["a1", "a2", "a3"]
    assert [x.data for x in out.pictures] == [COVER]  # the peer's art is gone


def test_mp3_keeps_replaygain_and_lyrics_replaces_the_rest(tmp_path: Path) -> None:
    p = mp3(tmp_path / "a.mp3")
    peer = ID3()
    peer.add(TALB(encoding=3, text="WRONG"))
    peer.add(COMM(encoding=3, lang="eng", desc="", text="ripped by someone"))
    peer.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-6.5 dB"))
    peer.add(USLT(encoding=3, lang="eng", desc="", text="la la"))
    peer.save(p)
    write_tags(p, build_tags(info(), release(discs=2)), COVER)
    out = ID3(p)
    assert str(out["TALB"]) == "Modal Soul" and not out.getall("COMM")
    assert str(out["TXXX:REPLAYGAIN_TRACK_GAIN"]) == "-6.5 dB" and out.getall("USLT")
    assert str(out["TRCK"]) == "4/14" and str(out["TPOS"]) == "2/2"
    assert out["UFID:http://musicbrainz.org"].data == REC.encode()
    assert out.getall("APIC")[0].data == COVER


def test_unsupported_format(tmp_path: Path) -> None:
    import wave

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * 800)
    with pytest.raises(TagFailed, match="format"):
        write_tags(p, {"title": "x"}, None)


def test_broken_file_is_a_fail_tag(tmp_path: Path) -> None:
    c = Case(tmp_path)
    (c.dl / "post-vinyl/1/1/a.flac").write_bytes(b"fLaC broken")
    assert not c.svc.tag(c.fid)
    state, rows = c.state_and_rows()
    assert state == "verified" and rows[0].startswith("failTag:")


def test_files_layout_reads_what_tagging_wrote(tmp_path: Path) -> None:
    import mutagen

    p = flac(tmp_path / "a.flac")
    write_tags(p, build_tags(info(), release(discs=2)), None)
    song = Song(1, "Feather", "x", "Feather", "Nujabes", None, None, None, None, 175)
    tags = mutagen.File(p, easy=True).tags
    assert layout(tags, song, "flac", tagged=True) == Path(
        "Nujabes/Modal Soul (2005)/Disc 2/04 Feather.flac"
    )


# ------------------------------------------------------------ the service


class Case:
    def __init__(
        self, tmp_path: Path, *, mbid: bool = True, token: bool = True
    ) -> None:
        self.settings = FilesSettings(str(tmp_path / "music"))
        self.dl = tmp_path / "music" / self.settings.slskd_downloads_dir
        self.db = Database(tmp_path / "pv.db")
        self.db.migrate()
        self.box = SecretBox(generate_key())
        self.mb, self.lb, self.caa = (
            FakeMusicBrainzTags(),
            FakeListenBrainzClient(),
            FakeCoverArt(),
        )
        self.mb.recordings[REC] = info()
        self.mb.releases["modal-2005"] = release()
        self.caa.covers["modal-2005"] = COVER
        with self.db.transaction() as conn:
            u = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW)
            if token:
                UsersStore().set_lb_token(conn, u.id, self.box.encrypt("tok"))
            fields = SongFields("Feather", "Nujabes", query_title="Feather",
                                main_artist="Nujabes", duration_s=175)  # fmt: skip
            song = (SongsStore().find_or_create(conn, fields, {"musicbrainz": REC})
                    if mbid else SongsStore().create(conn, fields))  # fmt: skip
            r = RequestsStore().create(conn, u.id, "catalog", NOW, song_id=song.id)
            d = DownloadsStore().create(conn, r.id, "slskd", NOW)
            DownloadsStore().complete(conn, d.id, "post-vinyl/1/1/a.flac", NOW)
            f = FilesStore().for_song(conn, song.id)
            assert f is not None
            FilesStore().set_state(conn, f.id, "verified", NOW)
            self.fid = f.id
        self.dl.joinpath("post-vinyl/1/1").mkdir(parents=True)
        flac(self.dl / "post-vinyl/1/1/a.flac")
        self.svc = TaggingService(self.db, self.mb, self.lb, self.caa, self.box,
                                  self.settings, lambda: NOW)  # fmt: skip

    def state_and_rows(self) -> tuple[str, list[str]]:
        with self.db.transaction() as conn:
            f = FilesStore().get(conn, self.fid)
            rows = FilesStore().tagging_for_file(conn, self.fid)
        assert f is not None
        return f.file_state, [f"{r.status}:{r.error or ''}" for r in rows]


def test_tags_a_verified_file_from_its_mbid(tmp_path: Path) -> None:
    c = Case(tmp_path)
    assert c.svc.tag(c.fid)
    assert c.state_and_rows() == ("tagged", ["tagged:"])
    out = FLAC(c.dl / "post-vinyl/1/1/a.flac")
    assert out["album"] == ["Modal Soul"] and out.pictures[0].data == COVER
    with c.db.transaction() as conn:
        (row,) = FilesStore().tagging_for_file(conn, c.fid)
    assert row.mb_release_id == "modal-2005" and row.tags_written
    assert row.tags_written["cover"] is True


def test_fail_tag_leaves_the_file_verified(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.mb.recordings[REC] = RecordingInfo(REC, "Feather", (), (), None, (),
                                         (rel("b", "1990", "Bootleg"),))  # fmt: skip
    assert not c.svc.tag(c.fid)
    assert c.state_and_rows() == ("verified", ["failTag:no_release"])


@pytest.mark.parametrize("why", ["release_lookup_fails", "song_not_on_tracklist"])
def test_no_disc_details_still_tags_the_album(tmp_path: Path, why: str) -> None:
    """User, 2026-10-29: put the song in its album folder rather than fail."""
    import mutagen

    c = Case(tmp_path)
    if why == "release_lookup_fails":
        del c.mb.releases["modal-2005"]
    else:
        c.mb.releases["modal-2005"] = ReleaseInfo(
            "modal-2005", "Modal Soul", "2005-11-11", "rg", "2005", "Nujabes", (), {1: 1},
            (ReleaseTrack("t", "rec-x", "1", 1, 1),), True,
        )  # fmt: skip
    assert c.svc.tag(c.fid)
    tags = mutagen.File(c.dl / "post-vinyl/1/1/a.flac", easy=True).tags
    assert tags["album"] == ["Modal Soul"] and "tracknumber" not in tags
    song = Song(1, "Feather", "x", "Feather", "Nujabes", None, None, None, None, 175)
    assert layout(tags, song, "flac", tagged=True) == Path(
        "Nujabes featuring Cise Starr & Akin from CYNE/Modal Soul (2005)/Feather.flac"
    )


def test_no_cover_still_tags(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.caa.covers.clear()
    assert c.svc.tag(c.fid)
    assert FLAC(c.dl / "post-vinyl/1/1/a.flac").pictures == []


class FixedLB(FakeListenBrainzClient):
    """Answers every lookup with one hit, as ListenBrainz's fuzzy lookup might."""

    def __init__(self, hit: RecordingMetadata | None) -> None:
        super().__init__()
        self.hit = hit

    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        return self.hit


def lb_hit(artist: str, length: float | None) -> RecordingMetadata:
    return RecordingMetadata(REC, "Feather", length, artist, (), None, (), (), ())


@pytest.mark.parametrize(
    ("token", "hit", "error"),
    [
        (False, lb_hit("Nujabes", 175.0), "no_listenbrainz_token"),
        (True, None, "no_recording"),
        (True, lb_hit("Someone Else", 175.0), "lookup_mismatch"),
        (True, lb_hit("Nujabes", 240.0), "lookup_mismatch"),
    ],
)
def test_lookup_without_mbid_fails_safely(
    tmp_path: Path, token: bool, hit: RecordingMetadata | None, error: str
) -> None:
    c = Case(tmp_path, mbid=False, token=token)
    c.svc._lb = FixedLB(hit)
    assert not c.svc.tag(c.fid)
    assert c.state_and_rows() == ("verified", [f"failTag:{error}"])


def test_lookup_without_mbid_succeeds(tmp_path: Path) -> None:
    c = Case(tmp_path, mbid=False)
    c.svc._lb = FixedLB(lb_hit("Nujabes feat. Cise Starr", 175.4))  # "A" in "A feat. B"
    assert c.svc.tag(c.fid)
    assert c.state_and_rows()[0] == "tagged"


def test_only_verified_files_are_tagged(tmp_path: Path) -> None:
    c = Case(tmp_path)
    with c.db.transaction() as conn:
        FilesStore().set_state(conn, c.fid, "moved", NOW)
    assert not c.svc.tag(c.fid)
    assert c.state_and_rows() == ("moved", [])


# ------------------------------------------------------------ sources off (user, 2026-10-30)


class Counting(FakeMusicBrainzTags):
    def __init__(self, base: FakeMusicBrainzTags) -> None:
        super().__init__()
        self.recordings, self.releases, self.calls = base.recordings, base.releases, 0

    def recording_info(self, recording_id: str) -> RecordingInfo:
        self.calls += 1
        return super().recording_info(recording_id)


class CountingLB(FixedLB):
    calls = 0

    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        self.calls += 1
        return super().lookup_recording(artist, title, token)


@pytest.mark.parametrize(
    ("off", "mbid", "error"),
    [
        ("musicbrainz", True, "musicbrainz_off"),
        ("listenbrainz", False, "listenbrainz_off"),
    ],
)
def test_nothing_is_sent_to_a_source_turned_off_for_the_requester(
    tmp_path: Path, off: str, mbid: bool, error: str
) -> None:
    c = Case(tmp_path, mbid=mbid)
    prefs = SourcePreferences(c.db, {"catalog": ("musicbrainz", "listenbrainz")})
    with c.db.transaction() as conn:
        alice = UsersStore().get_by_username(conn, "alice")
    assert alice is not None
    prefs.set_for_user(alice.id, "catalog", [], [off])
    mb, lb = Counting(c.mb), CountingLB(lb_hit("Nujabes", 175))
    svc = TaggingService(
        c.db, mb, lb, c.caa, c.box, c.settings, lambda: NOW, sources=prefs
    )
    assert not svc.tag(c.fid)
    assert c.state_and_rows() == ("verified", [f"failTag:{error}"])
    assert mb.calls == 0 and lb.calls == 0
