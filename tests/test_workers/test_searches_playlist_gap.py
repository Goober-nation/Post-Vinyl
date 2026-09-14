"""
Issue #19 (split from #4): no "Searches" Navidrome playlist is ever created
or grown for manual downloads, regardless of which search mode produced
them (MB-tab search routes to the "library" beets profile; direct Soulseek
search routes to the "searches" profile). Only rec categories (comfort_zone,
fresh_picks, deep_cuts) ever get a Navidrome playlist, via
RecPuller._write_category_playlist. playlist_registry.py has no "searches"
role registered, and DownloadMonitor never calls add_to_playlist on the
manual-download path.

Test plan posted to https://github.com/Goober-nation/Post-Vinyl/issues/19
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.services.interfaces.download import Transfer
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.sse import EventHub
from app.workers.download_monitor import DownloadMonitor


def _make_config(tmpdir, bad_peer_threshold=1):
    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.music_dir = p / "music"
    paths.download_dir = str(p / "downloads")
    paths.download_path = p / "downloads"
    paths.searches_dir = str(p / "searches")
    paths.discovery_dir = str(p / "discovery")

    class MockBeets:
        enabled = False
        binary = "beet"
        timeout_seconds = 300

    class MockDownload:
        check_interval = 15
        max_retries_per_track = 3
        missing_source_timeout_minutes = 5

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.beets = MockBeets()
    cfg.download = MockDownload()
    return cfg


class FakeBeetsService:
    """Same shape as test_download_monitor.py's fake — moves the file to a
    profile tree the way real beets would, so a completed import always has
    a target_path to hand a hypothetical playlist step."""

    def __init__(self, config, matched=True):
        self._config = config
        self.matched = matched
        self.calls: list[tuple] = []

    def import_file(
        self, source, is_rec, title=None, artist=None, category=None,
        library=None, mbid=None,
    ):
        from app.services.beets import BeetsImportResult

        self.calls.append((source, is_rec, title, artist, category, library, mbid))
        target_root = Path(
            self._config.paths.discovery_dir
            if is_rec
            else self._config.paths.searches_dir
        )
        target_dir = target_root / source.parent.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        source.rename(target)
        return BeetsImportResult(matched=self.matched, target_path=target)


class FakeLibraryService:
    def __init__(self):
        self._playlists: list[PlaylistInfo] = []
        self._playlist_songs: dict[str, list[Song]] = {}
        self.create_calls: list[str] = []
        self.add_calls: list[tuple] = []

    def list_playlists(self):
        return list(self._playlists)

    def create_playlist(self, name):
        pid = f"pl-{len(self._playlists) + 1}"
        self.create_calls.append(name)
        self._playlists.append(PlaylistInfo(pid, name, 0))
        self._playlist_songs[pid] = []
        return pid

    def add_to_playlist(self, playlist_id, song_ids):
        self.add_calls.append((playlist_id, list(song_ids)))
        return True

    def get_playlist_detail(self, playlist_id):
        return PlaylistDetail(
            playlist_id=playlist_id, name="",
            songs=list(self._playlist_songs.get(playlist_id, [])),
        )

    def trigger_scan(self):
        return True

    def get_song_real_path(self, song_id):
        return None


class FakeDownloadService:
    def __init__(self, transfers=None):
        self._transfers = transfers or []

    def get_status(self):
        return list(self._transfers)

    def cancel(self, transfer_id):
        return True

    def remove_completed(self, transfer_id):
        return True

    def queue(self, *a, **kw):
        raise NotImplementedError


def _transfer(tid, username, filename, size, state, progress=0.0, is_rec=False):
    return Transfer(
        transfer_id=tid, username=username, filename=filename, size=size,
        state=state, progress=progress, speed=None,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc) if state == "completed" else None,
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


def _complete_a_manual_download(tmp_config, db, *, is_library, mbid=None):
    hub = EventHub()
    if is_library:
        store = DownloadStore(db)
        store.insert_pending(
            "s-mb", "peerone", "a\\Track.mp3", 5000, False,
            is_library_download=True, mb_recording_id=mbid or "mbid-123",
        )
    source_dir = Path(tmp_config.paths.download_path) / "complete" / "soulseek" / "peerone"
    (source_dir / "a").mkdir(parents=True, exist_ok=True)
    (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

    tmp_config.beets.enabled = True
    beets = FakeBeetsService(tmp_config)
    library = FakeLibraryService()
    monitor = DownloadMonitor(
        tmp_config,
        FakeDownloadService([_transfer("t-1", "peerone", "a\\Track.mp3", 5000, "completed", progress=100.0)]),
        library,
        db,
        hub,
        interval=15,
        beets_service=beets,
    )
    monitor.poll_once()
    return library


class TestNoSearchesPlaylistExists:
    """T19a — gap proof: neither search mode ever touches a playlist."""

    def test_direct_search_import_never_calls_add_to_playlist(self, tmp_config, db):
        library = _complete_a_manual_download(tmp_config, db, is_library=False)

        assert library.create_calls == []
        assert library.add_calls == []

    def test_mb_search_import_never_calls_add_to_playlist(self, tmp_config, db):
        library = _complete_a_manual_download(tmp_config, db, is_library=True)

        assert library.create_calls == []
        assert library.add_calls == []

    def test_playlist_registry_has_no_searches_role(self):
        import inspect

        from app.services import playlist_registry

        # The registry itself is role-agnostic (resolve_playlist_id takes a
        # role string), so the gap isn't in the registry's code — it's that
        # nothing in the codebase ever calls it with role="searches". This
        # documents that absence directly against the module's own listed
        # roles (trash, comfort_zone, fresh_picks, deep_cuts).
        doc = inspect.getdoc(playlist_registry) or ""
        assert "searches" not in doc.lower()


class TestSearchesPlaylistSpec:
    """T19b — spec for the fix (xfail today): a completed manual download,
    from either search mode, should append to a "Searches" playlist,
    auto-created on first use like Fresh Picks."""

    @pytest.mark.xfail(
        reason="no searches playlist role exists yet — issue #19", strict=True
    )
    def test_direct_search_import_grows_the_searches_playlist(self, tmp_config, db):
        library = _complete_a_manual_download(tmp_config, db, is_library=False)

        assert library.create_calls == ["Searches"]
        assert library.add_calls

    @pytest.mark.xfail(
        reason="no searches playlist role exists yet — issue #19", strict=True
    )
    def test_mb_search_import_also_grows_the_searches_playlist(self, tmp_config, db):
        """Both search modes name-check "Searches" in the issue, so the fix
        must not gate on which beets profile the file landed in."""
        library = _complete_a_manual_download(tmp_config, db, is_library=True)

        assert library.create_calls == ["Searches"]
        assert library.add_calls
