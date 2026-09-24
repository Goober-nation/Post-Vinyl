"""
Tests for #19: no 'Searches' playlist is ever created for manual downloads.

T19a (gap proof, pre-fix): no code path called add_to_playlist for a manual
download in either beets profile — see git history for the pre-fix state.
T19b (spec, now implemented): SearchesPlaylistService gets a completed
manual download's track into an auto-created "Searches" playlist, the same
way RecPlaylistService does for rec categories.
"""

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.playlist_store import PlaylistStore
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.services.searches_playlist import (
    SEARCHES_PLAYLIST_NAME,
    SearchesPlaylistService,
)

import pytest


def _cfg(navidrome_enabled=True):
    class MockNavidrome:
        enabled = navidrome_enabled

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.navidrome = MockNavidrome()
    return cfg


def _song(song_id, title="Track", artist="Artist"):
    return Song(
        song_id=song_id,
        title=title,
        artist=artist,
        album="Album",
        path=f"{song_id}.flac",
        duration=200,
        size=1000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=0,
        starred=False,
        mbid=None,
    )


class FakeLibraryService:
    def __init__(self, library_songs=None):
        self.library_songs = list(library_songs or [])
        self.playlists: dict[str, list[Song]] = {}
        self.playlist_names: dict[str, str] = {}
        self._next_id = 1
        self.add_calls: list[tuple[str, list[str]]] = []

    def list_playlists(self):
        return [
            PlaylistInfo(pid, self.playlist_names[pid], len(songs))
            for pid, songs in self.playlists.items()
        ]

    def create_playlist(self, name):
        pid = f"pl-{self._next_id}"
        self._next_id += 1
        self.playlists[pid] = []
        self.playlist_names[pid] = name
        return pid

    def get_playlist_detail(self, playlist_id):
        songs = self.playlists.get(playlist_id, [])
        return PlaylistDetail(playlist_id, self.playlist_names.get(playlist_id, ""), list(songs))

    def add_to_playlist(self, playlist_id, song_ids):
        self.add_calls.append((playlist_id, list(song_ids)))
        current = {s.song_id for s in self.playlists.setdefault(playlist_id, [])}
        for sid in song_ids:
            if sid not in current:
                match = next((s for s in self.library_songs if s.song_id == sid), None)
                self.playlists[playlist_id].append(match or _song(sid))
        return True

    def search_library(self, query):
        q = query.lower()
        return [
            s
            for s in self.library_songs
            if q in s.title.lower() or q in s.artist.lower()
        ]


@pytest.fixture
def db(tmp_path):
    class MockPaths:
        data_dir = str(tmp_path / "data")

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = MockPaths()
    database = Database(cfg)
    database.initialize_schema()
    yield database
    database.close()


class TestSearchesPlaylist:
    def test_creates_playlist_and_adds_song_on_first_use(self, db):
        library = FakeLibraryService(library_songs=[_song("s1", "Outside", "Injury Reserve")])
        store = DownloadStore(db)
        service = SearchesPlaylistService(_cfg(), library, store, PlaylistStore(db))

        ok = service.add_downloaded_to_playlist("t1", "Outside", "Injury Reserve")

        assert ok is True
        assert len(library.playlists) == 1
        pid = next(iter(library.playlists))
        assert library.playlist_names[pid] == SEARCHES_PLAYLIST_NAME
        assert {s.song_id for s in library.playlists[pid]} == {"s1"}

    def test_song_not_yet_indexed_returns_false_for_retry(self, db):
        library = FakeLibraryService(library_songs=[])
        store = DownloadStore(db)
        service = SearchesPlaylistService(_cfg(), library, store, PlaylistStore(db))

        ok = service.add_downloaded_to_playlist("t1", "Outside", "Injury Reserve")

        assert ok is False
        assert library.add_calls == []

    def test_already_in_playlist_is_idempotent(self, db):
        library = FakeLibraryService(library_songs=[_song("s1", "Outside", "Injury Reserve")])
        store = DownloadStore(db)
        service = SearchesPlaylistService(_cfg(), library, store, PlaylistStore(db))

        assert service.add_downloaded_to_playlist("t1", "Outside", "Injury Reserve") is True
        assert service.add_downloaded_to_playlist("t1", "Outside", "Injury Reserve") is True
        assert len(library.add_calls) == 1

    def test_retry_unplaylisted_downloads_covers_both_beets_profiles(self, db, tmp_path):
        """Manual downloads from either search mode/profile (is_library
        True or False) both qualify — the issue named both."""
        library = FakeLibraryService(
            library_songs=[
                _song("s1", "Track One", "Artist One"),
                _song("s2", "Track Two", "Artist Two"),
            ]
        )
        store = DownloadStore(db)
        now = 1234567890
        store.insert_pending(
            search_id=None, username="peer", filename="a.mp3", size=1,
            is_rec_download=False, is_library_download=False,
        )
        store.insert_pending(
            search_id=None, username="peer", filename="b.mp3", size=1,
            is_rec_download=False, is_library_download=True,
        )
        rows = store._db.fetch_all("SELECT id FROM downloads")
        ids = [r["id"] for r in rows]
        store.mark_file_moved(ids[0], "/music/searches/a")
        store.mark_file_moved(ids[1], "/music/library/b")

        service = SearchesPlaylistService(_cfg(), library, store, PlaylistStore(db))
        intents = {ids[0]: ("Track One", "Artist One"), ids[1]: ("Track Two", "Artist Two")}
        linked = service.retry_unplaylisted_downloads(
            lambda row: intents.get(row["id"], (None, None))
        )

        assert linked == 2
        pid = next(iter(library.playlists))
        assert {s.song_id for s in library.playlists[pid]} == {"s1", "s2"}

    def test_rec_downloads_are_never_swept_here(self, db):
        """A rec download uses RecPlaylistService/recommendations.playlist_id
        instead — it must never show up in this store's unplaylisted query."""
        store = DownloadStore(db)
        store.insert_pending(
            search_id=None, username="peer", filename="rec.mp3", size=1,
            is_rec_download=True,
        )
        row = store._db.fetch_one("SELECT id FROM downloads")
        store.mark_file_moved(row["id"], "/music/discovery/rec")

        assert store.get_unplaylisted_manual_downloads() == []

    def test_navidrome_disabled_short_circuits_without_any_calls(self, db):
        """#7: navidrome.enabled=False must stop this service before it
        touches the library client at all."""
        library = FakeLibraryService(library_songs=[_song("s1", "Outside", "Injury Reserve")])
        store = DownloadStore(db)
        service = SearchesPlaylistService(
            _cfg(navidrome_enabled=False), library, store, PlaylistStore(db)
        )

        ok = service.add_downloaded_to_playlist("t1", "Outside", "Injury Reserve")

        assert ok is False
        assert library.playlists == {}
        assert library.add_calls == []
