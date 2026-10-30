"""slskd download: attempts, queue wait, stall rule, bans, end states (#102)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.clients.slskd import AppState, PeerFile, PeerResponse, SlskdNotFound
from app.clients.slskd_fake import FakeSlskdClient
from app.config import SlskdSettings
from app.services.slskd_download import SlskdDownloader, local_name, transfer_phase
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.stores.database import Database
from app.stores.downloads import Download, DownloadsStore
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdAttempt, SlskdStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore
from app.workers.download_monitor import DownloadMonitor

NOW = 1_800_000_000
FILE = r"Music\Nujabes\Modal Soul\04 Feather.flac"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def peer(name: str, speed: int) -> PeerResponse:
    f = PeerFile(FILE, 20_000_000, "", 175, None, 16, 44100, False)
    return PeerResponse(name, True, speed, 0, (f,))


@dataclass
class Case:
    dl: SlskdDownloader
    slskd: FakeSlskdClient
    db: Database
    req: int
    mono: Clock
    wall: Clock

    def attempts(self) -> list[SlskdAttempt]:
        with self.db.transaction() as conn:
            (d,) = DownloadsStore().for_request(conn, self.req)
            return SlskdStore().attempts_for_download(conn, d.id)

    def download(self) -> Download:
        with self.db.transaction() as conn:
            (d,) = DownloadsStore().for_request(conn, self.req)
        return d

    def state(self) -> str:
        with self.db.transaction() as conn:
            r = RequestsStore().get(conn, self.req)
        assert r is not None
        return r.track_state

    def transfer(self, state: str, bytes_: int | None = None) -> None:
        """Set the running attempt's transfer at slskd."""
        tid = self.attempts()[-1].transfer_id
        assert tid is not None
        t = self.slskd.transfers[tid]
        self.slskd.transfers[tid] = replace(
            t,
            state=state,
            bytes_transferred=t.bytes_transferred if bytes_ is None else bytes_,
        )

    def tick(self, seconds: float) -> None:
        self.mono.t += seconds
        self.wall.t += seconds
        self.dl.poll()


def build(tmp_path: Path, peers: list[PeerResponse], **settings: int) -> Case:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    slskd = FakeSlskdClient({"feather nujabes": peers})
    mono, wall = Clock(1000.0), Clock(float(NOW))
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields(
            "Feather", "Nujabes", query_title="Feather", main_artist="Nujabes",
            duration_s=175,
        ))  # fmt: skip
        req = RequestsStore().create(conn, uid, "catalog", NOW, song_id=song.id)
        RequestsStore().set_track_state(conn, req.id, "found")
    sid = slskd.start_search("feather nujabes", 600, 30)
    with db.transaction() as conn:
        SlskdStore().add_search(conn, sid, req.id, "feather nujabes", "L0", NOW)
    cfg = SlskdSettings(**settings)
    gate = SlskdGate(slskd, cfg, mono, lambda s: None)
    searcher = SlskdSearcher(db, slskd, gate, mono, lambda s: None, wall)
    dl = SlskdDownloader(db, slskd, searcher, cfg, mono, wall)
    return Case(dl, slskd, db, req.id, mono, wall)


PEERS = [peer(f"p{i}", 100 - i) for i in range(7)]  # p0 fastest


# ------------------------------------------------------------ the happy path


def test_start_writes_the_row_then_queues_the_best_candidate(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    assert c.dl.start(c.req)
    assert c.state() == "midDownload" and c.download().state == "queued"
    (a,) = c.attempts()
    assert (a.username, a.rank, a.outcome) == ("p0", 0, None) and a.transfer_id
    assert c.slskd.destinations == [f"post-vinyl/{c.download().id}/{a.id}"]


def test_completed_transfer_hands_off_the_file(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.transfer("InProgress", 1_000_000)
    c.tick(2)
    c.transfer("Completed, Succeeded", 20_000_000)
    c.tick(2)
    (a,) = c.attempts()
    d = c.download()
    assert a.outcome == "completed" and c.state() == "downloaded"
    assert d.state == "completed"
    assert d.file_location == f"post-vinyl/{d.id}/{a.id}/04 Feather.flac"
    with c.db.transaction() as conn:  # the trigger created the file row
        row = conn.execute(
            "SELECT file_location, file_state FROM file_management"
        ).fetchone()
    assert tuple(row) == (d.file_location, "downloaded")


# ------------------------------------------------------------ moving on


def test_queued_longer_than_15_s_moves_to_the_next_peer(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.tick(10)
    assert len(c.attempts()) == 1  # still within the queue wait
    c.tick(6)
    first, second = c.attempts()
    assert first.outcome == "noStart" and second.username == "p1" and second.rank == 1
    assert c.download().id == first.download_id == second.download_id  # same row
    assert c.slskd.transfers[first.transfer_id or ""].state == "Completed, Cancelled"


def test_no_bytes_20_s_after_downloading_is_no_start(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.transfer("InProgress", 0)
    for _ in range(10):
        c.tick(2)
    c.tick(2)
    assert c.attempts()[0].outcome == "noStart"


def test_too_slow_moves_on(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.transfer("InProgress", 1000)
    for i in range(12):  # +1 KB per 2 s ≈ 4 kbit/s, under 40
        c.transfer("InProgress", 1000 + 1000 * i)
        c.tick(2)
    assert c.attempts()[0].outcome == "tooSlow"


def test_slskd_reports_failure(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.transfer("Completed, Rejected")
    c.tick(2)
    first, second = c.attempts()
    assert first.outcome == "failed" and second.username == "p1"


def test_offline_peer_and_refused_file_fail_at_once(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.slskd.offline_peers = {"p0", "p1"}
    c.dl.start(c.req)
    assert [(a.username, a.outcome) for a in c.attempts()] == [
        ("p0", "failed"), ("p1", "failed"), ("p2", None),
    ]  # fmt: skip


def test_slskd_shutting_down_is_nobodys_fault(tmp_path: Path) -> None:
    """Live #115: a restarted slskd cut the transfer off and refused the next
    queue while logged out; both peers got blamed and two candidates went."""
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    tid = c.attempts()[-1].transfer_id
    assert tid is not None
    c.slskd.transfers[tid] = replace(c.slskd.transfers[tid], state="Completed, Errored",
                                     exception="Application shut down")  # fmt: skip
    c.slskd.state = AppState(logged_in=False, has_parent=False)  # restarting
    c.tick(2)
    c.tick(2)  # not logged in yet: nothing read, nothing queued
    assert [(a.username, a.outcome) for a in c.attempts()] == [("p0", None)]
    assert c.state() == "midDownload"
    c.slskd.state = AppState(logged_in=True, has_parent=True)
    c.tick(2)  # back: the cut-off attempt is nobody's fault, the same file again
    assert [(a.username, a.outcome) for a in c.attempts()] == [
        ("p0", "interrupted"), ("p0", None),
    ]  # fmt: skip
    with c.db.transaction() as conn:
        assert SlskdStore().get_peer(conn, "p0") is None  # not blamed


def test_slskds_own_resume_is_forgotten_and_the_file_queued_again(
    tmp_path: Path,
) -> None:
    """Live #115: slskd 0.26.2 requests a cut-off file again when it starts, but
    saves it outside our attempt folder."""
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    tid = c.attempts()[-1].transfer_id
    assert tid is not None
    old = c.slskd.transfers[tid]
    c.slskd.transfers[tid] = replace(old, state="Completed, Errored",
                                     exception="Application shut down")  # fmt: skip
    c.slskd.transfers["resumed"] = replace(old, id="resumed", state="InProgress")
    c.tick(2)
    assert "resumed" not in c.slskd.transfers
    assert [(a.username, a.outcome) for a in c.attempts()] == [
        ("p0", "interrupted"), ("p0", None),
    ]  # fmt: skip


def test_slskd_logging_out_as_a_file_is_queued_interrupts(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.slskd.log_out_on_queue = True
    c.dl.start(c.req)
    assert [(a.username, a.outcome) for a in c.attempts()] == [("p0", "interrupted")]
    c.slskd.log_out_on_queue = False
    c.slskd.state = AppState(logged_in=True, has_parent=True)
    c.tick(2)
    assert [(a.username, a.outcome) for a in c.attempts()][-1] == ("p0", None)


def test_a_poll_during_start_never_queues_the_file_twice(tmp_path: Path) -> None:
    """Live #115: the monitor polled between the row and its first attempt, saw
    a download with no attempt and queued it again."""
    c = build(tmp_path, PEERS)
    polls: list[threading.Thread] = []

    def poll_now() -> None:
        c.slskd.on_app_state = None
        polls.append(threading.Thread(target=c.dl.poll))
        polls[0].start()
        polls[0].join(timeout=0.3)  # without the lock it queues here

    c.slskd.on_app_state = poll_now  # after the row, before the attempt
    c.dl.start(c.req)
    polls[0].join()
    assert [(a.username, a.outcome) for a in c.attempts()] == [("p0", None)]
    assert len(c.slskd.transfers) == 1


def test_a_pick_made_while_slskd_is_logged_out_waits(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.slskd.state = AppState(logged_in=False, has_parent=False)
    c.dl.start(c.req)
    c.tick(2)
    assert c.attempts() == [] and c.state() == "midDownload"
    c.slskd.state = AppState(logged_in=True, has_parent=True)
    c.tick(2)
    assert [(a.username, a.outcome) for a in c.attempts()] == [("p0", None)]


# ------------------------------------------------------------ giving up


def test_five_candidates_at_most_then_not_downloaded(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)  # 7 candidates
    c.dl.start(c.req)
    for _ in range(5):
        c.tick(16)  # every peer keeps us queued
    assert len(c.attempts()) == 5
    assert c.state() == "notDownloaded" and c.download().state == "failed"


def test_last_attempt_that_broke_is_bad_download(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS[:1])
    c.dl.start(c.req)
    c.transfer("InProgress", 5_000)
    c.tick(2)
    c.transfer("Completed, Errored", 5_000)
    c.tick(2)
    assert c.state() == "badDownload"


def test_no_candidates_at_all_is_not_downloaded(tmp_path: Path) -> None:
    c = build(tmp_path, [])
    c.dl.start(c.req)
    assert c.state() == "notDownloaded" and c.attempts() == []


# ------------------------------------------------------------ peers


def test_three_failures_ban_a_peer_and_bans_expire(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    with c.db.transaction() as conn:
        for _ in range(2):
            SlskdStore().record_peer_failure(conn, "p0")
    c.dl.start(c.req)
    c.tick(16)  # p0's third failure
    with c.db.transaction() as conn:
        assert SlskdStore().blocked_usernames(conn) == {"p0"}
        assert SlskdStore().blocked_usernames(conn, NOW + 16 - 7 * 86_400) == {"p0"}
        assert SlskdStore().blocked_usernames(conn, NOW + 17) == set()  # expired: gone


def test_banned_peer_is_skipped_and_completion_clears_a_record(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    with c.db.transaction() as conn:
        SlskdStore().block_peer(conn, "p0", NOW)
        SlskdStore().record_peer_failure(conn, "p1")
    c.dl.start(c.req)
    assert c.attempts()[0].username == "p1"
    c.transfer("Completed, Succeeded", 20_000_000)
    c.tick(2)
    with c.db.transaction() as conn:
        assert SlskdStore().get_peer(conn, "p1") is None


# ------------------------------------------------------------ dropped, foreign


def test_dropped_mid_download_cancels_without_blaming_the_peer(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.dl.start(c.req)
    c.transfer("InProgress", 1000)
    with c.db.transaction() as conn:
        RequestsStore().set_track_state(conn, c.req, "dropped")
    c.tick(2)
    (a,) = c.attempts()
    assert a.outcome == "skipped" and c.download().state == "cancelled"
    assert c.slskd.transfers[a.transfer_id or ""].state == "Completed, Cancelled"
    with c.db.transaction() as conn:
        assert SlskdStore().get_peer(conn, "p0") is None


def test_transfers_the_app_did_not_start_are_ignored(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    c.slskd.queue_download("stranger", [("x.flac", 1)])
    c.dl.start(c.req)
    c.tick(2)
    assert {a.username for a in c.attempts()} == {"p0"}
    assert c.slskd.transfers["fake-transfer-1"].state == "Queued, Remotely"


def test_start_refuses_a_request_that_is_not_found(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS)
    with c.db.transaction() as conn:
        RequestsStore().set_track_state(conn, c.req, "dropped")
    assert not c.dl.start(c.req)


# ------------------------------------------------------------ helpers, worker


@pytest.mark.parametrize(
    ("remote", "name"),
    [
        (r"@@abc\Music\Nujabes\04 Feather.flac", "04 Feather.flac"),
        ("a/b\\c.mp3", "c.mp3"),
        ("x\\..", "_"),
        ("x\\a\0b.mp3", "a_b.mp3"),
    ],
)
def test_local_name_follows_slskd(remote: str, name: str) -> None:
    assert local_name(remote) == name


def test_transfer_phases() -> None:
    assert transfer_phase("Completed, Succeeded") == "done"
    for s in ("Completed, Cancelled", "Completed, TimedOut", "Completed, Errored",
              "Completed, Rejected", "Completed, Failed", "Completed, Aborted"):  # fmt: skip
        assert transfer_phase(s) == "failed"
    assert transfer_phase("InProgress") == "downloading"
    for s in ("Requested", "Queued, Remotely", "Queued, Locally", "Initializing"):
        assert transfer_phase(s) == "queued"


def test_monitor_keeps_polling_after_an_error() -> None:
    calls, done = [0], threading.Event()

    def poll() -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise SlskdNotFound("boom")
        done.set()

    m = DownloadMonitor(poll, every_s=0.01)
    m.start()
    assert done.wait(2)
    m.stop()


# ------------------------------------------------------------ next peer (#111)


def test_next_peer_skips_without_blaming_and_queues_the_next(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS, peer_ban_failures=1)  # one failure would ban
    c.dl.start(c.req)
    c.transfer("InProgress", 1_000)
    assert c.dl.next_peer(c.download().id)
    first, second = c.attempts()
    assert first.outcome == "skipped" and second.username == "p1"
    assert c.slskd.transfers[first.transfer_id or ""].state == "Completed, Cancelled"
    with c.db.transaction() as conn:
        assert SlskdStore().get_peer(conn, "p0") is None  # not blamed, not banned
    assert c.state() == "midDownload"


def test_next_peer_on_the_last_candidate_ends_the_request(tmp_path: Path) -> None:
    c = build(tmp_path, PEERS[:1])
    c.dl.start(c.req)
    assert c.dl.next_peer(c.download().id)
    assert c.state() == "notDownloaded" and c.download().state == "failed"
    assert not c.dl.next_peer(c.download().id)  # nothing running any more
