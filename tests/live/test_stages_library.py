"""
S11–S13: the last mile — from "a file landed in the music tree" to "the user
opens Navidrome and plays it".

Everything before S11 is about acquiring bytes. These three stages are about
whether the user ever *sees* them:

    S11  navidrome indexes it   the file is in Navidrome's library after a scan,
                                measured per tree, with the latency it took
    S12  playlist correct       the recs playlist exists and holds what it
                                should — including tracks that arrived by
                                download rather than by already being there
    S13  user can find it       searching the way a user searches (by artist,
                                by title) returns the track, with the metadata
                                the player will display

Why these are graded separately from everything upstream
--------------------------------------------------------
A download that completes, imports cleanly and is tagged perfectly is still a
total failure from where the user sits if Navidrome never indexed it or the
playlist never got it. Both of those have happened on this stack:

* **S11 / the mount.** `docker-compose.yml` mounted Navidrome's Discovery tree
  one level too deep (`${MUSIC_HOST_DIR}/discovery/Discovery`). That host path
  does not exist, virtiofs answered EPERM, and Navidrome's walk of the library
  *root* died on it — `Error loading dir. Skipping error=lstat /music/./discovery:
  operation not permitted path=.`. With `ND_SCANNER_PURGEMISSING=always` the
  whole library was then purged: `Purged missing items from the database
  mediaFiles=5`, and `getIndexes` started answering "Library not found or
  empty". One wrong mount emptied everything, not just the tree it named.
  `test_s11_scan_reads_every_tree` is the test that keeps that fixed: it does
  not care how the trees are mounted, only that a scan reads all of them and
  purges nothing it should not.

* **S12 / the playlist.** `RecPuller._run_pull` adds songs to the playlist in
  exactly one place (step 7), and only for recs that were **already in the
  library at pull time**. A rec that gets queued, downloads, imports and is
  indexed is marked `downloaded` in the ledger and then never touched again —
  nothing in the codebase adds it to the playlist afterwards. The tests here
  measure that gap instead of asserting it in prose.

No downloads
------------
Nothing in this module queues a transfer or triggers a rec pull (a pull queues
downloads). Every test grades state that already exists, and every stage-grading
helper is exported so the later download-wave runner can call it against a
freshly imported track. See `RUNNER NOTES` at the bottom of this docstring.

RUNNER NOTES — what these need from the run wave
------------------------------------------------
`grade_s11_indexed(...)`, `grade_s12_playlist(...)` and `grade_s13_findable(...)`
are the entry points. After the runner completes a real download + import it
should call, in order:

    r11 = grade_s11_indexed(probes, scorecard, path=<imported file>,
                            run_id=..., scenario=..., track=<corpus Track>,
                            disk_at=<time.time() when the file appeared>)
    if r11.verdict is Verdict.PASS:
        r13 = grade_s13_findable(probes, scorecard, path=..., run_id=...,
                                 scenario=..., track=...)

and after a rec pull it should call:

    grade_s12_playlist(probes, stack, scorecard, run_id=..., scenario=...,
                       since_id=<max(recommendations.id) before the pull>)

`since_id` scopes the ledger comparison to that pull; omit it to grade the whole
history (what the standalone tests here do).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from tests.live.corpus import CORPUS, Track
from tests.live.harness import REPO_ROOT
from tests.live.probes.contract import Stage, StageResult, Verdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Every playlist this module creates starts with this. Nothing else in the
#: user's Navidrome may ever be confused for test output, and a stray one is
#: greppable months later.
TEST_PLAYLIST_PREFIX = "zz-musica-live-test"

#: How long to keep asking Navidrome for a track after a scan says it finished.
#: Navidrome answers `getScanStatus.scanning=false` slightly before the rows are
#: queryable, so "the scan is done" and "the track is findable" are not the same
#: instant — which is exactly the latency S11 exists to measure.
FIND_TIMEOUT_S = 90.0
FIND_POLL_S = 1.0

#: Scan-log lines that mean the scanner could not read part of the tree. The
#: mount bug produced all three. Any of them is an S11 failure even if the
#: track under test happens to be findable, because the *next* scan is what
#: empties the library.
SCAN_ALARM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"Skipping unreadable directory", "scanner could not read a directory"),
    (r"Error loading dir\. Skipping", "scanner abandoned a directory walk"),
    (r"operation not permitted", "EPERM from the music mount"),
    (r"Error getting fileInfo", "scanner could not stat a path"),
)

#: A purge is only alarming when it removes files that are still on disk. The
#: count is captured either way — a run that purges hundreds is worth seeing.
PURGE_RE = re.compile(r"Purged missing items from the database.*mediaFiles=(\d+)")

AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _env(key: str, default: str = "") -> str:
    """Read a var from the process env, falling back to the repo `.env`.

    Same source `docker compose` reads, so the tests never need their own copy
    of the credentials.
    """
    if os.environ.get(key):
        return os.environ[key]
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def norm(value: str | None) -> str:
    """Compare-safe form of a title/artist.

    Necessary, not cosmetic: the live ledger holds `Charlie’s Inferno` with a
    typographic apostrophe while the corpus spells it `Charlie's Inferno`, and
    tags round-trip through beets, the filesystem and Navidrome picking up
    accent and case differences on the way. Without this a real match reads as
    a miss and the report blames the pipeline for a string comparison.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.replace("’", "'").replace("`", "'").replace("´", "'")
    folded = re.sub(r"[^a-z0-9]+", " ", folded.casefold())
    return " ".join(folded.split())


def strip_feat(artist: str | None) -> str:
    """`Alesso feat. Tove Lo` -> `Alesso`. Also handles ft./featuring/&/with."""
    if not artist:
        return ""
    return re.split(
        r"\s+(?:feat\.?|ft\.?|featuring|with|&|,)\s+", artist, maxsplit=1
    )[0].strip()


def corpus_track_for(title: str | None, artist: str | None) -> Track | None:
    """The corpus entry a file corresponds to, if any.

    Files that are not corpus tracks are still graded — against their own tags
    rather than against an expectation — so this returning None is normal.
    """
    nt, na = norm(title), norm(strip_feat(artist))
    for track in CORPUS:
        if norm(track.title) == nt and norm(track.expect_albumartist) == na:
            return track
    for track in CORPUS:
        if norm(track.title) == nt:
            return track
    return None


def new_run_id(label: str) -> str:
    return f"{label}-{int(time.time())}"


# ---------------------------------------------------------------------------
# A deliberately user-shaped Subsonic client
# ---------------------------------------------------------------------------


class UserSearch:
    """Navidrome as a *person* uses it, not as a lookup.

    `NavidromeProbe.find_song(title, artist)` answers "is this exact thing in
    the library". S13 is a different question: someone types `Nujabes` into the
    search box — does their track come back, and does the row show the right
    artist, album, track number and duration? That needs a raw `search3` query
    and the full song payload, which the probe contract does not expose.

    Independent of `app/services/navidrome_library.py` on purpose: if the code
    under test and the test share a bug, the test proves nothing. It is also
    independent of the probe, which is a second opinion rather than a
    duplication — S13 failing while the probe finds the track by exact title is
    itself a finding (the track is indexed but not *discoverable*).
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url or _env("NAVIDROME_TEST_URL", "http://localhost:8090")
        ).rstrip("/")
        self.user = _env("NAVIDROME_USERNAME")
        self.password = _env("NAVIDROME_PASSWORD")
        self._session = requests.Session()

    def _auth(self) -> dict:
        salt = secrets.token_hex(8)
        token = hashlib.md5(
            (self.password + salt).encode()
        ).hexdigest()
        return {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "musica-live-s13",
            "f": "json",
        }

    def call(self, endpoint: str, **params: Any) -> dict:
        resp = self._session.get(
            f"{self.base_url}/rest/{endpoint}",
            params={**self._auth(), **params},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("subsonic-response", {})

    def usable(self) -> bool:
        try:
            return self.call("ping").get("status") == "ok"
        except Exception:  # noqa: BLE001 — any failure means "cannot ask Navidrome"
            return False

    def search_songs(self, query: str, limit: int = 100) -> list[dict]:
        """What the search box does: one free-text query, songs back."""
        result = self.call(
            "search3",
            query=query,
            songCount=limit,
            albumCount=0,
            artistCount=0,
        ).get("searchResult3", {})
        songs = result.get("song", [])
        return [songs] if isinstance(songs, dict) else list(songs)

    def all_songs(self, limit: int = 500) -> list[dict]:
        """Every song Navidrome holds — `getRandomSongs` with a big size is the
        only Subsonic call that enumerates without knowing an id."""
        songs = self.call("getRandomSongs", size=limit).get("randomSongs", {}).get(
            "song", []
        )
        return [songs] if isinstance(songs, dict) else list(songs)

    def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """`songIdToAdd`, not `songId` — Subsonic's spelling, and a documented
        gotcha on this project."""
        resp = self.call(
            "updatePlaylist", playlistId=playlist_id, songIdToAdd=song_ids
        )
        return resp.get("status") == "ok"

    def playlist(self, playlist_id: str) -> dict:
        return self.call("getPlaylist", id=playlist_id).get("playlist", {})


@pytest.fixture(scope="module")
def user_search() -> UserSearch:
    client = UserSearch()
    if not client.usable():
        pytest.fail(
            f"Navidrome did not answer a Subsonic ping at {client.base_url} as "
            f"user {client.user!r}. S13 grades the user-facing search path, so "
            f"there is nothing meaningful to skip to."
        )
    return client


# ---------------------------------------------------------------------------
# Tree discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """One music tree, on the host, as musica writes it."""

    name: str
    root: Path

    @property
    def audio_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            p
            for p in self.root.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )


def music_trees(stack) -> dict[str, Tree]:
    """The Discovery and Searches trees as host paths.

    Derived from musica's own `/api/config` (`paths.*`) plus `MUSIC_HOST_DIR`,
    so a config change moves the tests with it. Hard-coding either half is how
    the compose mount drifted from `config.toml` in the first place.
    """
    host_root = Path(_env("MUSIC_HOST_DIR"))
    paths = stack.client.get_config().get("paths", {})
    return {
        "Discovery": Tree("Discovery", host_root / paths.get("discovery_dir", "Discovery")),
        "Searches": Tree("Searches", host_root / paths.get("searches_dir", "Searches")),
    }


# ---------------------------------------------------------------------------
# Stage graders — the API the download-wave runner calls
# ---------------------------------------------------------------------------


def wait_until_findable(
    probes,
    title: str,
    artist: str,
    timeout: float = FIND_TIMEOUT_S,
) -> tuple[dict | None, float]:
    """Poll Navidrome until the track appears. Returns (song, seconds waited).

    Polling rather than a single check because Navidrome's scan is
    asynchronous: `scanning=false` lands before the rows are queryable, so a
    one-shot check turns a slow index into a false "never indexed".
    """
    start = time.monotonic()
    deadline = start + timeout
    while True:
        song = probes.navidrome.find_song(title, artist)
        if song:
            return song, time.monotonic() - start
        if time.monotonic() >= deadline:
            return None, time.monotonic() - start
        time.sleep(FIND_POLL_S)


def scan_and_time(probes) -> float:
    """Trigger a scan, block until Navidrome says it finished, return seconds."""
    start = time.monotonic()
    probes.navidrome.trigger_scan(wait=True)
    return time.monotonic() - start


def scan_alarms(log_text: str) -> tuple[list[str], int | None]:
    """(alarm lines, purged media file count) from a slice of Navidrome's log."""
    alarms: list[str] = []
    for line in log_text.splitlines():
        for pattern, why in SCAN_ALARM_PATTERNS:
            if re.search(pattern, line):
                alarms.append(f"{why}: {line.strip()[:300]}")
                break
    purged: int | None = None
    for match in PURGE_RE.finditer(log_text):
        purged = (purged or 0) + int(match.group(1))
    return alarms, purged


def grade_s11_indexed(
    probes,
    scorecard,
    *,
    path: Path,
    run_id: str,
    scenario: str,
    track: Track | None = None,
    tree: str | None = None,
    disk_at: float | None = None,
    scan: bool = True,
) -> StageResult:
    """Is the file at `path` in Navidrome, and how long did that take?

    `disk_at` is the wall-clock moment the file appeared in the tree (the
    runner knows it; the standalone tests below use the file's mtime). It turns
    the reported latency from "how long the scan took" into the number the user
    actually experiences: **file on disk -> findable**.
    """
    tags = probes.tags.read(path)
    title = tags.title or path.stem
    artist = tags.albumartist or tags.artist or ""

    scan_s = scan_and_time(probes) if scan else 0.0
    song, find_s = wait_until_findable(probes, title, artist)

    disk_to_findable = None
    if disk_at is not None:
        disk_to_findable = round(time.time() - disk_at, 2)

    evidence = {
        "path": str(path),
        "tree": tree,
        "queried_title": title,
        "queried_artist": artist,
        "scan_s": round(scan_s, 2),
        "find_after_scan_s": round(find_s, 2),
        "disk_to_findable_s": disk_to_findable,
        "song_id": (song or {}).get("id"),
        "navidrome_song_count": probes.navidrome.song_count(),
    }
    ok = song is not None
    detail = (
        f"indexed in {scan_s + find_s:.1f}s after scan trigger"
        if ok
        else f"NOT in Navidrome {FIND_TIMEOUT_S:.0f}s after a completed scan — "
        f"queried title={title!r} artist={artist!r}"
    )
    return scorecard.grade(
        Stage.S11_NAVIDROME_INDEXED,
        ok,
        scenario=scenario,
        run_id=run_id,
        track=track.title if track else title,
        tier=track.tier.value if track else None,
        latency_s=round(scan_s + find_s, 2),
        detail=detail,
        evidence=evidence,
    )


def grade_s13_findable(
    probes,
    scorecard,
    user_search: UserSearch,
    *,
    path: Path,
    run_id: str,
    scenario: str,
    track: Track | None = None,
) -> StageResult:
    """Would a user find this track, and does the row read correctly?

    Two searches, because they fail for different reasons: by **artist** (the
    common case — a track that cannot be found under its own artist's name is
    unreachable no matter how well it imported) and by **title**. Then the
    displayed metadata is graded: artist, album, title, track number, duration.
    A row missing its duration or track number is a track the user can find but
    not trust.
    """
    tags = probes.tags.read(path)
    title = tags.title or path.stem
    artist = tags.albumartist or tags.artist or ""
    expect_album = track.expect_album if track else tags.album
    expect_artist = track.expect_albumartist if track else artist

    by_artist = user_search.search_songs(strip_feat(artist) or artist)
    by_title = user_search.search_songs(title)

    def _matches(songs: list[dict]) -> dict | None:
        for song in songs:
            if norm(song.get("title")) == norm(title):
                return song
        return None

    hit_artist = _matches(by_artist)
    hit_title = _matches(by_title)
    song = hit_artist or hit_title

    problems: list[str] = []
    if hit_artist is None:
        problems.append(
            f"searching the artist {strip_feat(artist)!r} does not return "
            f"{title!r} ({len(by_artist)} songs came back)"
        )
    if hit_title is None:
        problems.append(
            f"searching the title {title!r} does not return it "
            f"({len(by_title)} songs came back)"
        )

    shown: dict[str, Any] = {}
    if song is not None:
        shown = {
            "title": song.get("title"),
            "artist": song.get("artist"),
            "album": song.get("album"),
            "track": song.get("track"),
            "duration": song.get("duration"),
            "id": song.get("id"),
        }
        if norm(song.get("artist")) not in {
            norm(expect_artist),
            norm(strip_feat(expect_artist)),
            norm(artist),
        }:
            problems.append(
                f"displayed artist {song.get('artist')!r} != expected "
                f"{expect_artist!r}"
            )
        if expect_album and norm(song.get("album")) != norm(expect_album):
            problems.append(
                f"displayed album {song.get('album')!r} != expected {expect_album!r}"
            )
        if not song.get("duration"):
            problems.append("no duration — the player cannot show a seek bar")
        if song.get("track") in (None, 0) and tags.track:
            problems.append(
                f"no track number displayed, though the file is tagged "
                f"track={tags.track}"
            )

    ok = song is not None and not problems
    return scorecard.grade(
        Stage.S13_USER_CAN_FIND,
        ok,
        scenario=scenario,
        run_id=run_id,
        track=track.title if track else title,
        tier=track.tier.value if track else None,
        detail="; ".join(problems) if problems else "found by artist and by title",
        evidence={
            "path": str(path),
            "searched_artist": strip_feat(artist),
            "searched_title": title,
            "found_by_artist": hit_artist is not None,
            "found_by_title": hit_title is not None,
            "displayed": shown,
            "artist_query_hits": len(by_artist),
            "title_query_hits": len(by_title),
        },
    )


# --- S12 ------------------------------------------------------------------


@dataclass
class PlaylistAudit:
    """The whole S12 question in one object: what the pull decided, versus what
    the user's playlist actually holds."""

    playlist_name: str
    playlist_id: str | None
    playlist_titles: set[str]
    in_library_recs: list[dict]
    downloaded_recs: list[dict]
    queued_recs: list[dict]
    #: `downloaded` recs whose track is not in the playlist. The headline.
    downloaded_missing: list[dict]
    #: `in_library` recs not in the playlist — a different bug if it is ever
    #: non-empty, because those are the only ones the code even tries to add.
    in_library_missing: list[dict]
    #: `downloaded` recs Navidrome *does* have indexed but the playlist does
    #: not. Nothing can excuse these: the song exists, with an id, right now.
    downloaded_indexed_but_missing: list[dict]

    @property
    def decided(self) -> int:
        return len(self.in_library_recs) + len(self.downloaded_recs)

    @property
    def gap(self) -> int:
        """Recs the pull decided on that never reached the playlist."""
        return len(self.downloaded_missing) + len(self.in_library_missing)


def audit_playlist(probes, stack, *, since_id: int | None = None) -> PlaylistAudit:
    """Compare musica's recommendation ledger against the real playlist.

    The ledger is the record of what a pull *decided*: `in_library` rows are
    what it tried to add, `downloaded` rows are what it fetched instead. The
    playlist is what the user has. The difference is the finding.
    """
    playlist_name = stack.client.get_config()["recs"]["playlist_name"]
    match = next(
        (
            p
            for p in probes.navidrome.list_playlists()
            if norm(p.get("name")) == norm(playlist_name)
        ),
        None,
    )
    playlist_id = (match or {}).get("id")
    songs = probes.navidrome.playlist_songs(playlist_id) if playlist_id else []
    playlist_titles = {norm(s.get("title")) for s in songs}

    where = "WHERE id > ?" if since_id is not None else ""
    params = (since_id,) if since_id is not None else ()
    rows = stack.db.query(
        f"SELECT id, source, artist, track, status, search_id, download_id, "
        f"playlist_id, created_at FROM recommendations {where} ORDER BY id",
        params,
    )

    by_status: dict[str, list[dict]] = {}
    for row in rows:
        by_status.setdefault(row["status"], []).append(row)

    in_library = by_status.get("in_library", [])
    downloaded = by_status.get("downloaded", [])
    queued = by_status.get("queued", [])

    def _missing(recs: list[dict]) -> list[dict]:
        return [r for r in recs if norm(r["track"]) not in playlist_titles]

    downloaded_missing = _missing(downloaded)
    indexed_but_missing = []
    for rec in downloaded_missing:
        song = probes.navidrome.find_song(rec["track"], strip_feat(rec["artist"]))
        if song:
            indexed_but_missing.append({**rec, "song_id": song.get("id")})

    return PlaylistAudit(
        playlist_name=playlist_name,
        playlist_id=playlist_id,
        playlist_titles=playlist_titles,
        in_library_recs=in_library,
        downloaded_recs=downloaded,
        queued_recs=queued,
        downloaded_missing=downloaded_missing,
        in_library_missing=_missing(in_library),
        downloaded_indexed_but_missing=indexed_but_missing,
    )


def grade_s12_playlist(
    probes,
    stack,
    scorecard,
    *,
    run_id: str,
    scenario: str,
    since_id: int | None = None,
) -> StageResult:
    """Grade the playlist against the ledger and record the gap.

    FAILs when a rec the system worked hardest to get — searched, queued,
    downloaded, imported, indexed — is not in the playlist the user opens.
    """
    audit = audit_playlist(probes, stack, since_id=since_id)

    problems: list[str] = []
    if audit.playlist_id is None:
        problems.append(f"playlist {audit.playlist_name!r} does not exist in Navidrome")
    if audit.in_library_missing:
        problems.append(
            f"{len(audit.in_library_missing)} of {len(audit.in_library_recs)} "
            f"in-library recs are not in the playlist"
        )
    if audit.downloaded_indexed_but_missing:
        problems.append(
            f"{len(audit.downloaded_indexed_but_missing)} downloaded recs are "
            f"indexed in Navidrome but absent from the playlist"
        )
    elif audit.downloaded_missing:
        problems.append(
            f"{len(audit.downloaded_missing)} downloaded recs are not in the "
            f"playlist (and are not indexed either — S11 territory)"
        )

    return scorecard.grade(
        Stage.S12_PLAYLIST_CORRECT,
        not problems,
        scenario=scenario,
        run_id=run_id,
        detail="; ".join(problems) if problems else "playlist matches the ledger",
        evidence={
            "playlist_name": audit.playlist_name,
            "playlist_id": audit.playlist_id,
            "playlist_song_count": len(audit.playlist_titles),
            "recs_decided": audit.decided,
            "recs_in_library": len(audit.in_library_recs),
            "recs_downloaded": len(audit.downloaded_recs),
            "recs_queued": len(audit.queued_recs),
            "gap": audit.gap,
            "downloaded_missing_sample": [
                f"{r['artist']} - {r['track']}" for r in audit.downloaded_missing[:10]
            ],
            "downloaded_indexed_but_missing_sample": [
                f"{r['artist']} - {r['track']} (song {r['song_id']})"
                for r in audit.downloaded_indexed_but_missing[:10]
            ],
        },
    )


# ---------------------------------------------------------------------------
# S11 — Navidrome indexes it
# ---------------------------------------------------------------------------


def test_s11_scan_reads_every_tree(stack, probes, scorecard, since_now):
    """A scan must read the whole music root and purge nothing that is there.

    This is the regression guard for the mount bug. It asserts nothing about
    *how* the trees are mounted — a future layout change is free — only that
    the scanner walks them without an error and does not purge live files.
    That is the precise shape of the failure: the scan "succeeded" (exit was
    clean, `getScanStatus` said done) while quietly skipping the root and
    deleting the library behind it.
    """
    run_id = new_run_id("s11-mounts")
    scenario = "scan_reads_every_tree"

    trees = music_trees(stack)
    on_disk = {name: len(tree.audio_files) for name, tree in trees.items()}
    before = probes.navidrome.song_count()

    scan_s = scan_and_time(probes)
    # Navidrome's own log is the only place the skip is visible; the API
    # reports a clean scan either way.
    logs = stack.docker.logs("navidrome", since=since_now())
    alarms, purged = scan_alarms(logs)
    after = probes.navidrome.song_count()

    total_on_disk = sum(on_disk.values())
    problems: list[str] = []
    if alarms:
        problems.append(f"{len(alarms)} scanner read errors: {alarms[0]}")
    if purged and after < before:
        problems.append(
            f"scan purged {purged} media files and the library shrank "
            f"{before} -> {after}"
        )
    if total_on_disk and after == 0:
        problems.append(
            f"{total_on_disk} audio files across {list(on_disk)} but Navidrome "
            f"indexed 0 — the trees are not reaching the scanner"
        )

    result = scorecard.grade(
        Stage.S11_NAVIDROME_INDEXED,
        not problems,
        scenario=scenario,
        run_id=run_id,
        latency_s=round(scan_s, 2),
        detail="; ".join(problems) if problems else "scan read every tree cleanly",
        evidence={
            "audio_files_on_disk": on_disk,
            "song_count_before": before,
            "song_count_after": after,
            "purged_media_files": purged,
            "scan_s": round(scan_s, 2),
            "alarms": alarms[:10],
            "scan_schedule": _env("ND_SCANSCHEDULE", "@every 10m (compose default)"),
        },
    )
    if result.verdict is Verdict.FAIL:
        scorecard.skip_from(
            Stage.S12_PLAYLIST_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why="Navidrome cannot read the music tree; nothing downstream is measurable",
        )
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("tree_name", ["Discovery", "Searches"])
def test_s11_every_file_in_the_tree_is_indexed(
    stack, probes, scorecard, tree_name: str
):
    """Every audio file in a tree is findable in Navidrome after a scan.

    Parametrised per tree deliberately: the mount bug broke Discovery while
    Searches looked fine, and a fix verified on one tree only is a fix that
    will bite the moment recs start landing. Discovery is where recs go,
    Searches is where manual downloads go — the user reaches both.

    Latency is reported two ways. `scan_s + find_s` is what the user gets when
    something forces a scan (musica's DownloadMonitor does, after a move).
    `stale_s` is how long the file has been sitting on disk, which is only a
    lower bound on the unattended latency — `ND_SCANSCHEDULE=@every 10m` is the
    ceiling, and the runner measures the real number by timing an import.
    """
    run_id = new_run_id(f"s11-{tree_name.lower()}")
    scenario = f"tree_indexed:{tree_name}"
    tree = music_trees(stack)[tree_name]
    files = tree.audio_files

    if not files:
        # Not a pass. Nothing landed in this tree, so S11 was never exercised
        # here — recording it as SKIP is what keeps the funnel honest.
        scorecard.record(
            StageResult(
                stage=Stage.S11_NAVIDROME_INDEXED,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail=(
                    f"no audio files under {tree.root} — nothing to index. The "
                    f"download wave must re-run this after an import lands here."
                ),
                evidence={"tree_root": str(tree.root), "exists": tree.root.exists()},
            )
        )
        pytest.skip(f"{tree_name} tree is empty at {tree.root}")

    scan_s = scan_and_time(probes)

    misses: list[str] = []
    per_file: list[dict] = []
    for path in files:
        tags = probes.tags.read(path)
        title = tags.title or path.stem
        artist = tags.albumartist or tags.artist or ""
        song, find_s = wait_until_findable(probes, title, artist, timeout=30.0)
        stale_s = round(time.time() - path.stat().st_mtime, 1)
        per_file.append(
            {
                "path": str(path),
                "title": title,
                "artist": artist,
                "found": song is not None,
                "find_s": round(find_s, 2),
                "stale_s": stale_s,
                "song_id": (song or {}).get("id"),
            }
        )
        if song is None:
            misses.append(f"{artist} - {title} ({path})")

    result = scorecard.grade(
        Stage.S11_NAVIDROME_INDEXED,
        not misses,
        scenario=scenario,
        run_id=run_id,
        latency_s=round(scan_s + sum(f["find_s"] for f in per_file), 2),
        detail=(
            f"all {len(files)} files in {tree_name} are indexed"
            if not misses
            else f"{len(misses)}/{len(files)} files in {tree_name} are on disk but "
            f"not in Navidrome: {misses[0]}"
        ),
        evidence={
            "tree": tree_name,
            "tree_root": str(tree.root),
            "files": len(files),
            "scan_s": round(scan_s, 2),
            "per_file": per_file[:50],
            "misses": misses[:20],
        },
    )
    if result.verdict is Verdict.FAIL:
        scorecard.skip_from(
            Stage.S12_PLAYLIST_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why=f"{len(misses)} files in {tree_name} never reached Navidrome",
        )
    assert not misses, f"{tree_name}: not indexed -> {misses}"


# ---------------------------------------------------------------------------
# S12 — playlist correct
# ---------------------------------------------------------------------------


def test_s12_playlist_holds_what_the_pull_decided(stack, probes, scorecard):
    """The headline S12 measurement: ledger decided N, playlist holds M.

    Grades the *whole* recommendation history on the stack, because that is
    what the user's playlist is supposed to be the accumulation of. The run
    wave calls `grade_s12_playlist(..., since_id=...)` to scope the same
    comparison to a single pull.
    """
    run_id = new_run_id("s12-ledger")
    result = grade_s12_playlist(
        probes, stack, scorecard, run_id=run_id, scenario="playlist_vs_ledger"
    )
    if result.verdict is Verdict.FAIL:
        scorecard.skip_from(
            Stage.S13_USER_CAN_FIND,
            scenario="playlist_vs_ledger",
            run_id=run_id,
            why="playlist is wrong; the user cannot reach these tracks by playlist",
        )
    assert result.verdict is Verdict.PASS, result.detail


def test_s12_downloaded_recs_are_linked_to_a_playlist(stack, scorecard):
    """A rec that downloaded should end up in the playlist. Does it, ever?

    This asks the question of the ledger rather than of Navidrome, so it
    isolates *musica's own belief*: `recommendations.playlist_id` is set when
    a rec is added to the playlist. `in_library` rows get one. If no
    `downloaded` row ever has one, then by musica's own bookkeeping no
    downloaded rec has ever been added — independent of whether Navidrome
    would have accepted it.

    `RecPuller._run_pull` adds to the playlist in step 7 only, from
    `classification.in_library`, computed *before* anything is queued. The
    completion hook in `DownloadMonitor` (`mark_rec_downloaded`) sets the
    status and triggers a library scan, and stops there.
    """
    run_id = new_run_id("s12-linkage")
    scenario = "downloaded_recs_playlist_linkage"

    rows = stack.db.query(
        "SELECT status, COUNT(*) AS n, "
        "SUM(CASE WHEN playlist_id IS NOT NULL AND playlist_id != '' THEN 1 ELSE 0 END) "
        "AS with_playlist FROM recommendations GROUP BY status"
    )
    by_status = {r["status"]: r for r in rows}
    downloaded = by_status.get("downloaded", {"n": 0, "with_playlist": 0})
    in_library = by_status.get("in_library", {"n": 0, "with_playlist": 0})

    if not downloaded["n"]:
        scorecard.record(
            StageResult(
                stage=Stage.S12_PLAYLIST_CORRECT,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail="no rec has ever reached status 'downloaded' on this stack",
                evidence={"status_counts": {r["status"]: r["n"] for r in rows}},
            )
        )
        pytest.skip("no downloaded recs to grade")

    linked = downloaded["with_playlist"] or 0
    ok = linked > 0
    scorecard.grade(
        Stage.S12_PLAYLIST_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        detail=(
            f"{linked}/{downloaded['n']} downloaded recs carry a playlist_id"
            if ok
            else f"0 of {downloaded['n']} downloaded recs have ever been linked to a "
            f"playlist, while {in_library['with_playlist']}/{in_library['n']} "
            f"in-library recs have. A rec that had to be downloaded never "
            f"reaches the playlist."
        ),
        evidence={
            "downloaded_total": downloaded["n"],
            "downloaded_with_playlist_id": linked,
            "in_library_total": in_library["n"],
            "in_library_with_playlist_id": in_library["with_playlist"],
            "status_counts": {r["status"]: r["n"] for r in rows},
            "code_path": (
                "app/workers/rec_puller.py:556-618 is the only add_to_playlist "
                "call site; app/workers/download_monitor.py:210-219 is the rec "
                "completion hook and does not touch playlists"
            ),
        },
    )
    assert ok, (
        f"0 of {downloaded['n']} downloaded recs were ever added to a playlist "
        f"(in-library recs: {in_library['with_playlist']}/{in_library['n']})"
    )


def test_s12_all_three_categories_share_one_playlist(stack, probes, scorecard):
    """Record where each category lands. Today: all three, one playlist.

    Not a bug report — a measurement of a known design gap (P6.7-1, "three
    independent playlists"). It fails only if the config grows per-category
    names and the ledger still shows them merged, which is the regression that
    would silently undo the split.
    """
    run_id = new_run_id("s12-categories")
    scenario = "category_playlist_split"

    recs_cfg = stack.client.get_config()["recs"]
    name_keys = [k for k in recs_cfg if k.endswith("playlist_name")]
    per_category_names = {k: recs_cfg[k] for k in name_keys}
    single_name = recs_cfg.get("playlist_name")  # gone since P6.7-1

    rows = stack.db.query(
        "SELECT source, playlist_id, COUNT(*) AS n FROM recommendations "
        "WHERE playlist_id IS NOT NULL AND playlist_id != '' "
        "GROUP BY source, playlist_id"
    )
    ids_per_source: dict[str, set[str]] = {}
    for row in rows:
        ids_per_source.setdefault(row["source"], set()).add(row["playlist_id"])
    all_ids = {i for ids in ids_per_source.values() for i in ids}

    split_configured = len(name_keys) > 1
    merged_in_practice = len(all_ids) <= 1

    ok = not (split_configured and merged_in_practice)
    scorecard.grade(
        Stage.S12_PLAYLIST_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        detail=(
            f"one playlist name ({single_name!r}) is configured for all three "
            f"categories — Comfort Zone, Fresh Picks and Deep Cuts all land in "
            f"the same playlist (P6.7-1 not built)"
            if not split_configured
            else f"per-category names are configured {per_category_names} but the "
            f"ledger still shows a single playlist id {all_ids}"
        ),
        evidence={
            "configured_playlist_names": per_category_names,
            "playlist_ids_per_source": {
                k: sorted(v) for k, v in ids_per_source.items()
            },
            "distinct_playlist_ids": sorted(all_ids),
            "categories_enabled": {
                k: v for k, v in recs_cfg.items() if k.endswith("_enabled")
            },
        },
    )
    assert ok, "per-category playlists are configured but everything lands in one"


def test_s12_navidrome_accepts_playlist_writes(probes, scorecard, user_search):
    """Can anything add a song to a playlist here at all?

    Without this, "the playlist is empty" has two indistinguishable causes:
    musica never tried, or Navidrome refused. This creates a test-prefixed
    playlist, adds a real song id, reads it back and deletes it. If it passes,
    every empty-playlist finding above is musica's, not Navidrome's.
    """
    run_id = new_run_id("s12-write")
    scenario = "navidrome_playlist_write"
    name = f"{TEST_PLAYLIST_PREFIX}-write-{int(time.time())}"

    songs = user_search.all_songs(limit=5)
    if not songs:
        scorecard.record(
            StageResult(
                stage=Stage.S12_PLAYLIST_CORRECT,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail="Navidrome has no songs, so a playlist write cannot be tested",
            )
        )
        pytest.skip("empty Navidrome library")

    playlist_id = probes.navidrome.create_playlist(name)
    try:
        added = user_search.add_to_playlist(playlist_id, [songs[0]["id"]])
        contents = probes.navidrome.playlist_songs(playlist_id)
        ok = bool(added) and any(s.get("id") == songs[0]["id"] for s in contents)
        scorecard.grade(
            Stage.S12_PLAYLIST_CORRECT,
            ok,
            scenario=scenario,
            run_id=run_id,
            detail=(
                "Navidrome accepts playlist creation and song adds"
                if ok
                else "Navidrome refused a playlist write — every empty-playlist "
                "finding needs re-reading in that light"
            ),
            evidence={
                "playlist_name": name,
                "playlist_id": playlist_id,
                "song_id": songs[0]["id"],
                "read_back": [s.get("id") for s in contents],
            },
        )
        assert ok, "Navidrome refused a playlist write"
    finally:
        probes.navidrome.delete_playlist(playlist_id)


def test_s12_u9_playlist_is_not_recreated_eagerly(stack, probes, scorecard):
    """U9: delete the playlist — musica must not recreate it until it has
    tracks to put in it.

    The user's stated behaviour: a deleted playlist stays deleted until the
    next pull actually has something to add. An eager recreate (an empty
    playlist reappearing on a timer, or on a pull that found nothing) is the
    failure this catches.

    The playlist's contents are snapshotted and restored afterwards, so running
    this never costs the user their Recs playlist. The other half of U9 — "it
    *is* recreated when the next pull has tracks" — needs a pull, which queues
    downloads, so the run wave owns it: call `grade_s12_playlist` after its
    pull and check `playlist_id is not None`.
    """
    run_id = new_run_id("s12-u9")
    scenario = "u9_playlist_lifecycle"

    recs_cfg = stack.client.get_config()["recs"]
    # P6.7-1: per-category names. Prefer an enabled category so the test
    # exercises a playlist musica actually maintains; fall back to the
    # first name so the lifecycle rule is still auditable.
    enabled = [
        cat
        for cat in ("comfort_zone", "fresh_picks", "deep_cuts")
        if recs_cfg.get(f"{cat}_enabled")
    ]
    category = enabled[0] if enabled else "comfort_zone"
    playlist_name = recs_cfg[f"{category}_playlist_name"]
    match = next(
        (
            p
            for p in probes.navidrome.list_playlists()
            if norm(p.get("name")) == norm(playlist_name)
        ),
        None,
    )
    if match is None:
        scorecard.record(
            StageResult(
                stage=Stage.S12_PLAYLIST_CORRECT,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail=(
                    f"playlist {playlist_name!r} does not exist, so the delete "
                    f"half of U9 cannot run. That it is absent is itself worth "
                    f"reading: musica only ever creates it during a pull that "
                    f"found in-library recs."
                ),
            )
        )
        pytest.skip(f"no {playlist_name!r} playlist to delete")

    original_id = match["id"]
    original_songs = [s.get("id") for s in probes.navidrome.playlist_songs(original_id)]

    probes.navidrome.delete_playlist(original_id)
    # Long enough for the DownloadMonitor (check_interval 15s) and any scan
    # hook to fire. The RecPuller's own timer is days away, so this only ever
    # catches an *eager* recreate — which is exactly the failure mode.
    time.sleep(45)

    recreated = next(
        (
            p
            for p in probes.navidrome.list_playlists()
            if norm(p.get("name")) == norm(playlist_name)
        ),
        None,
    )
    eager = recreated is not None and not probes.navidrome.playlist_songs(
        recreated["id"]
    )

    try:
        scorecard.grade(
            Stage.S12_PLAYLIST_CORRECT,
            not eager,
            scenario=scenario,
            run_id=run_id,
            detail=(
                f"an empty {playlist_name!r} reappeared within 45s of deletion — "
                f"musica recreates it eagerly, not when it has tracks to add"
                if eager
                else f"{playlist_name!r} stayed deleted, as the user wants"
            ),
            evidence={
                "playlist_name": playlist_name,
                "deleted_id": original_id,
                "songs_before_delete": len(original_songs),
                "recreated_id": (recreated or {}).get("id"),
                "waited_s": 45,
                "unverified": (
                    "the other half of U9 — recreated on the next pull that has "
                    "tracks — needs a pull and therefore the download wave"
                ),
            },
        )
        assert not eager, f"{playlist_name!r} was recreated empty"
    finally:
        # Restore what the user had. A new id is unavoidable (Navidrome mints
        # one per playlist) and musica matches by name, so nothing breaks.
        if recreated is None:
            restored = probes.navidrome.create_playlist(playlist_name)
            if original_songs:
                UserSearch().add_to_playlist(restored, original_songs)


# ---------------------------------------------------------------------------
# S13 — user can find it
# ---------------------------------------------------------------------------


def test_s13_every_indexed_track_is_findable(stack, probes, scorecard, user_search):
    """Every file in the music trees can be found the way a user searches.

    "Findable" means two things and both are graded: typing the artist returns
    it, and typing the title returns it. The first is the one that matters —
    a track you cannot reach from its own artist's name is effectively lost,
    however clean its tags are.
    """
    run_id = new_run_id("s13-findable")
    scenario = "user_search"

    files = [p for tree in music_trees(stack).values() for p in tree.audio_files]
    if not files:
        scorecard.record(
            StageResult(
                stage=Stage.S13_USER_CAN_FIND,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail="no audio files in the music trees — nothing to search for",
            )
        )
        pytest.skip("no files in the music trees")

    scan_and_time(probes)
    failures: list[str] = []
    for path in files:
        tags = probes.tags.read(path)
        track = corpus_track_for(tags.title, tags.albumartist or tags.artist)
        result = grade_s13_findable(
            probes,
            scorecard,
            user_search,
            path=path,
            run_id=run_id,
            scenario=scenario,
            track=track,
        )
        if result.verdict is not Verdict.PASS:
            failures.append(f"{path.name}: {result.detail}")

    assert not failures, "\n".join(failures)


def test_s13_corpus_tracks_on_disk_are_reachable(stack, probes, scorecard, user_search):
    """The corpus tracks that made it to disk must be reachable by name.

    Narrower and louder than the sweep above: these are the tracks the whole
    suite is built around, so a miss here is graded against the tier weight and
    ranks at the top of the report.
    """
    run_id = new_run_id("s13-corpus")
    scenario = "user_search:corpus"

    files = [p for tree in music_trees(stack).values() for p in tree.audio_files]
    on_disk: list[tuple[Track, Path]] = []
    for path in files:
        tags = probes.tags.read(path)
        track = corpus_track_for(tags.title, tags.albumartist or tags.artist)
        if track:
            on_disk.append((track, path))

    if not on_disk:
        scorecard.record(
            StageResult(
                stage=Stage.S13_USER_CAN_FIND,
                verdict=Verdict.SKIP,
                scenario=scenario,
                run_id=run_id,
                detail=(
                    "no corpus track is on disk yet — the download wave has to "
                    "run before this stage means anything"
                ),
                evidence={"files_on_disk": len(files)},
            )
        )
        pytest.skip("no corpus tracks on disk")

    failures: list[str] = []
    for track, path in on_disk:
        result = grade_s13_findable(
            probes,
            scorecard,
            user_search,
            path=path,
            run_id=run_id,
            scenario=scenario,
            track=track,
        )
        if result.verdict is not Verdict.PASS:
            failures.append(f"[{track.tier.value}] {track.artist} - {track.title}: {result.detail}")

    assert not failures, "\n".join(failures)
