"""File pipeline, missing check, slskd cleanup, re-dispatch, workers (#105)."""

from __future__ import annotations

import wave
from dataclasses import replace
from pathlib import Path

from app.clients.navidrome import Song as NdSong
from app.clients.navidrome_fake import FakeNavidromeClient
from app.clients.slskd_fake import FakeSlskdClient
from app.config import FilesSettings, WorkersSettings
from app.core import states
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
from app.services.pipeline import FilePipeline
from app.services.slskd_cleanup import SlskdCleanup
from app.services.tagging import TaggingServiceBase
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.files import FileRecord, FilesStore
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore
from app.workers.core_loop import CoreLoopWorkers

NOW = 1_800_000_000


class Clock:
    def __init__(self) -> None:
        self.t = float(NOW)

    def __call__(self) -> float:
        return self.t


def wav(path: Path, seconds: float = 175) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * int(8000 * seconds))


class StubTagging(TaggingServiceBase):
    """Succeeds or fails on demand, writing what the real one writes in the DB."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock, self.fail, self.calls = db, clock, False, 0

    def tag(self, file_id: int) -> bool:
        self.calls += 1
        with self.db.transaction(write=True) as conn:
            store = FilesStore()
            if self.fail:
                store.add_tagging(
                    conn, file_id, "failTag", int(self.clock()), error="x"
                )
                return False
            store.add_tagging(conn, file_id, "tagged", int(self.clock()))
            store.set_state(
                conn, file_id, states.file_tagged("verified"), int(self.clock())
            )
        return True


class ScanCounting(FakeNavidromeClient):
    """Indexes whatever the pipeline moved into the library on each scan."""

    def __init__(self, library: Path) -> None:
        super().__init__({"admin": ("pw", True)})
        self.library, self.hide = library, False

    def start_scan(self, session, full=False):
        if not self.hide:
            for p in self.library.rglob("*.wav"):
                rel = str(p.relative_to(self.library))
                if not any(s.path == rel for s in self.songs.values()):
                    sid = f"nd-{len(self.songs) + 1}"
                    self.songs[sid] = NdSong(sid, rel, "/music/library", p.stem, "", "", 175,
                                             1, "wav", False)  # fmt: skip
        return super().start_scan(session, full)


class Case:
    def __init__(self, tmp_path: Path) -> None:
        self.music = tmp_path / "music"
        self.settings = FilesSettings(str(self.music))
        self.dl = self.music / self.settings.slskd_downloads_dir
        self.db = Database(tmp_path / "pv.db")
        self.db.migrate()
        self.clock = Clock()
        box = SecretBox(generate_key())
        self.nd = ScanCounting(self.music / "library")
        with self.db.transaction() as conn:
            self.uid = UsersStore().upsert_on_login(conn, "admin", box.encrypt("pw"), True,
                                                    NOW).id  # fmt: skip
        self.files = FilesService(self.db, self.nd, box, self.settings, self.clock,
                                  lambda s: None)  # fmt: skip
        self.tagging = StubTagging(self.db, self.clock)
        self.pipe = FilePipeline(self.db, self.files, self.tagging, WorkersSettings(),
                                 self.clock)  # fmt: skip
        self.n = 0

    def download(self, title: str = "Feather", seconds: float = 175) -> int:
        self.n += 1
        rel = f"post-vinyl/{self.n}/{self.n}/{title}.wav"
        wav(self.dl / rel, seconds)
        with self.db.transaction() as conn:
            song = SongsStore().create(conn, SongFields(title, "Nujabes", query_title=title,
                                       main_artist="Nujabes", duration_s=175))  # fmt: skip
            req = RequestsStore().create(
                conn, self.uid, "catalog", NOW, song_id=song.id
            )
            d = DownloadsStore().create(conn, req.id, "slskd", NOW)
            DownloadsStore().complete(conn, d.id, rel, int(self.clock()))
            f = FilesStore().for_song(conn, song.id)
        assert f is not None
        return f.id

    def rec(self, fid: int) -> FileRecord:
        with self.db.transaction() as conn:
            r = FilesStore().get(conn, fid)
        assert r is not None
        return r


# ------------------------------------------------------------ the pipeline


def test_new_downloads_go_all_the_way_with_one_scan(tmp_path: Path) -> None:
    c = Case(tmp_path)
    a, b = c.download("Feather"), c.download("Luv Sic")
    out = c.pipe.tick()
    assert (out.verified, out.tagged, out.moved, out.indexed) == (2, 2, 2, 2)
    assert c.nd.scans == 1  # one scan for the whole batch
    assert c.rec(a).file_state == c.rec(b).file_state == "indexed"
    assert c.pipe.tick().indexed == 0 and c.nd.scans == 1  # nothing left: no scan


def test_failed_tag_retries_once_after_an_hour_then_moves_untagged(
    tmp_path: Path,
) -> None:
    c = Case(tmp_path)
    c.tagging.fail = True
    fid = c.download()
    c.pipe.tick()
    assert c.rec(fid).file_state == "verified" and c.tagging.calls == 1
    c.clock.t += 59 * 60
    c.pipe.tick()
    assert c.tagging.calls == 1  # still waiting
    c.clock.t += 2 * 60
    c.pipe.tick()
    assert c.tagging.calls == 2 and c.rec(fid).file_state == "verified"
    c.pipe.tick()
    assert c.tagging.calls == 2  # no third try
    assert c.rec(fid).file_state == "indexed"
    assert c.rec(fid).file_location == "library/Nujabes/Feather.wav"  # untagged layout


def test_retry_succeeds(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.tagging.fail = True
    fid = c.download()
    c.pipe.tick()
    c.tagging.fail = False
    c.clock.t += 61 * 60
    c.pipe.tick()
    assert c.rec(fid).file_state == "indexed" and c.tagging.calls == 2


def test_earlier_files_failed_tags_dont_count(tmp_path: Path) -> None:
    c = Case(tmp_path)
    c.tagging.fail = True
    fid = c.download()
    c.pipe.tick()
    c.clock.t += 61 * 60
    c.pipe.tick()
    c.pipe.tick()  # moved untagged after two failures
    with c.db.transaction() as conn:  # a better copy of the same song arrives
        song_id = c.rec(fid).song_id
        req = RequestsStore().latest_for_song(conn, song_id)
        assert req is not None
        d = DownloadsStore().create(conn, req.id, "slskd", NOW)
        wav(c.dl / "post-vinyl/9/9/new.wav")
        DownloadsStore().complete(
            conn, d.id, "post-vinyl/9/9/new.wav", int(c.clock()) + 1
        )
    c.clock.t += 2
    c.tagging.fail = False
    c.pipe.tick()
    assert c.tagging.calls == 3 and c.rec(fid).file_state == "indexed"


def test_not_listed_yet_is_linked_on_a_later_tick(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download()
    c.nd.hide = True
    c.pipe.tick()
    assert c.rec(fid).file_state == "moved"
    c.nd.hide = False
    c.pipe.tick()
    assert c.rec(fid).file_state == "indexed"


def test_unverifiable_waits_a_day_then_is_deleted(tmp_path: Path) -> None:
    c = Case(tmp_path)
    fid = c.download(seconds=30)  # wrong length: fails verification
    c.pipe.tick()
    assert c.rec(fid).verification == "duration"
    assert c.pipe.discard_unverifiable() == 0
    c.clock.t += 25 * 3600
    assert c.pipe.discard_unverifiable() == 1
    assert (
        c.rec(fid).file_state == "missing"
        and not (c.dl / c.rec(fid).file_location).exists()
    )
    assert c.rec(fid).verification == "duration"  # the reason stays


def test_missing_files(tmp_path: Path) -> None:
    c = Case(tmp_path)
    gone_early = c.download("A")
    (c.dl / c.rec(gone_early).file_location).unlink()
    indexed = c.download("B")
    c.pipe.tick()
    assert c.rec(indexed).file_state == "indexed"
    nd_id = c.rec(indexed).navidrome_id or ""
    c.nd.songs[nd_id] = replace(c.nd.songs[nd_id], missing=True)  # deleted outside PV
    assert c.pipe.check_missing() == 2
    assert c.rec(gone_early).file_state == c.rec(indexed).file_state == "missing"
    assert c.pipe.check_missing() == 0


# ------------------------------------------------------------ slskd cleanup


def test_cleanup(tmp_path: Path) -> None:
    c = Case(tmp_path)
    slskd = FakeSlskdClient()
    cleanup = SlskdCleanup(c.db, slskd, c.settings)
    stranger = slskd.queue_download("stranger", [("x.flac", 1)]).transfers[0].id
    with c.db.transaction() as conn:
        uid = c.uid
        song = SongsStore().create(conn, SongFields("S", "A"))
        req = RequestsStore().create(conn, uid, "catalog", NOW, song_id=song.id)
        d = DownloadsStore().create(conn, req.id, "slskd", NOW)
        store = SlskdStore()
        sid = "s1"
        store.add_search(conn, sid, req.id, "q", "L0", NOW)
        attempts = {}
        for name, outcome in (("done-moved", "completed"), ("done-waiting", "completed"),
                              ("failed", "tooSlow")):  # fmt: skip
            a = store.add_attempt(conn, d.id, sid, 0, name, f"{name}.flac", 1, NOW)
            t = slskd.queue_download(name, [(f"{name}.flac", 1)]).transfers[0].id
            store.set_transfer_id(conn, a.id, t)
            store.end_attempt(conn, a.id, outcome, NOW)
            attempts[name] = (a.id, t)
    root = c.dl / "post-vinyl" / str(d.id)
    (root / str(attempts["done-moved"][0])).mkdir(
        parents=True
    )  # its file was moved out
    (root / str(attempts["done-waiting"][0])).mkdir()
    (root / str(attempts["done-waiting"][0]) / "done-waiting.flac").write_text("x")
    (root / str(attempts["failed"][0])).mkdir()
    (root / str(attempts["failed"][0]) / "partial.flac").write_text("x")
    assert cleanup.tick() == 2
    left = set(slskd.transfers)
    assert attempts["done-moved"][1] not in left and attempts["failed"][1] not in left
    assert (
        attempts["done-waiting"][1] in left and stranger in left
    )  # waiting / not ours
    assert not (root / str(attempts["failed"][0])).exists()
    assert not (root / str(attempts["done-moved"][0])).exists()  # empty: removed
    assert (root / str(attempts["done-waiting"][0]) / "done-waiting.flac").exists()
    assert cleanup.tick() == 0  # idempotent


# ------------------------------------------------------------ workers


class Recording:
    def __init__(self) -> None:
        self.calls: list[str] = []


def test_core_loop_resumes_then_starts_workers(tmp_path: Path) -> None:
    from app.services.dispatch_fake import FakeDownloadDispatch, FakeRequest

    c = Case(tmp_path)
    dispatch = FakeDownloadDispatch(
        requests={1: FakeRequest(1, "searching"), 2: FakeRequest(1, "requested")},
        enabled=["slskd"],
    )
    polls: list[int] = []
    loop = CoreLoopWorkers(WorkersSettings(), dispatch, c.pipe, lambda: polls.append(1),
                           lambda: None)  # fmt: skip
    loop.start()
    loop.stop()
    assert [r for _, r in dispatch.started] == [1, 2]  # resumed, then the waiting one
    assert {w.name for w in loop.workers} == {
        "download-monitor", "file-pipeline", "cleanup", "missing-check", "redispatch",
    }  # fmt: skip
