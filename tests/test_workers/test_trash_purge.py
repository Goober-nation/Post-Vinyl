"""
Tests for the TrashPurge worker (P6.7-6): Trash playlist sweep + stranded
download sweep.
"""

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.sync_store import HATE, SyncStore
from app.exceptions import ListenBrainzDisabledError
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.workers.trash_purge import TRASH_PLAYLIST_NAME, TrashPurge


def _make_config(tmpdir):
    from pathlib import Path

    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.music_dir = p / "music"
    paths.download_dir = "downloads"
    paths.download_path = p / "music" / "downloads"

    class MockSync:
        interval_hours = 12
        love_enabled = True
        hate_enabled = True
        star_rating_enabled = True
        trash_deletion_enabled = True

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.sync = MockSync()
    return cfg


def _song(song_id, title, mbid, path="some/path/file.flac"):
    return Song(
        song_id=song_id,
        title=title,
        artist="Artist",
        album="Album",
        path=path,
        duration=200,
        size=1000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=0,
        starred=False,
        mbid=mbid,
    )


class FakeLibraryService:
    def __init__(self, trash_songs=None):
        self.trash_songs = list(trash_songs or [])
        self.real_paths: dict[str, str | None] = {}
        self.scan_count = 0
        self.removed: list[tuple[str, str]] = []

    def list_playlists(self):
        if not self.trash_songs:
            return []
        return [PlaylistInfo("trash-pl", TRASH_PLAYLIST_NAME, len(self.trash_songs))]

    def get_playlist_detail(self, playlist_id):
        return PlaylistDetail(playlist_id, TRASH_PLAYLIST_NAME, list(self.trash_songs))

    def get_song_real_path(self, song_id):
        return self.real_paths.get(song_id)

    def remove_songs_from_playlist(self, playlist_id, song_ids):
        self.removed.append((playlist_id, song_ids[0]))
        self.trash_songs = [s for s in self.trash_songs if s.song_id not in song_ids]
        return True

    def trigger_scan(self):
        self.scan_count += 1
        return True


class FakeFeedbackService:
    def __init__(self, disabled=False, results=None):
        self.disabled = disabled
        self.results = results or {}
        self.sent: list[tuple[str, int]] = []

    def send_feedback(self, mbid, score):
        if self.disabled:
            raise ListenBrainzDisabledError()
        self.sent.append((mbid, score))
        return self.results.get(mbid, True)


@pytest.fixture
def db(tmp_path):
    config = _make_config(str(tmp_path))
    database = Database(config)
    database.initialize_schema()
    yield database
    database.close()


def _make_worker(config, db, library, feedback):
    return TrashPurge(
        config,
        library,
        feedback,
        db,
        sync_store=SyncStore(db),
        download_store=DownloadStore(db),
    )


class TestTrashSweep:
    def test_entry_is_hated_deleted_and_removed(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        real = config.paths.music_dir / "discovery" / "Deep_Cuts" / "bad.flac"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("data")
        library = FakeLibraryService(trash_songs=[_song("s1", "Bad Track", "mbid-1")])
        library.real_paths["s1"] = "discovery/Deep_Cuts/bad.flac"
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.purge_once()

        assert feedback.sent == [("mbid-1", -1)]
        assert SyncStore(db).needs_feedback("s1", HATE) is False
        assert not real.exists()
        assert library.removed == [("trash-pl", "s1")]
        assert result["trashed"] == 1
        assert library.scan_count == 1

    def test_already_synced_entry_not_resent(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        real = config.paths.music_dir / "bad.flac"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("data")
        SyncStore(db).record("s1", HATE, "mbid-1", lb_synced=1)
        library = FakeLibraryService(trash_songs=[_song("s1", "Bad Track", "mbid-1")])
        library.real_paths["s1"] = "bad.flac"
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        worker.purge_once()

        assert feedback.sent == []
        assert not real.exists()
        assert library.removed == [("trash-pl", "s1")]

    def test_disabled_lb_still_deletes_but_keeps_entry(self, db, tmp_path):
        """Feedback stays pending (retried when LB returns) while the file
        disposal is never blocked on it."""
        config = _make_config(str(tmp_path))
        real = config.paths.music_dir / "bad.flac"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("data")
        library = FakeLibraryService(trash_songs=[_song("s1", "Bad Track", "mbid-1")])
        library.real_paths["s1"] = "bad.flac"
        feedback = FakeFeedbackService(disabled=True)
        worker = _make_worker(config, db, library, feedback)

        result = worker.purge_once()

        assert not real.exists()
        assert result["trashed"] == 0
        assert result["feedback_pending"] == 1
        assert library.removed == []  # kept for the next cycle
        assert SyncStore(db).needs_feedback("s1", HATE) is True

    def test_mbid_less_entry_is_disposed(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        real = config.paths.music_dir / "bad.flac"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("data")
        library = FakeLibraryService(trash_songs=[_song("s1", "Bad Track", None)])
        library.real_paths["s1"] = "bad.flac"
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        worker.purge_once()

        assert feedback.sent == []
        assert library.removed == [("trash-pl", "s1")]
        assert SyncStore(db).needs_feedback("s1", HATE) is False

    def test_no_trash_playlist_is_a_noop(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(trash_songs=[])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.purge_once()

        assert result["trashed"] == 0
        assert feedback.sent == []

    def test_unresolvable_path_keeps_entry_in_trash(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(trash_songs=[_song("s1", "Bad Track", "mbid-1")])
        library.real_paths["s1"] = None
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.purge_once()

        assert feedback.sent == [("mbid-1", -1)]
        assert result["trashed"] == 0
        assert library.removed == []


class TestStrandedSweep:
    def _seed_stranded(self, config, username="peerone", filename="dir\\track.mp3"):
        source_dir = (
            config.paths.download_path / "complete" / "soulseek" / username
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "track.mp3").write_text("stranded")
        return source_dir

    def test_skipped_import_file_is_deleted_with_empty_dirs(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        source_dir = self._seed_stranded(config)
        store = DownloadStore(db)
        store.insert_pending(
            "search-1", "peerone", "dir\\track.mp3", 5000, False
        )
        row = db.fetch_one(
            "SELECT id FROM downloads WHERE username = 'peerone'"
        )
        store.mark_import_skipped(row["id"])

        library = FakeLibraryService(trash_songs=[])
        worker = _make_worker(config, db, library, FakeFeedbackService())

        result = worker.purge_once()

        assert result["files_deleted"]
        assert not (source_dir / "track.mp3").exists()
        # Empty parent dirs pruned up to (but not including) the downloads root.
        assert not source_dir.exists()
        assert config.paths.download_path.exists()

    def test_live_row_is_left_alone(self, db, tmp_path):
        """A row still being handled by the monitor (import not terminal)
        must not be swept."""
        config = _make_config(str(tmp_path))
        source_dir = self._seed_stranded(config)
        store = DownloadStore(db)
        store.insert_pending(
            "search-1", "peerone", "dir\\track.mp3", 5000, False
        )
        assert store.get_stranded_downloads() == []

        library = FakeLibraryService(trash_songs=[])
        worker = _make_worker(config, db, library, FakeFeedbackService())

        result = worker.purge_once()

        assert result["files_deleted"] == []
        assert (source_dir / "track.mp3").exists()

    def test_basename_fallback_resolves_file(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        source_dir = self._seed_stranded(config, filename="weird\\sub\\track.mp3")
        (source_dir / "track.mp3").write_text("stranded")
        store = DownloadStore(db)
        store.insert_pending(
            "search-1", "peerone", "weird\\sub\\track.mp3", 5000, False
        )
        row = db.fetch_one("SELECT id FROM downloads WHERE username = 'peerone'")
        store.mark_import_skipped(row["id"])

        library = FakeLibraryService(trash_songs=[])
        worker = _make_worker(config, db, library, FakeFeedbackService())

        result = worker.purge_once()

        assert result["files_deleted"]
        assert not (source_dir / "track.mp3").exists()
