"""
Tests for DownloadMonitor background worker.

Uses fake services, real EventHub, and real SQLite via temporary directories.
Event capture follows the same pattern as tests/test_sse.py: publish from a
background thread, then await asyncio.sleep(0) to process call_soon_threadsafe.
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.exceptions import SlskdConnectionError
from app.services.interfaces.download import QueueResult, Transfer
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.sse import EventHub
from app.workers.download_monitor import DownloadMonitor


def _make_config(tmpdir, bad_peer_threshold=1):
    """Build a minimal test config."""
    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.download_dir = str(p / "download")
    paths.searches_dir = str(p / "searches")
    paths.discovery_dir = str(p / "discovery")
    # download_monitor.py resolves paths via the *_path properties in real
    # PathsConfig (music_dir / relative suffix) — mirror that here too.
    paths.download_path = p / "download"
    paths.searches_path = p / "searches"
    paths.discovery_path = p / "discovery"
    paths.library_dir = str(p / "library")
    paths.library_path = p / "library"

    class MockDownload:
        check_interval = 15
        max_retries_per_track = 3
        peer_ban_days = 2
        # #57 defaults to False in real config, but most tests here are
        # about retry *mechanics* (peer failover, budget, orphan handling),
        # not the toggle itself — keep it enabled here so those aren't all
        # forced to opt in, and cover the toggle's own behavior separately
        # in TestAutoRetryManualToggle.
        auto_retry_manual_soulseek = True

    MockDownload.bad_peer_threshold = bad_peer_threshold

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.download = MockDownload()
    return cfg


class FakeDownloadService:
    """In-memory download service for testing."""

    def __init__(self, transfers=None):
        self._transfers = transfers or []
        self.queue_calls: list[dict] = []
        self._search_responses: dict[str, list[dict]] = {}
        self.fetch_search_responses_calls: list[str] = []
        self.deleted_transfers: list[str] = []
        self.delete_transfer_result = True

    def get_status(self) -> list[Transfer]:
        return list(self._transfers)

    def queue(self, username, files, search_id=None, destination=None):
        self.queue_calls.append(
            {
                "username": username,
                "files": files,
                "search_id": search_id,
                "destination": destination,
            }
        )
        return QueueResult(
            enqueued_count=len(files),
            failures=[],
            search_id=search_id,
        )

    def set_transfers(self, transfers):
        self._transfers = transfers

    def set_search_responses(self, search_id, responses):
        self._search_responses[search_id] = responses

    def fetch_search_responses(self, search_id):
        self.fetch_search_responses_calls.append(search_id)
        return self._search_responses.get(search_id, [])

    def delete_transfer(self, transfer_id):
        self.deleted_transfers.append(transfer_id)
        return self.delete_transfer_result


class FakeLibraryService:
    """In-memory library for tests."""

    def __init__(self):
        self.scan_count = 0
        self.playlists: list[PlaylistInfo] = []
        self.playlist_songs: dict[str, list[Song]] = {}
        self.songs_by_query: dict[str, list[Song]] = {}
        self.create_calls: list[str] = []

    def trigger_scan(self):
        self.scan_count += 1
        return True

    def list_playlists(self):
        return list(self.playlists)

    def create_playlist(self, name):
        playlist_id = f"pl-{len(self.playlists) + 1}"
        self.playlists.append(PlaylistInfo(playlist_id, name, 0))
        self.create_calls.append(name)
        return playlist_id

    def get_playlist_detail(self, playlist_id):
        name = next(
            (p.name for p in self.playlists if p.playlist_id == playlist_id), ""
        )
        return PlaylistDetail(
            playlist_id, name, list(self.playlist_songs.get(playlist_id, []))
        )

    def search_library(self, query):
        return list(self.songs_by_query.get(query, []))

    def add_to_playlist(self, playlist_id, song_ids):
        songs = self.playlist_songs.setdefault(playlist_id, [])
        for sid in song_ids:
            songs.append(Song(sid, "", "", "", "", 0, 0, None, None, None, None, 0, False))
        return True


def _transfer(
    tid, username, filename, size, state, progress=0.0, speed=None, is_rec=False
):
    return Transfer(
        transfer_id=tid,
        username=username,
        filename=filename,
        size=size,
        state=state,
        progress=progress,
        speed=speed,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc)
        if state in ("completed", "failed", "cancelled")
        else None,
        is_rec_download=is_rec,
    )


@pytest.fixture
def tmp_config(tmp_path):
    return _make_config(str(tmp_path))


@pytest.fixture
def db(tmp_config):
    database = Database(tmp_config)
    database.initialize_schema()
    yield database
    database.close()


def _run_poll_and_capture(monitor, hub):
    """Run poll_once() in a background thread and capture SSE events.

    Pattern from tests/test_sse.py: publish from thread, await sleep(0),
    then drain subscriber queue.  Returns (poll_result, events_list).
    """
    result_holder: dict = {}

    def _poll():
        result_holder["result"] = monitor.poll_once()

    async def _capture():
        sub = hub.subscribe()
        t = threading.Thread(target=_poll)
        t.start()
        t.join()
        await asyncio.sleep(0)
        captured: list[tuple[str, dict]] = []
        while not sub.queue.empty():
            ev = sub.queue.get_nowait()
            captured.append((ev.event_type, ev.data))
        hub.unsubscribe(sub)
        return captured

    captured_events = asyncio.run(_capture())
    return result_holder["result"], captured_events


class TestPollOnceBasic:
    def test_empty_transfers(self, tmp_config, db):
        hub = EventHub()
        download_svc = FakeDownloadService([])
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["transfers_seen"] == 0
        assert result["moved"] == []
        assert result["scan_triggered"] is False
        assert result["retried"] == []

    def test_states_no_files_on_disk(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-complete",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
            _transfer(
                "t-errored",
                "absolutelyjaked2",
                "music\\Clipse\\Clipse - Let God Sort Em Out\\03 - P.O.V.mp3",
                10455731,
                "failed",
                progress=0.0,
            ),
            _transfer(
                "t-cancelled",
                "cr4sh0verr1de",
                "media\\Sorted\\Singles\\Queen-Awesome 80's (disc 1)-01-Another One Bites the Dust.mp3",
                5190425,
                "cancelled",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        assert _result["transfers_seen"] == 3
        assert _result["scan_triggered"] is False

        event_types = {e[0] for e in events}
        assert "transfer.completed" in event_types
        assert "transfer.failed" in event_types
        assert "transfer.cancelled" not in event_types

        completed_ev = next(e for e in events if e[0] == "transfer.completed")
        assert completed_ev[1]["target_dir"] == ""

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False

    def test_started_event(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer("t-new", "peer1", "song.mp3", 1000, "queued"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        started = [e for e in events if e[0] == "transfer.started"]
        assert len(started) == 1
        assert started[0][1]["transfer_id"] == "t-new"

    def test_started_event_on_pending_adoption(self, tmp_config, db):
        """A queue-time pending row adopted by its slskd transfer emits started."""
        store = DownloadStore(db)
        store.insert_pending("search-1", "peer1", "song.mp3", 1000, False)

        hub = EventHub()
        transfers = [
            _transfer(
                "uuid-123", "peer1", "song.mp3", 1000, "downloading", progress=5.0
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        started = [e for e in events if e[0] == "transfer.started"]
        assert len(started) == 1
        assert started[0][1]["transfer_id"] == "uuid-123"
        row = store.get_transfer("uuid-123")
        assert row is not None
        assert row["search_id"] == "search-1"

    def test_progress_event(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-dl",
                "peer1",
                "song.mp3",
                1000,
                "downloading",
                progress=45.0,
                speed=1024,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        progress_ev = [e for e in events if e[0] == "transfer.progress"]
        assert len(progress_ev) == 1
        assert progress_ev[0][1]["progress"] == 45
        assert progress_ev[0][1]["speed"] == 1024


class TestFileMove:
    def test_file_move_cycle(self, tmp_config, db):
        """Completed downloads land flat in library/new-system-tests/, no
        tagging, no per-username subfolder (beets removed; see AGENTS.md /
        issues #2, #55, #58)."""
        hub = EventHub()
        transfers = [
            _transfer(
                "t-move",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        source_dir = (
            Path(tmp_config.paths.download_dir)
            / "complete"
            / "soulseek"
            / "ShaLaLaLaLee"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "01 - Bohemian Rhapsody.mp3").write_text("fake mp3 data")

        target_dir = tmp_config.paths.library_path / "new-system-tests"

        monitor = DownloadMonitor(
            tmp_config,
            download_svc,
            lib_svc,
            db,
            hub,
            interval=15,
        )

        result, events = _run_poll_and_capture(monitor, hub)

        assert result["scan_triggered"] is True
        assert lib_svc.scan_count == 1
        assert len(result["moved"]) == 1

        target = target_dir / "01 - Bohemian Rhapsody.mp3"
        assert target.exists()

        store = DownloadStore(db)
        assert store.file_moved("t-move")

        completed_ev = next(e for e in events if e[0] == "transfer.completed")
        assert completed_ev[1]["target_dir"] == str(target_dir)

    def test_file_move_collision(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-collide",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        source_dir = (
            Path(tmp_config.paths.download_dir)
            / "complete"
            / "soulseek"
            / "ShaLaLaLaLee"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "01 - Bohemian Rhapsody.mp3").write_text("fake mp3 data")

        target_dir = tmp_config.paths.library_path / "new-system-tests"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "01 - Bohemian Rhapsody.mp3").write_text("pre-existing")

        monitor = DownloadMonitor(
            tmp_config,
            download_svc,
            lib_svc,
            db,
            hub,
            interval=15,
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        moved = result["moved"]
        assert len(moved) == 1
        assert "(1)" in Path(moved[0]).name

class TestFileImportMove:
    """beets is gone (issues #2, #55, #58): a completed download is now
    unconditionally moved into library/new-system-tests/, flat, no
    tagging, no metadata parsing, no profile selection."""

    def _completed(self, filename="a\\b\\01 - Track.mp3"):
        return [
            _transfer("t-b", "peerone", filename, 5000, "completed", progress=100.0)
        ]

    def _seed_source(self, tmp_config, *relative_parts):
        source = Path(
            tmp_config.paths.download_dir,
            "complete",
            "soulseek",
            "peerone",
            *relative_parts,
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fake mp3 data")
        return source

    def test_move_lands_flat_in_new_system_tests(self, tmp_config, db):
        hub = EventHub()
        source = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1
        target = tmp_config.paths.library_path / "new-system-tests" / "01 - Track.mp3"
        assert Path(result["moved"][0]) == target
        assert target.exists()
        assert not source.exists()
        assert DownloadStore(db).file_moved("t-b")

    def test_exact_reported_path_wins_over_a_basename_twin(self, tmp_config, db):
        """The old _move_file() defect: a first-basename-match glob could
        move a *different* download that happened to share a filename."""
        hub = EventHub()
        wanted = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        decoy = self._seed_source(tmp_config, "other", "01 - Track.mp3")
        decoy.write_text("WRONG FILE")

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        _run_poll_and_capture(monitor, hub)

        assert not wanted.exists()
        assert decoy.exists(), "the same-named file from another download was moved"

    def test_falls_back_to_basename_when_exact_path_is_absent(self, tmp_config, db):
        """slskd doesn't always mirror the peer's directory structure
        locally, so the old basename search stays as a fallback."""
        hub = EventHub()
        self._seed_source(tmp_config, "01 - Track.mp3")

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1

    def test_missing_source_does_not_mark_the_file_moved(self, tmp_config, db):
        """No source file on disk at all: _resolve_source_path returns None,
        _handle_missing_source runs, nothing is moved or marked."""
        hub = EventHub()

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert not DownloadStore(db).file_moved("t-b")

    def test_move_failure_logs_and_returns_none_without_crashing(
        self, tmp_config, db, monkeypatch
    ):
        """shutil.move raising (e.g. destination unwritable) must not crash
        poll_once — it logs a warning, leaves the source in place, and the
        polling loop keeps going for other transfers."""
        import shutil

        hub = EventHub()
        source = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")

        def _boom(*args, **kwargs):
            raise OSError("destination unwritable")

        monkeypatch.setattr(shutil, "move", _boom)

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert source.exists()
        assert not DownloadStore(db).file_moved("t-b")


class TestAutoRetryManualToggle:
    """#57: auto-retry for a failed manual (non-rec) download is gated by
    download.auto_retry_manual_soulseek (default False in real config —
    _make_config here defaults it True so the rest of TestRetry/
    TestOrphanReconciliation/TestPeerBlocking don't have to opt in for
    unrelated retry-mechanics coverage)."""

    def _queue_manual_download(self, db, is_library_download=False):
        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
            is_library_download,
            "recording-1" if is_library_download else None,
        )

    def _make_monitor_with_responses(self, cfg, db, hub):
        transfers = [
            _transfer(
                "t-retry",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
                progress=0.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        download_svc.set_search_responses(
            "search-retry-1", json.loads(fixture_path.read_text())
        )
        return DownloadMonitor(
            cfg, download_svc, FakeLibraryService(), db, hub, interval=15
        ), download_svc

    def test_disabled_by_default_manual_download_is_not_retried(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        cfg.download.auto_retry_manual_soulseek = False
        self._queue_manual_download(db)
        monitor, download_svc = self._make_monitor_with_responses(cfg, db, hub)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert download_svc.queue_calls == []
        failed = [d for name, d in events if name == "transfer.failed"]
        assert failed[0]["will_retry"] is False
        assert "Auto-retry disabled for manual downloads" in failed[0]["error"]

    def test_disabled_by_default_also_covers_library_profile_downloads(
        self, tmp_path, db
    ):
        """MB-tab downloads are manual (is_rec_download=False) too — the
        toggle covers both beets profiles, same as #19's scope."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        cfg.download.auto_retry_manual_soulseek = False
        self._queue_manual_download(db, is_library_download=True)
        monitor, download_svc = self._make_monitor_with_responses(cfg, db, hub)

        _run_poll_and_capture(monitor, hub)

        assert download_svc.queue_calls == []

    def test_enabled_manual_download_is_retried(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        cfg.download.auto_retry_manual_soulseek = True
        self._queue_manual_download(db)
        monitor, download_svc = self._make_monitor_with_responses(cfg, db, hub)

        _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1

    def test_rec_download_retries_regardless_of_the_toggle(self, tmp_path, db):
        """A rec's auto-retry is pre-existing behavior, unaffected by this
        manual-only toggle — even with it off."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        cfg.download.auto_retry_manual_soulseek = False
        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            True,  # is_rec_download
        )
        monitor, download_svc = self._make_monitor_with_responses(cfg, db, hub)

        _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1


class TestRetry:
    def test_retry_path_with_search_id(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer(
                "t-retry",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
                progress=0.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-1", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
            True,
            "recording-1",
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "absolutelyjaked2"

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

        retry_row = db.fetch_one(
            "SELECT search_id, is_library_download, mb_recording_id, retry_count "
            "FROM downloads WHERE username = ? AND state = 'queued'",
            (retry_peer,),
        )
        assert retry_row is not None
        assert retry_row["search_id"] == "search-retry-1"
        assert retry_row["is_library_download"] == 1
        assert retry_row["mb_recording_id"] == "recording-1"
        assert retry_row["retry_count"] == 1

    def test_superseded_transfer_is_removed_from_slskd(self, tmp_path, db):
        """A 'failed' state is often transient — slskd (or the peer) can
        still finish the original after musica re-queued the track
        elsewhere, and both copies land. Live-confirmed 2026-08-11: a
        5-track Comfort Zone pull downloaded 2 tracks twice."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        download_svc = FakeDownloadService(
            [
                _transfer(
                    "t-dupe",
                    "absolutelyjaked2",
                    "music\\Clipse\\03 - P.O.V.mp3",
                    10455731,
                    "failed",
                )
            ]
        )
        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        download_svc.set_search_responses(
            "search-dupe-1", json.loads(fixture_path.read_text())
        )
        DownloadStore(db).insert_pending(
            "search-dupe-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(
            cfg, download_svc, FakeLibraryService(), db, hub, interval=15
        )
        _run_poll_and_capture(monitor, hub)

        assert download_svc.queue_calls, "expected a retry to be queued"
        assert download_svc.deleted_transfers == ["t-dupe"]

    def test_failed_slskd_removal_does_not_break_the_retry(self, tmp_path, db):
        """Removal is best-effort — losing it only risks the duplicate we
        already had, so it must never turn a good retry into a failure."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        download_svc = FakeDownloadService(
            [
                _transfer(
                    "t-dupe2",
                    "absolutelyjaked2",
                    "music\\Clipse\\03 - P.O.V.mp3",
                    10455731,
                    "failed",
                )
            ]
        )
        download_svc.delete_transfer = lambda _tid: (_ for _ in ()).throw(
            RuntimeError("slskd unreachable")
        )
        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        download_svc.set_search_responses(
            "search-dupe-2", json.loads(fixture_path.read_text())
        )
        DownloadStore(db).insert_pending(
            "search-dupe-2",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(
            cfg, download_svc, FakeLibraryService(), db, hub, interval=15
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

    def test_no_retry_when_no_search_id(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer("t-noretry", "peerX", "track.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False
        assert "No search context" in failed_ev[1]["error"]

    def test_retry_refetches_from_slskd_after_restart(self, tmp_path, db):
        """Retry must survive a restart with musica holding no copy of the
        results. After a restart the in-memory cache is empty, so the search
        is re-read from slskd by search_id — which is a *re-read of the same
        completed search*, not a new one, so the candidate pool is unchanged.
        Verified live 2026-08-11: slskd still served all 250 responses for
        musica's oldest search, and they survived a slskd restart."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer(
                "t-persisted",
                "failingpeer",
                "music\\Band\\01 - Track.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())

        store = DownloadStore(db)
        store.insert_pending(
            "search-persisted-1",
            "failingpeer",
            "music\\Band\\01 - Track.mp3",
            10455731,
            False,
        )
        # slskd still holds this search — nothing in musica's DB does.
        download_svc.set_search_responses("search-persisted-1", responses)

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "failingpeer"
        # Sourced from slskd, keyed by the search_id on the download row.
        assert download_svc.fetch_search_responses_calls == ["search-persisted-1"]

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True


class TestPeerBlocking:
    def test_peer_blocked_after_threshold(self, tmp_config, db):
        hub = EventHub()

        transfers1 = [
            _transfer("t-fail1", "badpeer", "song1.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers1)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )
        _run_poll_and_capture(monitor, hub)

        transfers2 = [
            _transfer("t-fail2", "badpeer", "song2.mp3", 200, "failed"),
        ]
        download_svc.set_transfers(transfers2)
        _result, events = _run_poll_and_capture(monitor, hub)

        store = DownloadStore(db)
        assert store.is_peer_blocked("badpeer")

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert "blocked" in failed_ev[1]["error"].lower()
        assert failed_ev[1]["will_retry"] is False

    def test_expired_block_is_lifted_and_peer_retried(self, tmp_path, db):
        """A block older than config.download.peer_ban_days must be lifted
        so the peer is eligible again, not treated as permanently banned."""
        import time

        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=1)
        cfg.download.peer_ban_days = 2
        transfers = [
            _transfer(
                "t-retry-expired",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-expired", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-expired",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )
        # Pre-block the alternative peer the fixture would otherwise pick
        # first ("fivepointsquare" — first candidate with a free slot and
        # an actual matching file), but backdate it past the 2-day window
        # so it must be treated as eligible again rather than skipped.
        store.increment_peer_failure("fivepointsquare")
        store.set_peer_blocked("fivepointsquare")
        store._db.execute(
            "UPDATE peers SET blocked_at = ? WHERE username = ?",
            (int(time.time()) - 3 * 86400, "fivepointsquare"),
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)
        _run_poll_and_capture(monitor, hub)

        assert not store.is_peer_blocked("fivepointsquare", 2 * 86400)
        assert store.get_peer_failure_count("fivepointsquare") == 0
        assert download_svc.queue_calls[0]["username"] == "fivepointsquare"

    def test_blocked_peer_still_retries_with_alternative(self, tmp_path, db):
        """Blocking the failing peer must not short-circuit retry when an
        alternative peer is available in the stored search responses."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=1)
        transfers = [
            _transfer(
                "t-retry-blocked",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-2", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-2",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert store.is_peer_blocked("absolutelyjaked2")
        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "absolutelyjaked2"

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

    def test_blocked_peer_no_alternative_gives_up(self, tmp_config, db):
        """Blocking with no alternative peer available still gives up, but
        the retry budget is what stops it, not the block itself."""
        hub = EventHub()
        transfers = [
            _transfer("t-fail-noalt", "lonelypeer", "song.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        store = DownloadStore(db)
        assert store.is_peer_blocked("lonelypeer")

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False
        assert "blocked" in failed_ev[1]["error"].lower()
        assert "no search context" in failed_ev[1]["error"].lower()


class TestStartStop:
    def test_start_and_stop(self, tmp_config, db):
        hub = EventHub()
        download_svc = FakeDownloadService([])
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=0.1
        )

        monitor.start()
        assert monitor._thread is not None
        assert monitor._thread.is_alive()

        monitor.stop()
        assert monitor._thread is None


class TestHousekeeping:
    """P6.5 review fixes (2026-08-11): stale-pending reaping and the search
    retention sweep, both driven off the existing poll loop."""

    def test_stale_pending_row_is_failed_and_announced(self, tmp_config, db):
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "stuck.mp3", 100, False)
        db.execute("UPDATE downloads SET created_at = created_at - 600")
        assert store.has_active_manual_downloads() is True

        monitor = DownloadMonitor(
            tmp_config, FakeDownloadService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        # The gate RecPuller waits on is released...
        assert store.has_active_manual_downloads() is False
        # ...and the user is told why, rather than the row just vanishing.
        failed = [d for name, d in events if name == "transfer.failed"]
        assert len(failed) == 1
        assert failed[0]["will_retry"] is False
        assert "never picked up" in failed[0]["error"]

    def test_fresh_pending_row_is_left_alone(self, tmp_config, db):
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "recent.mp3", 100, False)

        monitor = DownloadMonitor(
            tmp_config, FakeDownloadService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is True
        assert [name for name, _ in events if name == "transfer.failed"] == []

    def test_reaping_runs_even_when_slskd_is_unreachable(self, tmp_config, db):
        """slskd being down is exactly when rows go unadopted, and poll_once
        returns early on a connection error — so housekeeping runs first."""
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "stuck.mp3", 100, False)
        db.execute("UPDATE downloads SET created_at = created_at - 600")

        class UnreachableService(FakeDownloadService):
            def get_status(self):
                raise SlskdConnectionError("http://slskd:5030", "refused")

        monitor = DownloadMonitor(
            tmp_config, UnreachableService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert "error" in result
        assert store.has_active_manual_downloads() is False

class TestOrphanReconciliation:
    """A transfer slskd stops reporting used to sit in 'downloading' forever:
    phantom UI row, no retry (retry fires on a reported 'failed' that never
    comes), and a permanent block on rec queueing if it was manual."""

    def _adopt(self, db, monitor, hub, *, is_rec=False, search_id="s-orphan"):
        """Get a row to the adopted/downloading state the normal way."""
        store = DownloadStore(db)
        store.insert_pending(search_id, "peer1", "song.mp3", 100, is_rec)
        monitor._download_service._transfers = [
            _transfer("uuid-1", "peer1", "song.mp3", 100, "downloading")
        ]
        _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "downloading"
        return store

    def _monitor(self, cfg, db, hub, responses=None):
        svc = FakeDownloadService([])
        if responses is not None:
            svc._search_responses["s-orphan"] = responses
        return DownloadMonitor(cfg, svc, FakeLibraryService(), db, hub, interval=15)

    def test_still_reported_transfer_is_untouched(self, tmp_config, db):
        """Restarting musica alone leaves slskd transferring — those rows
        resync normally and must never be cancelled."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)

        for _ in range(5):
            _run_poll_and_capture(monitor, hub)

        assert store.get_transfer("uuid-1")["state"] == "downloading"

    def test_orphan_is_failed_after_the_grace_period(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)

        # slskd stops reporting it.
        monitor._download_service._transfers = []

        # One miss is within grace (default 2) — still live.
        _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "downloading"

        _result, events = _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "failed"
        failed = [d for name, d in events if name == "transfer.failed"]
        assert len(failed) == 1
        assert "stopped reporting" in failed[0]["error"]

    def test_a_blip_does_not_kill_a_healthy_transfer(self, tmp_config, db):
        """One truncated status response must not orphan anything — the
        miss counter resets as soon as the row is reported again."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        transfers = monitor._download_service._transfers

        for _ in range(4):
            monitor._download_service._transfers = []
            _run_poll_and_capture(monitor, hub)
            monitor._download_service._transfers = transfers
            _run_poll_and_capture(monitor, hub)

        assert store.get_transfer("uuid-1")["state"] == "downloading"

    def test_orphan_releases_the_rec_queue_gate(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub, is_rec=False)
        assert store.has_active_manual_downloads() is True

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is False

    def test_orphan_is_retried_from_persisted_responses(self, tmp_config, db):
        """The payoff of P6.5-4: re-pick an alternative peer without
        re-issuing a search."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        monitor._download_service.set_search_responses(
            "s-orphan",
            [
                {
                    "username": "peer1",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                },
                {
                    "username": "peer2",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                },
            ],
        )

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        result, events = _run_poll_and_capture(monitor, hub)

        # Re-queued from the *other* peer, sourced by re-reading the same
        # completed search from slskd — never by starting a new one.
        assert result["retried"] == ["uuid-1"]
        assert monitor._download_service.queue_calls[-1]["username"] == "peer2"
        assert monitor._download_service.fetch_search_responses_calls == ["s-orphan"]
        failed = [d for name, d in events if name == "transfer.failed"]
        assert failed[0]["will_retry"] is True

    def test_orphan_respects_the_retry_budget(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        monitor._download_service.set_search_responses(
            "s-orphan",
            [
                {
                    "username": "peer2",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                }
            ],
        )
        db.execute("UPDATE downloads SET retry_count = 99 WHERE id = 'uuid-1'")

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        _result, events = _run_poll_and_capture(monitor, hub)

        failed = [d for name, d in events if name == "transfer.failed"]
        assert failed[0]["will_retry"] is False
        assert "Max retries exceeded" in failed[0]["error"]

    def test_unadopted_pending_rows_are_left_to_the_reaper(self, tmp_config, db):
        """A 'pending:' row has never been confirmed by slskd, so its absence
        from a status poll means nothing — fail_stale_pending owns those."""
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "fresh.mp3", 100, False)

        monitor = self._monitor(tmp_config, db, hub)
        for _ in range(5):
            _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is True


class TestDirectSearchImportRegression:
    """Issue #4: direct Soulseek-tab search downloads (not MB search, not
    recs) used to complete in slskd but be reported as never picked up by
    beets. beets (and its profile routing) is gone now — the move is
    unconditional — but the regression this guarded (a transfer with no
    `downloads` row at all still gets moved) still matters."""

    def test_direct_search_with_no_pending_row_at_all_still_moves(
        self, tmp_config, db
    ):
        """A direct-search download never persisted as a `downloads` row
        (e.g. the queueing UI call didn't pass search_id) is still picked
        up and moved — get_transfer() returning None doesn't block the
        move."""
        hub = EventHub()
        transfers = [
            _transfer(
                "t-untracked", "peerone", "a\\Track.mp3", 5000, "completed",
                progress=100.0,
            )
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "peerone"
        )
        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1


class TestMissingSourceTimeout:
    """A transfer slskd swears is 'completed' but whose file never appears
    on disk (the dominant real case: a zombie row adopted from slskd's own
    transfer history, which outlives a musica reset, pointing at a file the
    reset already deleted) must eventually stop being retried rather than
    spinning forever — see _handle_missing_source's docstring."""

    def _completed(self, tid="t-ghost"):
        return [
            _transfer(tid, "ghost", "a\\Track.mp3", 5000, "completed", progress=100.0)
        ]

    def test_keeps_retrying_before_the_timeout(self, tmp_config, db):
        hub = EventHub()
        tmp_config.download.missing_source_timeout_minutes = 5
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert not DownloadStore(db).import_handled("t-ghost")
        assert "t-ghost" in monitor._missing_source_since

    def test_gives_up_after_the_timeout(self, tmp_config, db):
        hub = EventHub()
        tmp_config.download.missing_source_timeout_minutes = 5
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        # Simulate having already been missing for longer than the timeout,
        # rather than sleeping in the test.
        monitor._missing_source_since["t-ghost"] = time.time() - 301

        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert DownloadStore(db).import_handled("t-ghost")
        assert "t-ghost" not in monitor._missing_source_since

    def test_giving_up_stops_the_retry_even_though_slskd_still_says_completed(
        self, tmp_config, db
    ):
        """slskd never stops reporting this transfer as 'completed' (it is
        not wrong — the network transfer did complete), so the terminal
        marker must be import_skipped, not state='failed': upsert_transfer
        would silently overwrite a 'failed' state back to 'completed' on
        the very next poll and the row would start retrying all over
        again."""
        hub = EventHub()
        tmp_config.download.missing_source_timeout_minutes = 5
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
        )
        monitor._missing_source_since["t-ghost"] = time.time() - 301
        _run_poll_and_capture(monitor, hub)
        assert DownloadStore(db).import_handled("t-ghost")

        # slskd reports the exact same transfer as "completed" again.
        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []

    def test_source_found_before_timeout_clears_the_tracking_entry(
        self, tmp_config, db
    ):
        """A transient miss that resolves itself (e.g. the file becomes
        visible a poll later) must not leave a stale timer running."""
        hub = EventHub()
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "ghost"
        )
        service = FakeDownloadService(self._completed())
        monitor = DownloadMonitor(
            tmp_config, service, FakeLibraryService(), db, hub, interval=15,
        )

        result, _events = _run_poll_and_capture(monitor, hub)
        assert "t-ghost" in monitor._missing_source_since
        assert result["moved"] == []

        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

        result, _events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1
        assert "t-ghost" not in monitor._missing_source_since


class TestFileMovedBookkeeping:
    """file_moved = 1 is the app's claim that the file is at target_dir.
    Nothing may set it without one (migration 007)."""

    def test_mark_file_moved_rejects_an_empty_target_dir(self, db):
        store = DownloadStore(db)
        db.execute(
            "INSERT INTO downloads (id, username, filename, state, created_at) "
            "VALUES ('t-empty', 'peer', 'a.mp3', 'completed', 1)"
        )
        with pytest.raises(ValueError):
            store.mark_file_moved("t-empty", "")
        row = db.fetch_one("SELECT file_moved FROM downloads WHERE id = 't-empty'")
        assert not row["file_moved"]

    def test_import_handled_covers_both_terminal_states(self, db):
        store = DownloadStore(db)
        for tid in ("t-moved", "t-skipped", "t-open"):
            db.execute(
                "INSERT INTO downloads (id, username, filename, state, created_at) "
                f"VALUES ('{tid}', 'peer', 'a.mp3', 'completed', 1)"
            )
        store.mark_file_moved("t-moved", "/music/Searches/A/B")
        store.mark_import_skipped("t-skipped")

        assert store.import_handled("t-moved")
        assert store.import_handled("t-skipped")
        assert not store.import_handled("t-open")
        assert not store.import_handled("t-nonexistent")

    def test_import_pending_treats_no_row_as_not_pending(self, db):
        """import_pending is not simply `not import_handled`: a transfer_id
        musica has no downloads row for at all (e.g. slskd reporting its
        own transfer history from before a musica reset) means "not ours to
        track", not "still importing" — those are different things a caller
        surfacing progress needs to tell apart."""
        store = DownloadStore(db)
        for tid in ("t-moved", "t-skipped", "t-open"):
            db.execute(
                "INSERT INTO downloads (id, username, filename, state, created_at) "
                f"VALUES ('{tid}', 'peer', 'a.mp3', 'completed', 1)"
            )
        store.mark_file_moved("t-moved", "/music/Searches/A/B")
        store.mark_import_skipped("t-skipped")

        assert not store.import_pending("t-moved")
        assert not store.import_pending("t-skipped")
        assert store.import_pending("t-open")
        assert not store.import_pending("t-nonexistent")

    def test_skipped_row_is_not_reported_as_moved(self, db):
        store = DownloadStore(db)
        db.execute(
            "INSERT INTO downloads (id, username, filename, state, created_at) "
            "VALUES ('t-s', 'peer', 'a.mp3', 'completed', 1)"
        )
        store.mark_import_skipped("t-s")
        assert not store.file_moved("t-s")
        row = db.fetch_one("SELECT target_dir FROM downloads WHERE id = 't-s'")
        assert not row["target_dir"]
