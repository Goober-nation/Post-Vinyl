"""
Tests for album consolidation (P6.9) — the fix for albums fractured by
peer-sourced singleton imports: tracks of one album landing in differently
spelled directories and in different profile trees (live example 2026-08-14:
Terror Reid - Hot Vodka 2 split between library/ and searches/).

The subprocess `beet` calls are emulated by FakeBeet, which mirrors the
parts of beets 2.13.1 the consolidation path relies on (verified live in the
built image): `beet import` moves the source into the profile tree and
creates a library row; `beet modify -m` updates the row fields and moves the
file when the path template output changes. What is tested here is the
orchestration: grouping, canonical identity, dedupe, cross-tree moves,
renumbering, empty-dir pruning, and the import_file integration.
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.services.beets import BeetsService

_ITEM_COLUMNS = (
    "id",
    "path",
    "albumartist",
    "album",
    "title",
    "track",
    "disc",
    "mb_trackid",
    "mb_albumid",
    "added",
)


def _make_config(tmp_path):
    class Paths:
        data_dir = str(tmp_path / "data")
        music_dir = tmp_path / "music"
        searches_path = tmp_path / "music" / "searches"
        library_path = tmp_path / "music" / "library"
        discovery_familiar_path = tmp_path / "music" / "Discovery" / "Comfort_Zone"
        discovery_new_releases_path = tmp_path / "music" / "Discovery" / "New_Releases"
        discovery_exploration_path = tmp_path / "music" / "Discovery" / "Deep_Cuts"

    class Beets:
        binary = "beet"
        timeout_seconds = 120
        enabled = True

    class Config:
        pass

    cfg = Config()
    cfg.paths = Paths()
    cfg.beets = Beets()
    return cfg


def _tree_for(profile: str, config) -> Path:
    return {
        "searches": config.paths.searches_path,
        "library": config.paths.library_path,
        "discovery_familiar": config.paths.discovery_familiar_path,
        "discovery_new_releases": config.paths.discovery_new_releases_path,
        "discovery_exploration": config.paths.discovery_exploration_path,
    }[profile]


def _seed_item(
    config,
    profile: str,
    relative: str,
    *,
    artist,
    album,
    title,
    track,
    mb_trackid=None,
    mb_albumid=None,
    added=100.0,
):
    """Create a library row (full beets schema) plus its file on disk."""
    db = Path(config.paths.data_dir) / "beets" / f"{profile}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    tree = _tree_for(profile, config)
    file_path = tree / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("data")
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id INTEGER PRIMARY KEY, path BLOB, albumartist TEXT, album TEXT, "
            "title TEXT, track INTEGER, disc INTEGER, mb_trackid TEXT, "
            "mb_albumid TEXT, added REAL, album_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO items (path, albumartist, album, title, track, disc, "
            "mb_trackid, mb_albumid, added) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                relative.encode(),
                artist,
                album,
                title,
                track,
                1,
                mb_trackid,
                mb_albumid,
                added,
            ),
        )


def _rows(config, profile: str) -> list[dict]:
    db = Path(config.paths.data_dir) / "beets" / f"{profile}.db"
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT id, path, albumartist, album, title, track, mb_trackid, "
            "mb_albumid FROM items"
        ).fetchall()
    return [
        {
            "id": r[0],
            "path": Path(r[1].decode() if isinstance(r[1], bytes) else r[1]),
            "albumartist": r[2],
            "album": r[3],
            "title": r[4],
            "track": r[5],
            "mb_trackid": r[6],
            "mb_albumid": r[7],
        }
        for r in rows
    ]


def _leading_track(name: str) -> int | None:
    head = name.split(" ", 1)[0]
    return int(head) if head.isdigit() else None


class FakeBeet:
    """Emulates the beets CLI surface the consolidation path uses:
    `import` (move file into the profile tree + create a row) and
    `modify -m` (update row fields + move file).

    An import without `--set` album fields mimics beets' `asis` fallback:
    the row keeps whatever identity the file already claimed — emulated via
    `asis_artist`/`asis_album`, the "peer's tags" of the test.
    """

    def __init__(self, config):
        self.config = config
        self.calls: list[list[str]] = []
        self.fail_imports: set[int] = set()
        self.asis_artist = "Unknown"
        self.asis_album = "Unknown"
        self._import_count = 0

    def _db(self, profile: str) -> Path:
        return Path(self.config.paths.data_dir) / "beets" / f"{profile}.db"

    def _next_added(self, profile: str) -> float:
        db = self._db(profile)
        if not db.exists():
            return 1.0
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT MAX(added) FROM items").fetchone()
        return float(row[0]) + 1.0 if row and row[0] is not None else 1.0

    def _create_table(self, profile: str) -> None:
        with sqlite3.connect(str(self._db(profile))) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS items ("
                "id INTEGER PRIMARY KEY, path BLOB, albumartist TEXT, album TEXT, "
                "title TEXT, track INTEGER, disc INTEGER, mb_trackid TEXT, "
                "mb_albumid TEXT, added REAL, album_id INTEGER)"
            )

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        cfg_index = argv.index("--config")
        profile = Path(argv[cfg_index + 1]).stem
        cmd = argv[3]
        if cmd == "import":
            self._import_count += 1
            if self._import_count in self.fail_imports:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            self._import(argv[4:], profile)
        elif cmd == "modify":
            self._modify(argv[4:], profile)
        elif cmd == "write":
            pass  # tag write is not modeled — the call is recorded only
        else:
            raise AssertionError(f"unexpected beet command: {cmd}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _import(self, args, profile):
        sets: dict[str, str] = {}
        search_id = None
        source: Path | None = None
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--set":
                key, value = args[i + 1].split("=", 1)
                sets[key] = value
                i += 2
            elif arg == "--search-id":
                search_id = args[i + 1]
                i += 2
            elif arg.startswith("--"):
                i += 1
            else:
                source = Path(arg)
                i += 1
        assert source is not None
        title = source.stem
        if _leading_track(title) is not None and " " in title:
            title = title.split(" ", 1)[1]
        track = sets.get("track")
        if track is None:
            track = _leading_track(title)
        artist = sets.get("albumartist", self.asis_artist)
        album = sets.get("album", self.asis_album)
        mb_albumid = sets.get("mb_albumid")
        if track is not None:
            relative = f"{artist}/{album}/{int(track):02d} {title}{source.suffix}"
        else:
            relative = f"{artist}/{album}/{title}{source.suffix}"
        dest = _tree_for(profile, self.config) / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(dest.stem + " (1)" + dest.suffix)
        os.replace(source, dest)
        added = self._next_added(profile)
        self._create_table(profile)
        with sqlite3.connect(str(self._db(profile))) as conn:
            conn.execute(
                "INSERT INTO items (path, albumartist, album, title, track, disc, "
                "mb_trackid, mb_albumid, added) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    relative.encode(),
                    artist,
                    album,
                    title,
                    int(track) if track is not None else None,
                    1,
                    search_id,
                    mb_albumid,
                    added,
                ),
            )

    def _modify(self, args, profile):
        path_query: Path | None = None
        mods: dict[str, str] = {}
        for arg in args:
            if arg.startswith("path:"):
                path_query = Path(arg[len("path:") :])
            elif "=" in arg and not arg.startswith("--"):
                key, value = arg.split("=", 1)
                mods[key] = value
        assert path_query is not None
        db = self._db(profile)
        tree = _tree_for(profile, self.config)
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT id, path, albumartist, album, title, track, disc, "
                "mb_trackid, mb_albumid, added FROM items"
            ).fetchall()
            for row in rows:
                d = dict(zip(_ITEM_COLUMNS, row))
                resolved = Path(
                    d["path"].decode() if isinstance(d["path"], bytes) else d["path"]
                )
                if not resolved.is_absolute():
                    resolved = tree / resolved
                if resolved == path_query:
                    break
            else:
                raise AssertionError(f"modify: no item at {path_query}")
            for key, value in mods.items():
                d[key] = (
                    value
                    if key in ("albumartist", "album", "title", "mb_albumid")
                    else int(value)
                )
            new_relative = (
                f"{d['albumartist']}/{d['album']}/"
                f"{int(d['track']):02d} {d['title']}{resolved.suffix}"
            )
            new_abs = tree / new_relative
            if new_abs != resolved:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                os.replace(resolved, new_abs)
            conn.execute(
                "UPDATE items SET albumartist=?, album=?, title=?, track=?, "
                "mb_albumid=?, path=? WHERE id=?",
                (
                    d["albumartist"],
                    d["album"],
                    d["title"],
                    int(d["track"]),
                    d["mb_albumid"],
                    new_relative.encode(),
                    d["id"],
                ),
            )


class FakeMusicBrainz:
    """Canned release/recording lookups for the consolidation paths."""

    def __init__(self, release=None, recording=None):
        self.release = release
        self.recording = recording
        self.fail_release = False

    def lookup_release_tracks(self, release_mbid):
        if self.fail_release:
            raise RuntimeError("MusicBrainz down")
        return self.release

    def lookup_recording(self, mbid):
        return self.recording

    def resolve_canonical(self, title, artist, min_score=90):
        return None


def _rec(mbid: str, title: str):
    from app.services.interfaces.musicbrainz import MBRecording

    return MBRecording(mbid=mbid, title=title, artist_credit="", artist="")


def _recording(mbid: str, title: str, release_mbid: str, release_title: str):
    from app.services.interfaces.musicbrainz import MBRecording, MBRelease

    return MBRecording(
        mbid=mbid,
        title=title,
        artist_credit="A",
        artist="A",
        releases=[
            MBRelease(mbid=release_mbid, title=release_title, primary_type="Album")
        ],
    )


def _service(config, mb):
    return BeetsService(config, musicbrainz_service=mb)


@pytest.fixture
def config(tmp_path):
    return _make_config(tmp_path)


class TestGrouping:
    def test_garbage_mbid_never_isolates_a_member(self, config, tmp_path, monkeypatch):
        """Live 2026-08-14: NO GIMMIX carried mb_albumid '1yTnNouJawgOy700QENgVh'
        (not a UUID — peer's tags). A garbage MBID must not bucket the
        member into its own album; it joins the string bucket and gets the
        canonical release MBID stamped."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/03 Babe Ruthless.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Babe Ruthless",
            track=3,
            mb_trackid="10000000-0000-0000-0000-000000000003",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/12 NO GIMMIX.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="NO GIMMIX",
            track=12,
            mb_albumid="1yTnNouJawgOy700QENgVh",
            added=2.0,
        )
        mb = FakeMusicBrainz(
            release=(
                "HOT VODKA 2",
                [_rec("10000000-0000-0000-0000-000000000003", "Babe Ruthless")],
            )
        )
        service = _service(config, mb)
        groups = service._group_all()
        assert len(groups) == 1, "garbage MBID must not split the album"
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)
        summary = service.consolidate_all()
        assert summary["renamed"] >= 1
        assert {r["mb_albumid"] for r in _rows(config, "library")} == {
            "20000000-0000-0000-0000-000000000001"
        }

    def test_garbage_mbid_is_normalized_to_none(self, config, tmp_path):
        service = _service(config, FakeMusicBrainz())
        assert service._valid_mbid(None) is None
        assert service._valid_mbid("") is None
        assert service._valid_mbid("not-a-uuid") is None
        assert service._valid_mbid("1yTnNouJawgOy700QENgVh") is None
        assert (
            service._valid_mbid("04f932c6-bf3e-4094-8f89-a26f3ebfabc2")
            == "04f932c6-bf3e-4094-8f89-a26f3ebfabc2"
        )

    def test_mbid_and_string_members_group_across_profiles(self, config, tmp_path):
        """The exact Hot Vodka 2 shape: a library member carrying the release
        MBID groups with searches members that match only by normalized
        string."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/03 Babe Ruthless.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Babe Ruthless",
            track=3,
            mb_trackid="10000000-0000-0000-0000-000000000003",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "Terror Reid/Hot Vodka 2/04 The Jackpot.flac",
            artist="Terror Reid",
            album="Hot Vodka 2",
            title="The Jackpot",
            track=4,
            added=2.0,
        )
        service = _service(config, FakeMusicBrainz())
        groups = service._group_all()
        assert len(groups) == 1
        assert {m.profile for m in groups[0].members} == {"library", "searches"}

    def test_different_release_mbids_never_group(self, config, tmp_path):
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_albumid="20000000-0000-0000-0000-000000000002",
            added=2.0,
        )
        service = _service(config, FakeMusicBrainz())
        assert len(service._group_all()) == 2, "different releases stay separate"

    def test_dead_rows_do_not_seed_groups(self, config, tmp_path):
        """A row whose file is gone must not pull live members around."""
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            added=2.0,
        )
        (config.paths.searches_path / "A/One/01 X.flac").unlink()
        service = _service(config, FakeMusicBrainz())
        groups = service._group_all()
        assert len(groups) == 1
        assert [m.profile for m in groups[0].members] == ["library"]


class TestHomeProfile:
    def test_library_wins_over_majority(self, config, tmp_path):
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "A/One/02 Y.flac",
            artist="A",
            album="one",
            title="Y",
            track=2,
            added=2.0,
        )
        _seed_item(
            config,
            "searches",
            "A/One/03 Z.flac",
            artist="A",
            album="one",
            title="Z",
            track=3,
            added=3.0,
        )
        service = _service(config, FakeMusicBrainz())
        assert service._home_profile(service._group_all()[0].members) == "library"

    def test_majority_wins_without_library_member(self, config, tmp_path):
        _seed_item(
            config,
            "searches",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            added=1.0,
        )
        _seed_item(
            config,
            "discovery_familiar",
            "A/One/02 Y.flac",
            artist="A",
            album="One",
            title="Y",
            track=2,
            added=2.0,
        )
        _seed_item(
            config,
            "discovery_familiar",
            "A/One/03 Z.flac",
            artist="A",
            album="One",
            title="Z",
            track=3,
            added=3.0,
        )
        service = _service(config, FakeMusicBrainz())
        assert service._home_profile(service._group_all()[0].members) == (
            "discovery_familiar"
        )


class TestDedupe:
    def test_same_mb_trackid_keeps_one(self, config, tmp_path, monkeypatch):
        """Two copies of the same recording (different peer spellings of the
        path) — only one may survive."""
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_trackid="10000000-0000-0000-0000-000000000001",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "A/One/00 X.flac",
            artist="A",
            album="One",
            title="X",
            track=0,
            mb_trackid="10000000-0000-0000-0000-000000000001",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=2.0,
        )
        service = _service(config, FakeMusicBrainz())
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        stats, _ = service._unify_group(service._group_all()[0])
        assert stats["deduplicated"] == 1
        assert len(_rows(config, "library")) == 1
        assert len(list((config.paths.library_path / "A/One").glob("*.flac"))) == 1

    def test_matched_plus_unmatched_same_title_drops_unmatched(
        self, config, tmp_path, monkeypatch
    ):
        """The live See You Again pair: one matched copy with mb_trackid,
        one asis copy beets' own duplicate guard never caught."""
        _seed_item(
            config,
            "discovery_familiar",
            "Tyler, The Creator/Flower Boy/00 See You Again.flac",
            artist="Tyler, The Creator",
            album="Flower Boy",
            title="See You Again",
            track=0,
            mb_trackid="10000000-0000-0000-0000-000000000006",
            added=1.0,
        )
        _seed_item(
            config,
            "discovery_familiar",
            "Tyler, The Creator/Flower Boy/04 See You Again.mp3",
            artist="Tyler, The Creator",
            album="Flower Boy",
            title="See You Again",
            track=4,
            added=2.0,
        )
        service = _service(config, FakeMusicBrainz())
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        stats, _ = service._unify_group(service._group_all()[0])
        assert stats["deduplicated"] == 1
        rows = _rows(config, "discovery_familiar")
        assert len(rows) == 1
        assert rows[0]["mb_trackid"] == "10000000-0000-0000-0000-000000000006"
        assert not (
            config.paths.discovery_familiar_path
            / "Tyler, The Creator/Flower Boy/04 See You Again.mp3"
        ).exists()

    def test_unmatched_pair_keeps_earliest(self, config, tmp_path, monkeypatch):
        _seed_item(
            config,
            "searches",
            "A/One/01 X.mp3",
            artist="A",
            album="One",
            title="X",
            track=1,
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            added=2.0,
        )
        service = _service(config, FakeMusicBrainz())
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        stats, _ = service._unify_group(service._group_all()[0])
        assert stats["deduplicated"] == 1
        rows = _rows(config, "searches")
        assert len(rows) == 1
        assert rows[0]["path"].suffix == ".mp3", "earliest copy survives"

    def test_same_release_position_dedupes_across_spellings(
        self, config, tmp_path, monkeypatch
    ):
        """The live MB-search dupe: a re-downloaded 'Tha Jackpot' (real
        title, mb_trackid) sits beside the earlier asis 'The Jackpot'.
        Different titles, different MBIDs — only the shared release
        position 4 gives them away. The release-verified copy survives."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/04 The Jackpot.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="The Jackpot",
            track=4,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/04 Tha Jackpot.mp3",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Tha Jackpot",
            track=4,
            mb_trackid="10000000-0000-0000-0000-000000000004",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=2.0,
        )
        mb = FakeMusicBrainz(
            release=(
                "HOT VODKA 2",
                [
                    _rec("r-1", "Intro 2"),
                    _rec("r-2", "Run It Back"),
                    _rec("r-3", "Babe Ruthless"),
                    _rec("r-4", "Tha Jackpot"),
                ],
            )
        )
        service = _service(config, mb)
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)
        summary = service.consolidate_all()
        assert summary["deduplicated"] == 1
        rows = _rows(config, "library")
        assert len(rows) == 1
        assert rows[0]["title"] == "Tha Jackpot", "the release-verified copy survives"
        assert not (
            config.paths.library_path / "Terror Reid/HOT VODKA 2/04 The Jackpot.flac"
        ).exists()

    def test_same_position_without_release_keeps_both(
        self, config, tmp_path, monkeypatch
    ):
        """Without a resolved release there is no authority for 'one track
        per position' — two members claiming position 4 both stay."""
        _seed_item(
            config,
            "library",
            "A/One/04 X.flac",
            artist="A",
            album="One",
            title="X",
            track=4,
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "A/One/04 Y.mp3",
            artist="A",
            album="One",
            title="Y",
            track=4,
            added=2.0,
        )
        mb = FakeMusicBrainz()
        mb.fail_release = True
        service = _service(config, mb)
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        summary = service.consolidate_all()
        assert summary["deduplicated"] == 0
        assert len(_rows(config, "library")) == 2


class TestRenumbering:
    def test_renumber_from_release_by_recording_mbid(
        self, config, tmp_path, monkeypatch
    ):
        """00 Run It Back (recording on the release at position 2) becomes
        02 Run It Back — single member, release derived from mb_trackid."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/00 Run It Back.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Run It Back",
            track=0,
            mb_trackid="10000000-0000-0000-0000-000000000002",
            added=1.0,
        )
        mb = FakeMusicBrainz(
            release=(
                "HOT VODKA 2",
                [
                    _rec("10000000-0000-0000-0000-000000000001", "Intro 2"),
                    _rec("10000000-0000-0000-0000-000000000002", "Run It Back"),
                ],
            ),
            recording=_recording(
                "10000000-0000-0000-0000-000000000002",
                "Run It Back",
                "20000000-0000-0000-0000-000000000001",
                "HOT VODKA 2",
            ),
        )
        service = _service(config, mb)
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)
        summary = service.consolidate_all()
        assert summary["renamed"] >= 1
        rows = _rows(config, "library")
        assert rows[0]["track"] == 2
        assert rows[0]["path"] == Path("Terror Reid/HOT VODKA 2/02 Run It Back.flac")
        assert (
            config.paths.library_path / "Terror Reid/HOT VODKA 2/02 Run It Back.flac"
        ).exists()
        # A matched member was tagged by beets at import — no tag write.
        assert not any(c[3] == "write" for c in fake.calls)

    def test_title_match_renumbers_unique_title_only(
        self, config, tmp_path, monkeypatch
    ):
        _seed_item(
            config,
            "library",
            "A/One/00 Tha Jackpot.flac",
            artist="A",
            album="One",
            title="Tha Jackpot",
            track=0,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "A/One/00 Daz My Bitch.flac",
            artist="A",
            album="One",
            title="Daz My Bitch",
            track=0,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=2.0,
        )
        mb = FakeMusicBrainz(
            release=("One", [_rec("r-1", "Tha Jackpot"), _rec("r-2", "Daz My Bitch")])
        )
        service = _service(config, mb)
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        service.consolidate_all()
        rows = {r["title"]: r for r in _rows(config, "library")}
        assert rows["Tha Jackpot"]["track"] == 1
        assert rows["Daz My Bitch"]["track"] == 2

    def test_ambiguous_title_never_renumbers(self, config, tmp_path, monkeypatch):
        release = (
            "One",
            [_rec("r-1", "Intro"), _rec("r-2", "Outro"), _rec("r-3", "Intro")],
        )
        _seed_item(
            config,
            "library",
            "A/One/00 Intro.flac",
            artist="A",
            album="One",
            title="Intro",
            track=0,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        service = _service(config, FakeMusicBrainz(release=release))
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        service.consolidate_all()
        assert _rows(config, "library")[0]["track"] == 0

    def test_mb_down_falls_back_to_majority_spelling(
        self, config, tmp_path, monkeypatch
    ):
        _seed_item(
            config,
            "library",
            "A/Hot Vodka 2/03 X.flac",
            artist="A",
            album="Hot Vodka 2",
            title="X",
            track=3,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "searches",
            "A/HOT VODKA 2/04 Y.flac",
            artist="A",
            album="HOT VODKA 2",
            title="Y",
            track=4,
            added=2.0,
        )
        mb = FakeMusicBrainz(release=None)
        mb.fail_release = True
        service = _service(config, mb)
        monkeypatch.setattr("subprocess.run", FakeBeet(config))
        service.consolidate_all()
        rows = _rows(config, "library")
        assert len(rows) == 2, "the searches member still joins the library album"
        # Tie falls to the first-seen (mbid-bearing library) member's spelling.
        assert {r["album"] for r in rows} == {"Hot Vodka 2"}


class TestCrossProfileMove:
    def test_sweep_merges_hot_vodka_2(self, config, tmp_path, monkeypatch):
        """The live shape end-to-end: 3 library tracks (one with the release
        MBID) + 2 searches tracks in the wrong spelling; the sweep moves
        everything into library, unifies spelling, renumbers, prunes the
        searches dir."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/00 Run It Back.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Run It Back",
            track=0,
            mb_trackid="10000000-0000-0000-0000-000000000002",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/03 Babe Ruthless.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Babe Ruthless",
            track=3,
            mb_trackid="10000000-0000-0000-0000-000000000003",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=2.0,
        )
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/32 Remember When.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Remember When",
            track=32,
            added=3.0,
        )
        _seed_item(
            config,
            "searches",
            "Terror Reid/Hot Vodka 2/04 The Jackpot.flac",
            artist="Terror Reid",
            album="Hot Vodka 2",
            title="The Jackpot",
            track=4,
            added=4.0,
        )
        _seed_item(
            config,
            "searches",
            "Terror Reid/Hot Vodka 2/05 Daz Ma Bitch.flac",
            artist="Terror Reid",
            album="Hot Vodka 2",
            title="Daz Ma Bitch",
            track=5,
            added=5.0,
        )
        tracks = [
            _rec("10000000-0000-0000-0000-000000000001", "Intro 2"),
            _rec("10000000-0000-0000-0000-000000000002", "Run It Back"),
            _rec("10000000-0000-0000-0000-000000000003", "Babe Ruthless"),
            _rec("10000000-0000-0000-0000-000000000004", "Tha Jackpot"),
            _rec("10000000-0000-0000-0000-000000000005", "Daz My Bitch"),
        ]
        mb = FakeMusicBrainz(release=("HOT VODKA 2", tracks))
        service = _service(config, mb)
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)

        summary = service.consolidate_all()

        assert summary["moved"] == 2
        assert _rows(config, "searches") == [], "searches rows must be gone"
        library_rows = _rows(config, "library")
        assert len(library_rows) == 5
        assert {r["album"] for r in library_rows} == {"HOT VODKA 2"}
        # Every member carries the release MBID: Navidrome groups files by
        # MBID when present, so a mixed set (one tagged, four not) stays
        # fractured into separate albums even after spelling converges.
        assert {r["mb_albumid"] for r in library_rows} == {
            "20000000-0000-0000-0000-000000000001"
        }
        run_it_back = next(r for r in library_rows if r["title"] == "Run It Back")
        assert run_it_back["track"] == 2, "renumbered from the release"
        assert not (config.paths.searches_path / "Terror Reid").exists(), (
            "emptied searches dirs must be pruned"
        )
        for row in library_rows:
            assert (config.paths.library_path / row["path"]).exists()

        # asis-origin members (no mb_trackid) get a tag write: beets' asis
        # fallback updates the row but never writes tags, and Navidrome
        # groups by tags — the file must be synced to the canonical row.
        writes = [c for c in fake.calls if c[3] == "write"]
        assert len(writes) == 3, "the three unmatched members need tag writes"
        for call in writes:
            path_arg = next(a for a in call if a.startswith("path:"))
            assert config.paths.library_path in Path(path_arg[5:]).parents

    def test_converged_sweep_is_a_noop(self, config, tmp_path, monkeypatch):
        """The live bug: the sweep reported `renamed: 3` on every run for
        Hot Vodka 2 because asis-origin members got an idempotent `beet
        write` every sweep and the counter counted it as a rename. Once a
        sweep has converged — spelling + mb_albumid stamped, files synced —
        the next sweep must report zero renames."""
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/03 Babe Ruthless.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="Babe Ruthless",
            track=3,
            mb_trackid="10000000-0000-0000-0000-000000000003",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        _seed_item(
            config,
            "library",
            "Terror Reid/HOT VODKA 2/04 The Jackpot.flac",
            artist="Terror Reid",
            album="HOT VODKA 2",
            title="The Jackpot",
            track=4,
            added=2.0,
        )
        mb = FakeMusicBrainz(
            release=(
                "HOT VODKA 2",
                [_rec("10000000-0000-0000-0000-000000000003", "Babe Ruthless")],
            )
        )
        service = _service(config, mb)
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)
        # The asis file's tags match its row after the first sweep.
        monkeypatch.setattr(BeetsService, "_file_tags_match_row", lambda self, m: True)

        first = service.consolidate_all()
        assert first["renamed"] >= 1, "first sweep stamps the release mb_albumid"
        assert {r["mb_albumid"] for r in _rows(config, "library")} == {
            "20000000-0000-0000-0000-000000000001"
        }

        second = service.consolidate_all()
        assert second["renamed"] == 0
        assert second["moved"] == 0
        assert second["deduplicated"] == 0

    def test_per_import_joins_library_album(self, config, tmp_path, monkeypatch):
        """A manual (searches) download of an album that lives in library/
        joins the library album immediately — import_file returns the
        library-tree path."""
        _seed_item(
            config,
            "library",
            "A/HOT VODKA 2/03 Babe Ruthless.flac",
            artist="A",
            album="HOT VODKA 2",
            title="Babe Ruthless",
            track=3,
            mb_trackid="10000000-0000-0000-0000-000000000003",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        fake = FakeBeet(config)
        fake.asis_artist = "A"
        fake.asis_album = "Hot Vodka 2"
        monkeypatch.setattr("subprocess.run", fake)
        mb = FakeMusicBrainz(
            release=(
                "HOT VODKA 2",
                [_rec("10000000-0000-0000-0000-000000000003", "Babe Ruthless")],
            )
        )
        service = _service(config, mb)
        source = tmp_path / "src" / "04 The Jackpot.flac"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        result = service.import_file(source, is_rec=False)

        assert result.ok
        # The re-import lands at the canonical album path; the 04 prefix is
        # beets' job from the file's embedded track (not emulated here).
        assert result.target_path == config.paths.library_path / (
            "A/HOT VODKA 2/The Jackpot.flac"
        )
        assert _rows(config, "searches") == []
        assert len(_rows(config, "library")) == 2

    def test_per_import_dedupe_returns_duplicate(self, config, tmp_path, monkeypatch):
        """Re-downloading a track that's already in the library: the new
        copy is deleted by consolidation and the import is terminal."""
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_trackid="10000000-0000-0000-0000-000000000001",
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        fake = FakeBeet(config)
        fake.asis_artist = "A"
        fake.asis_album = "One"
        monkeypatch.setattr("subprocess.run", fake)
        service = _service(
            config,
            FakeMusicBrainz(
                release=("One", [_rec("10000000-0000-0000-0000-000000000001", "X")])
            ),
        )
        source = tmp_path / "src" / "01 X.flac"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        result = service.import_file(source, is_rec=False)

        assert result.duplicate is True
        assert result.handled is True
        assert len(_rows(config, "library")) == 1, "the old copy survives"
        assert (config.paths.library_path / "A/One/01 X.flac").exists()
        assert not (config.paths.searches_path / "A").exists(), (
            "the searches copy was deleted and its dirs pruned"
        )

    def test_move_failure_leaves_member_in_place(self, config, tmp_path, monkeypatch):
        """A failed re-import must not delete the origin row/file — the
        member stays for the next sweep, and import_file still succeeds."""
        _seed_item(
            config,
            "library",
            "A/One/01 X.flac",
            artist="A",
            album="One",
            title="X",
            track=1,
            mb_albumid="20000000-0000-0000-0000-000000000001",
            added=1.0,
        )
        fake = FakeBeet(config)
        fake.asis_artist = "A"
        fake.asis_album = "One"
        fake.fail_imports = {2}
        monkeypatch.setattr("subprocess.run", fake)
        service = _service(config, FakeMusicBrainz(release=("One", [])))
        source = tmp_path / "src" / "02 Y.flac"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        result = service.import_file(source, is_rec=False)

        assert result.ok
        assert result.target_path == config.paths.searches_path / "A/One/Y.flac"
        assert len(_rows(config, "searches")) == 1
        assert (config.paths.searches_path / "A/One/Y.flac").exists()


class TestImportIntegration:
    def test_single_unmatched_member_is_left_alone(self, config, tmp_path, monkeypatch):
        """A lone asis member is its own canonical: no extra beets calls."""
        fake = FakeBeet(config)
        fake.asis_artist = "A"
        fake.asis_album = "Hot Vodka 2"
        monkeypatch.setattr("subprocess.run", fake)
        service = _service(config, FakeMusicBrainz())
        source = tmp_path / "src" / "04 The Jackpot.flac"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        result = service.import_file(source, is_rec=False)

        assert result.ok
        assert [c[3] for c in fake.calls] == ["import"], (
            "nothing to canonicalize -> no modify"
        )

    def test_untagged_import_never_consolidates(self, config, tmp_path, monkeypatch):
        fake = FakeBeet(config)
        monkeypatch.setattr("subprocess.run", fake)
        service = _service(config, FakeMusicBrainz())
        source = tmp_path / "src" / "00.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        result = service.import_file(source, is_rec=False)

        assert result.ok
        assert all(c[3] == "import" for c in fake.calls)


def _write_flac(path: Path, *, artist, album, title, track):
    """A real FLAC (minimal STREAMINFO + tags) for `_file_tags_match_row`."""
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        b"\x00\x00"  # min block size
        + b"\x00\x00"  # max block size
        + b"\x00\x00\x00"  # min frame size
        + b"\x00\x00\x00"  # max frame size
        + b"\x0a\xc4"  # sample rate 44100, first 16 bits
        + b"\x40"  # sample-rate tail 4 | channels 1 | bps head
        + b"\x00" * 5  # bps tail + total samples
        + b"\x00" * 16  # MD5
    )
    path.write_bytes(b"fLaC" + b"\x80\x00\x00\x22" + streaminfo)
    audio = FLAC(path)
    audio["albumartist"] = artist
    audio["album"] = album
    audio["title"] = title
    if track is not None:
        audio["tracknumber"] = str(track)
    audio.save()


class TestTagSyncCheck:
    """`_file_tags_match_row`: skips the asis tag write when the file
    already matches its row, so a converged sweep reports zero renames."""

    def _member(self, config, path, *, artist, album, title, track):
        from app.services.beets import _AlbumMember

        return _AlbumMember(
            profile="library",
            item_id=1,
            path=path,
            albumartist=artist,
            album=album,
            title=title,
            track=track,
            mb_trackid=None,
            mb_albumid=None,
            added=1.0,
        )

    def test_matching_file_needs_no_write(self, config, tmp_path):
        path = config.paths.library_path / "A/One/01 X.flac"
        _write_flac(path, artist="A", album="One", title="X", track=1)
        service = _service(config, FakeMusicBrainz())
        member = self._member(config, path, artist="A", album="One", title="X", track=1)
        assert service._file_tags_match_row(member) is True

    def test_mismatched_album_needs_write(self, config, tmp_path):
        path = config.paths.library_path / "A/One/01 X.flac"
        _write_flac(path, artist="A", album="Hot Vodka 2", title="X", track=1)
        service = _service(config, FakeMusicBrainz())
        member = self._member(
            config, path, artist="A", album="HOT VODKA 2", title="X", track=1
        )
        assert service._file_tags_match_row(member) is False

    def test_mismatched_track_needs_write(self, config, tmp_path):
        path = config.paths.library_path / "A/One/01 X.flac"
        _write_flac(path, artist="A", album="One", title="X", track=1)
        service = _service(config, FakeMusicBrainz())
        member = self._member(config, path, artist="A", album="One", title="X", track=9)
        assert service._file_tags_match_row(member) is False

    def test_missing_tracknumber_needs_write(self, config, tmp_path):
        path = config.paths.library_path / "A/One/X.flac"
        _write_flac(path, artist="A", album="One", title="X", track=None)
        service = _service(config, FakeMusicBrainz())
        member = self._member(config, path, artist="A", album="One", title="X", track=1)
        assert service._file_tags_match_row(member) is False

    def test_unparseable_file_needs_write(self, config, tmp_path):
        path = config.paths.library_path / "A/One/01 X.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data")
        service = _service(config, FakeMusicBrainz())
        member = self._member(config, path, artist="A", album="One", title="X", track=1)
        assert service._file_tags_match_row(member) is False
