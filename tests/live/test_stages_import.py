"""
Stages S7-S10: what happens to a file between "slskd finished downloading it"
and "it is sitting in the right place, with the right tags, exactly once".

This is the part of the pipeline the user described as a roulette spin. The
unit suite is green and the disk is wrong, so nothing here reads musica's own
opinion of what it did:

- **S7** grades the import against the `downloads` row, the beets library row
  and the file on disk — three independent witnesses that must agree.
- **S8** reads tags with mutagen through `probes.tags`, never beets' library.
- **S9** grades placement **strict canonical** (explicit user decision): one
  folder per artist, no featuring clause in albumartist, no case- or
  punctuation-variant artist folders, no strays, no partials, no empty dirs,
  nothing left behind under `downloads/complete`. This is **expected to fail
  today** — measuring that is the point — so every failure message names the
  specific defect rather than "the audit was not clean".
- **S10** is the dedup block, including the regression test for the defect
  that stranded five files on the live stack.

Ground truth this file was written against (verified 2026-08-12, before any
fix landed):

    beets' library DBs claimed 15 (searches) + 35 (discovery) items while the
    disk held 0 and 1. Those stale rows make `duplicate_action: skip` and
    `BeetsService._find_cross_profile_duplicate` falsely skip *new* downloads;
    `DownloadMonitor._import_via_beets` then calls
    `mark_file_moved(transfer_id, "")` and the file is stranded forever under
    `downloads/complete/soulseek/`. Five files were sitting there, each with a
    `downloads` row reading `file_moved=1, target_dir=''`.

    beets also runs in singleton mode and promotes featuring artists to
    albumartist, so "Tyler, The Creator", "Tyler, the Creator" and
    "Tyler, The Creator ft. Rex Orange County" are three folders for one
    artist.

What this file needs from the runner
------------------------------------
**Nothing is downloaded here.** Downloads are the scarce resource in this
suite, so S7-S10 grade what the S1-S6 wave already put on disk, and S10
manufactures its duplicate/stale-row states by re-importing *copies* of a file
that is already in the tree.

1. Run this file **after** the S1-S6 stage tests in the same session, in the
   same process order (`pytest tests/live/ --live` in file order, or an
   explicit runner ordering). S7-S9 read the `downloads`/`searches` tables to
   find what landed; they do not care *how* it landed.
2. Optionally export `MUSICA_LIVE_RUN_ID` so every agent's StageResults share
   one run id. Without it this file mints its own.
3. Optionally have the download wave append one JSON object per corpus track
   to `<artifact_root>/download_ledger.jsonl`:
   `{"title": ..., "artist": ..., "transfer_id": ..., "is_rec": bool}`.
   When present it is authoritative. When absent the ledger is reconstructed
   from `musica.db` by matching `searches.query`/`searches.artist` back to the
   corpus, then following `downloads.search_id` — which is why the S1-S3 tests
   should issue each corpus search with the corpus `title`/`artist` verbatim.
4. **Do not run this file under `pytest-xdist`.** The per-tier summary tests
   accumulate in module-level state that only survives within one process.
5. Do **not** request the `full_reset` fixture for this file — it would erase
   the very tree S7-S9 are grading. S10 does its own scoped cleanup.

Latency, honestly
-----------------
`latency_s` on every StageResult here is the time *this grading* took, not the
time the pipeline took: S7-S10 are post-hoc audits and the pipeline's own
import latency is not recorded anywhere queryable. Where a lag *is* derivable
(`downloads.completed_at` vs. the mtime of the file beets produced) it is
recorded as `evidence["import_lag_s"]` and labelled as an estimate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.live.corpus import Track, tracks_in_run_order
from tests.live.harness import REPO_ROOT
from tests.live.probes.contract import Stage, StageResult, TreeAudit, Verdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Where the app sees the music tree from inside its container.
CONTAINER_MUSIC_ROOT = PurePosixPath("/music")

#: Profiles BeetsService manages, mirroring `app.services.beets._PROFILES`.
PROFILES = ("searches", "discovery")

#: Tree each profile writes into, relative to the music root. These mirror
#: `config.toml`'s `paths.searches_dir` / `paths.discovery_dir`; the live
#: values are read from config at runtime and cross-checked against these.
PROFILE_TREE = {"searches": "Searches", "discovery": "Discovery"}

#: slskd's completed-download tree, relative to `paths.download_path`.
COMPLETE_SUBDIR = PurePosixPath("complete/soulseek")

#: Synthetic slskd "peer" S10 stages its copies under. Named so a leftover is
#: unmistakably ours if a run dies mid-test.
STAGING_PEER = "musica-live-stage"

#: Audio extensions the pipeline accepts (mirrors
#: `app.db.download_store.ALLOWED_EXTENSIONS`).
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

#: Sentinel the in-container drivers prefix their JSON result with, so beets'
#: own chatter on stdout can never be mistaken for the result.
_JSON_SENTINEL = "@@MUSICA_LIVE_JSON@@"


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested in tests/test_live_import_helpers.py
#
# These carry the assertions. If one of them silently returns "everything is
# fine" the live run reports success while checking nothing, which is exactly
# the failure mode this whole suite exists to stop.
# ---------------------------------------------------------------------------

# beets' default `replace` rules, in beets' own order (config_default.yaml in
# beets 2.x). Reproduced rather than imported because beets runs *inside the
# container* and the tests run on the host. A drift here shows up as a S9
# "wrong folder name" failure against a folder beets named correctly, so the
# table has its own unit tests.
_BEETS_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\\/]"), "_"),
    (re.compile(r"^\."), "_"),
    (re.compile(r"[\x00-\x1f]"), ""),
    (re.compile(r'[<>:"\?\*\|]'), "_"),
    (re.compile(r"\.$"), "_"),
    (re.compile(r"\s+$"), ""),
    (re.compile(r"^\s+"), ""),
    (re.compile(r"^-"), "_"),
)

# Only unambiguous featuring markers. "&", "with" and "x" are *not* included:
# "Kendrick Lamar & SZA" and "Earth, Wind & Fire" are credited collaborations,
# not featuring clauses, and folding them would manufacture defects that
# aren't there.
_FEAT_RE = re.compile(
    r"""
    \s*                       # leading space
    [\(\[\{]?                 # optional opening bracket
    \s*
    \b(?:feat|feats|ft|featuring)\b\.?   # the marker
    \s
    .*$                       # everything after it
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bracketed noise peers weld into titles: "(feat. X)", "[Explicit]",
# "(Official Video)", "(Remastered 2011)".
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def beets_sanitize(name: str) -> str:
    """Apply beets' default path-component `replace` rules to one component.

    This is how "jev." becomes the folder `jev_` and "MUTT Deluxe: HEEL"
    becomes `MUTT Deluxe_ HEEL`. Without it, strict placement grading fails
    corpus entries for names beets sanitised *correctly*.
    """
    out = name
    for pattern, repl in _BEETS_REPLACEMENTS:
        out = pattern.sub(repl, out)
    return out


def has_feat_clause(name: str | None) -> bool:
    """True when a featuring clause is welded into an artist credit.

    An albumartist that answers True here is a S8/S9 defect by itself: it is
    what splits one artist across several folders.
    """
    return bool(name) and _FEAT_RE.search(name or "") is not None


def strip_feat_clause(name: str | None) -> str:
    """Drop a trailing featuring clause from an artist credit."""
    if not name:
        return ""
    return _FEAT_RE.sub("", name).strip().rstrip(",;-").strip()


def _fold(text: str) -> str:
    """Accent-fold, casefold and strip everything that is not alphanumeric."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]+", "", stripped.casefold())


def artist_variant_key(name: str | None) -> str:
    """The key two artist folders must share to be *the same artist*.

    "Tyler, The Creator", "Tyler, the Creator" and
    "Tyler, The Creator ft. Rex Orange County" all fold to `tylerthecreator`;
    "jev." and the sanitised folder `jev_` both fold to `jev`. Case variants,
    punctuation variants and feat.-clause variants are the three ways the live
    tree fragmented an artist, and this collapses all three.
    """
    return _fold(strip_feat_clause(name))


def title_key(title: str | None) -> str:
    """Fold a title for comparison, dropping bracketed clauses.

    "Write This Down (feat. Nieve)" and "Write This Down" compare equal;
    "ALICE_" and "ALICE." compare equal. Peers weld "[Explicit]" and
    "(Official Video)" into filenames and tags, and grading a title as wrong
    for that would bury the failures that matter.
    """
    if not title:
        return ""
    return _fold(_BRACKETED_RE.sub(" ", title))


def album_key(album: str | None) -> str:
    """Fold an album title for comparison. Same rules as `title_key` minus
    the bracket stripping — "Cherry Bomb + Instrumentals" and "Cherry Bomb"
    really are different releases."""
    return _fold(album or "")


def canonical_artist_dir(albumartist: str) -> str:
    """The one folder name an artist is allowed to have under strict
    canonical placement: feat. clause removed, then beets-sanitised."""
    return beets_sanitize(strip_feat_clause(albumartist))


def is_mbid(value: str | None) -> bool:
    """True for a well-formed MusicBrainz id. `asis` imports have none, which
    is exactly what distinguishes them from matched ones."""
    return bool(value) and _UUID_RE.match(value or "") is not None


def parse_env(text: str) -> dict[str, str]:
    """Minimal `.env` parser — enough for `MUSIC_HOST_DIR`.

    Deliberately not `python-dotenv`: the harness already reads `.env` this
    way for `SLSKD_API_KEY`, and one more dependency in the test path is one
    more thing that can differ between the runner's machine and the user's.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def container_to_host(
    container_path: str | Path,
    music_host_root: Path,
    container_root: PurePosixPath = CONTAINER_MUSIC_ROOT,
) -> Path | None:
    """Translate a path the app recorded (e.g. `downloads.target_dir`) into a
    host path. Returns None for a path that is not under the music root — a
    `target_dir` outside `/music` is itself a finding, not something to
    silently rebase."""
    if not container_path:
        return None
    posix = PurePosixPath(str(container_path).replace("\\", "/"))
    try:
        relative = posix.relative_to(container_root)
    except ValueError:
        return None
    return music_host_root / relative


def slskd_source_path(download_root: Path, username: str, filename: str) -> Path:
    """Where `DownloadMonitor._resolve_source_path` looks for a completed
    transfer's file. slskd reports Windows-style separators; the monitor
    normalises them, so this must too or every strand check misses."""
    normalised = filename.replace("\\", "/")
    return download_root / COMPLETE_SUBDIR / username / normalised


def same_path(a: Path | None, b: Path | None) -> bool:
    """Case-insensitive path equality.

    The host filesystem is macOS (case-insensitive): config says `Searches`
    and `Discovery`, the directories on disk are `searches` and `discovery`,
    and both resolve. A case-sensitive comparison here would report a defect
    that does not exist.
    """
    if a is None or b is None:
        return False
    return str(a).casefold().rstrip("/") == str(b).casefold().rstrip("/")


def is_under(child: Path | None, parent: Path) -> bool:
    """Case-insensitive `child is inside parent`."""
    if child is None:
        return False
    c = str(child).casefold().rstrip("/")
    p = str(parent).casefold().rstrip("/")
    return c == p or c.startswith(p + "/")


def relative_to_root(path: Path, root: Path) -> str:
    """`path` expressed relative to `root`, case-insensitively.

    `Path.relative_to` is case-sensitive and the host tree spells `Searches`
    as `searches`, so it raises on paths that are genuinely inside the root.
    """
    if not is_under(path, root):
        raise ValueError(f"{path} is not under {root}")
    return str(path)[len(str(root)) :].lstrip("/")


def host_to_container(
    path: Path,
    music_host_root: Path,
    container_root: PurePosixPath = CONTAINER_MUSIC_ROOT,
) -> PurePosixPath:
    """Inverse of `container_to_host` — for handing a host path to a driver
    running inside the container."""
    return container_root / relative_to_root(path, music_host_root)


# ---------------------------------------------------------------------------
# Download ledger — "which rows belong to which corpus track"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadRecord:
    """One `downloads` row, joined to the `searches` row that produced it."""

    id: str
    username: str
    filename: str
    state: str
    is_rec: bool
    file_moved: bool
    target_dir: str
    search_id: str | None
    import_unmatched: bool
    created_at: int | None
    completed_at: int | None
    query: str | None = None
    search_artist: str | None = None

    @property
    def basename(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def profile(self) -> str:
        return "discovery" if self.is_rec else "searches"


def _row_to_record(row: dict, search: dict | None) -> DownloadRecord:
    return DownloadRecord(
        id=row["id"],
        username=row["username"],
        filename=row["filename"],
        state=row["state"],
        is_rec=bool(row.get("is_rec_download")),
        file_moved=bool(row.get("file_moved")),
        target_dir=row.get("target_dir") or "",
        search_id=row.get("search_id"),
        import_unmatched=bool(row.get("import_unmatched")),
        created_at=row.get("created_at"),
        completed_at=row.get("completed_at"),
        query=(search or {}).get("query"),
        search_artist=(search or {}).get("artist"),
    )


def _significant_tokens(text: str) -> set[str]:
    """Alphanumeric tokens of 2+ characters, folded. Used for the fuzzy
    filename fallback only — never for grading."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return {t for t in re.split(r"[^0-9a-z]+", folded) if len(t) >= 2}


def record_matches_track(record: DownloadRecord, track: Track) -> str | None:
    """How (if at all) this download belongs to this corpus track.

    Returns the match strategy — `"search"` (the search row carries the corpus
    title and artist verbatim, which is what the S1-S3 tests issue) or
    `"filename"` (every significant token of the title, plus at least one
    artist token, appears in the downloaded filename) — or None.

    `"filename"` is a fallback, not a preference: a peer that names a file
    badly enough will not match, and that is better than confidently grading
    the wrong file.
    """
    q_key = title_key(record.query)
    if q_key and q_key == title_key(track.title):
        a_key = artist_variant_key(record.search_artist)
        if not a_key or a_key == artist_variant_key(track.artist):
            return "search"
        if a_key == artist_variant_key(track.expect_albumartist):
            return "search"

    haystack = _significant_tokens(record.basename)
    title_tokens = _significant_tokens(track.title)
    artist_tokens = _significant_tokens(track.expect_albumartist)
    if title_tokens and title_tokens <= haystack and artist_tokens & haystack:
        return "filename"
    return None


def build_ledger(
    downloads: list[dict],
    searches: list[dict],
    tracks: list[Track],
) -> dict[str, list[DownloadRecord]]:
    """Map each corpus track to the `downloads` rows it produced.

    Keyed by `f"{artist} - {title}"`, ordered oldest-first so "the same track
    downloaded twice" is visible as a list of length 2 rather than collapsed.
    """
    by_id = {s["id"]: s for s in searches}
    records = [_row_to_record(r, by_id.get(r.get("search_id"))) for r in downloads]
    records.sort(key=lambda r: (r.created_at or 0))

    ledger: dict[str, list[DownloadRecord]] = {track_key(t): [] for t in tracks}
    for record in records:
        for track in tracks:
            if record_matches_track(record, track):
                ledger[track_key(track)].append(record)
                break
    return ledger


def track_key(track: Track) -> str:
    return f"{track.artist} - {track.title}"


def track_id(track: Track) -> str:
    """pytest parameter id — tier first so the run order is readable."""
    return f"{track.tier.value}-{track.artist} - {track.title}"


# ---------------------------------------------------------------------------
# Defect descriptions — a failure message that names the thing that is wrong
# ---------------------------------------------------------------------------


def stranding_defect(record: DownloadRecord, source_exists: bool) -> str | None:
    """The exact signature of the bug that stranded five files on the live
    stack, stated in words the user can act on.

    `file_moved=1` with an empty `target_dir` is musica recording "handled"
    for a file it never moved. Combined with the source still sitting under
    `downloads/complete`, that file is stranded forever: the monitor will
    never retry it and nothing else ever looks at it.
    """
    if not record.file_moved:
        return None
    if record.target_dir.strip():
        return None
    where = (
        "and the downloaded file is STILL under downloads/complete"
        if source_exists
        else "and the downloaded file is gone from downloads/complete too "
        "(so it was neither imported nor kept)"
    )
    return (
        f"downloads row {record.id} claims file_moved=1 with an EMPTY "
        f"target_dir {where}. This is the false-skip strand: beets refused "
        f"the import (stale library row / duplicate_action=skip) and "
        f"DownloadMonitor._import_via_beets recorded it as handled anyway."
    )


def describe_audit_defects(audit: TreeAudit, limit: int = 8) -> str:
    """Turn a `TreeAudit` into a message that names the specific defects.

    "audit not clean" is useless — the whole complaint being measured here is
    that directory management is unintelligible, so a failure has to say
    *which folders* fragmented and *which files* are stranded.
    """
    parts: list[str] = []

    if audit.artist_folder_variants:
        for canonical, folders in sorted(audit.artist_folder_variants.items()):
            parts.append(
                f"artist '{canonical}' is split across {len(folders)} folders: "
                + ", ".join(repr(f) for f in sorted(folders))
            )

    def _listing(label: str, paths: list[Path], why: str) -> None:
        if not paths:
            return
        shown = ", ".join(str(p) for p in paths[:limit])
        more = f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
        parts.append(f"{len(paths)} {label} ({why}): {shown}{more}")

    _listing(
        "stranded download(s)",
        audit.stranded_downloads,
        "still under downloads/complete, never consumed by an import",
    )
    _listing("partial file(s)", audit.partial_files, "incomplete transfer remnants")
    _listing("stray file(s)", audit.stray_files, "not audio and not expected")
    _listing("empty director(y/ies)", audit.empty_dirs, "nothing inside")

    if not parts:
        return "no defects"
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Tag grading (S8)
# ---------------------------------------------------------------------------

#: Fields S8 grades. `mb_trackid` is graded as a **core** field on purpose:
#: an `asis` fallback import writes no MusicBrainz data at all, and the user
#: asked for MBID to be graded. That makes S8 fail for every unmatched import,
#: which is intended — the per-field breakdown in `evidence` is what keeps the
#: statistic readable ("tags were right except nothing matched MusicBrainz").
TAG_FIELDS = ("albumartist", "artist", "album", "title", "track", "mb_trackid")


@dataclass
class TagGrade:
    ok: bool
    per_field: dict[str, bool]
    reasons: list[str]
    not_applicable: list[str] = field(default_factory=list)

    @property
    def graded_fields(self) -> int:
        return len(self.per_field)

    @property
    def fields_ok(self) -> int:
        return sum(1 for v in self.per_field.values() if v)

    def summary(self) -> str:
        if self.ok:
            return f"all {self.graded_fields} graded tag fields match"
        return "; ".join(self.reasons)


def grade_tags(tags: Any, track: Track) -> TagGrade:
    """S8: do the tags actually written to this file match what the user asked
    for?

    `tags` is a `tests.live.probes.contract.TrackTags` (mutagen's reading of
    the file, not beets' library row). Graded independently of
    `TagProbe.grade` so the verdict does not depend on another component's
    grading policy — `TagProbe.grade` is still called and recorded as a
    cross-check.

    Grading rules, and why:

    - **albumartist** must fold to the corpus `expect_albumartist` *and* must
      not contain a featuring clause. The fold alone is not enough: it strips
      feat. clauses by design, so "Tyler, The Creator ft. Rex Orange County"
      would pass. The literal check is what catches the folder-fragmenting
      defect.
    - **artist** may legitimately carry the featuring credit
      ("Alesso feat. Tove Lo"), so it passes when the canonical album artist
      is the leading credit.
    - **album** is graded only when the corpus knows one (`expect_album`).
    - **title** is folded with bracketed clauses removed, so a peer's
      "[Explicit]" is not a failure.
    - **track** is graded as "present and >= 1" — the corpus does not carry
      canonical track numbers, so a wrong-but-plausible number is not
      detectable here. Not graded for singles (`expect_album is None`).
    - **mb_trackid** must be a well-formed MBID: no MBID means the import fell
      back to `asis` and wrote no MusicBrainz metadata at all.
    """
    per_field: dict[str, bool] = {}
    reasons: list[str] = []
    na: list[str] = []

    # -- albumartist --------------------------------------------------------
    got_aa = getattr(tags, "albumartist", None)
    aa_ok = artist_variant_key(got_aa) == artist_variant_key(track.expect_albumartist)
    if aa_ok and has_feat_clause(got_aa):
        aa_ok = False
        reasons.append(
            f"albumartist has a featuring clause welded into it: {got_aa!r} "
            f"(expected {track.expect_albumartist!r}) — this is what splits "
            f"one artist across several folders"
        )
    elif not aa_ok:
        reasons.append(
            f"albumartist is {got_aa!r}, expected {track.expect_albumartist!r}"
        )
    per_field["albumartist"] = aa_ok

    # -- artist -------------------------------------------------------------
    got_artist = getattr(tags, "artist", None)
    artist_fold = _fold(got_artist or "")
    expected_lead = artist_variant_key(track.expect_albumartist)
    artist_ok = bool(expected_lead) and artist_fold.startswith(expected_lead)
    if not artist_ok:
        reasons.append(
            f"artist is {got_artist!r}, which does not lead with "
            f"{track.expect_albumartist!r}"
        )
    per_field["artist"] = artist_ok

    # -- album --------------------------------------------------------------
    if track.expect_album is None:
        na.append("album")
    else:
        got_album = getattr(tags, "album", None)
        album_ok = album_key(got_album) == album_key(track.expect_album)
        if not album_ok:
            reasons.append(f"album is {got_album!r}, expected {track.expect_album!r}")
        per_field["album"] = album_ok

    # -- title --------------------------------------------------------------
    got_title = getattr(tags, "title", None)
    title_ok = title_key(got_title) == title_key(track.title)
    if not title_ok:
        reasons.append(f"title is {got_title!r}, expected {track.title!r}")
    per_field["title"] = title_ok

    # -- track number -------------------------------------------------------
    if track.expect_album is None:
        na.append("track")
    else:
        got_track = getattr(tags, "track", None)
        track_ok = isinstance(got_track, int) and got_track >= 1
        if not track_ok:
            reasons.append(
                f"track number is {got_track!r}; an album track must carry a "
                f"positive track number"
            )
        per_field["track"] = track_ok

    # -- MusicBrainz --------------------------------------------------------
    got_mbid = getattr(tags, "mb_trackid", None)
    mbid_ok = is_mbid(got_mbid)
    if not mbid_ok:
        reasons.append(
            f"mb_trackid is {got_mbid!r} — no MusicBrainz recording id, so "
            f"this file was imported `asis` with no MusicBrainz metadata"
        )
    per_field["mb_trackid"] = mbid_ok

    return TagGrade(
        ok=all(per_field.values()),
        per_field=per_field,
        reasons=reasons,
        not_applicable=na,
    )


# ---------------------------------------------------------------------------
# Placement grading (S9)
# ---------------------------------------------------------------------------


@dataclass
class PlacementGrade:
    ok: bool
    reasons: list[str]
    expected_artist_dir: str
    actual_artist_dir: str | None


def grade_placement(path: Path, tree_root: Path, track: Track) -> PlacementGrade:
    """Strict canonical placement for one file.

    Required shape: `<tree_root>/<canonical albumartist>/<album>/<file>`, with
    the artist folder carrying **no** featuring clause and matching the corpus
    `expect_albumartist` once beets' own path sanitisation is accounted for
    (so `jev.` legitimately becomes the folder `jev_`).
    """
    reasons: list[str] = []
    expected = canonical_artist_dir(track.expect_albumartist)

    if not is_under(path, tree_root):
        return PlacementGrade(
            ok=False,
            reasons=[f"{path} is not under the expected tree {tree_root}"],
            expected_artist_dir=expected,
            actual_artist_dir=None,
        )

    relative = Path(str(path)[len(str(tree_root)) :].lstrip("/"))
    parts = relative.parts
    if len(parts) < 2:
        return PlacementGrade(
            ok=False,
            reasons=[
                f"{path} sits {len(parts)} level(s) under {tree_root}; strict "
                f"canonical placement requires <artist>/<album>/<file>"
            ],
            expected_artist_dir=expected,
            actual_artist_dir=None,
        )

    artist_dir = parts[0]
    if has_feat_clause(artist_dir):
        reasons.append(
            f"artist folder {artist_dir!r} contains a featuring clause; the "
            f"canonical folder is {expected!r}"
        )
    elif artist_variant_key(artist_dir) != artist_variant_key(expected):
        reasons.append(
            f"artist folder is {artist_dir!r}, expected {expected!r}"
        )
    elif artist_dir != expected:
        reasons.append(
            f"artist folder is {artist_dir!r}, a case/punctuation variant of "
            f"the canonical {expected!r} — variants are what fragment an "
            f"artist across folders"
        )

    if track.expect_album is not None:
        if len(parts) < 3:
            reasons.append(
                f"{path} has no album folder; expected "
                f"{beets_sanitize(track.expect_album)!r}"
            )
        else:
            album_dir = parts[1]
            if album_key(album_dir) != album_key(track.expect_album):
                reasons.append(
                    f"album folder is {album_dir!r}, expected "
                    f"{beets_sanitize(track.expect_album)!r}"
                )

    return PlacementGrade(
        ok=not reasons,
        reasons=reasons,
        expected_artist_dir=expected,
        actual_artist_dir=artist_dir,
    )


# ---------------------------------------------------------------------------
# beets library access (host side)
# ---------------------------------------------------------------------------


def beets_db_path(profile: str) -> Path:
    """Host path of a profile's beets library.

    `BeetsService._profiles_dir` is `config.paths.data_dir / "beets"` and
    docker-compose bind-mounts `./app_data` at `/app/data`, so the container's
    `/app/data/beets/searches.db` is this repo's `app_data/beets/searches.db`.
    """
    return REPO_ROOT / "app_data" / "beets" / f"{profile}.db"


def _decode_path(raw: Any) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def beets_rows(profile: str, tree_root: Path) -> list[dict]:
    """Every item row in a profile's library, with `path` resolved to a host
    path.

    beets 2.x stores `items.path` **relative to the library directory** (its
    own migration is named `items-relative_path`), which
    `BeetsService._latest_item` also has to compensate for. Absolute legacy
    paths are container paths and are rebased onto the host tree.
    """
    db = beets_db_path(profile)
    if not db.exists():
        return []
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        raw_rows = conn.execute(
            "SELECT id, path, albumartist, artist, album, title, track, "
            "mb_trackid, mb_albumid, added FROM items"
        ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - live-only failure mode
        pytest.fail(f"could not read beets library {db}: {exc}")
    finally:
        conn.close()

    rows: list[dict] = []
    for raw in raw_rows:
        row = dict(raw)
        stored = _decode_path(row["path"])
        resolved = Path(stored)
        if not resolved.is_absolute():
            resolved = tree_root / stored
        row["stored_path"] = stored
        row["host_path"] = resolved
        row["profile"] = profile
        rows.append(row)
    return rows


def beets_max_added(profile: str) -> float:
    db = beets_db_path(profile)
    if not db.exists():
        return 0.0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(added) FROM items").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    finally:
        conn.close()


def beets_prune_since(profile: str, after_added: float) -> int:
    """Delete rows this test added. Mirrors `BeetsService._delete_since` — the
    S10 cleanup path, so a dedup test never leaves the library dirtier than it
    found it."""
    db = beets_db_path(profile)
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute("DELETE FROM items WHERE added > ?", (after_added,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# In-container drivers
#
# S10 needs to run the *real* `BeetsService.import_file` — same code, same
# beets binary, same profile DBs, same filesystem — without queueing a
# download. `docker compose exec` into the running musica container is the
# only way to get that: nothing is mocked, and nothing is re-implemented.
# ---------------------------------------------------------------------------

_PREAMBLE = """
import json, os, shutil, sys
from pathlib import Path
from app.config import Config
_cfg = Config(os.environ.get("MUSICA_CONFIG_PATH"))
_cfg.load()
def _emit(payload):
    print("%s" + json.dumps(payload, default=str))
""" % (
    _JSON_SENTINEL,
)

_IMPORT_DRIVER = (
    _PREAMBLE
    + """
from app.services.beets import BeetsService
source = Path(sys.argv[1])
is_rec = sys.argv[2] == "rec"
existed = source.exists()
svc = BeetsService(_cfg)
result = svc.import_file(source, is_rec=is_rec)
_emit({
    "source": str(source),
    "source_existed_before": existed,
    "source_exists_after": source.exists(),
    "matched": bool(result.matched),
    "target_path": str(result.target_path) if result.target_path else None,
    "error": result.error,
    "duplicate": bool(result.duplicate),
    "ok": bool(result.ok),
    "handled": bool(result.handled),
})
"""
)

_STAGE_DRIVER = (
    _PREAMBLE
    + """
src = Path(sys.argv[1]); dst = Path(sys.argv[2])
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(src, dst)
_emit({"dst": str(dst), "size": dst.stat().st_size})
"""
)

_MOVE_DRIVER = (
    _PREAMBLE
    + """
src = Path(sys.argv[1]); dst = Path(sys.argv[2])
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.move(str(src), str(dst))
_emit({"src": str(src), "dst": str(dst), "src_exists": src.exists()})
"""
)

_RMTREE_DRIVER = (
    _PREAMBLE
    + """
removed = []
for raw in sys.argv[1:]:
    p = Path(raw)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True); removed.append(str(p))
    elif p.exists():
        p.unlink(missing_ok=True); removed.append(str(p))
_emit({"removed": removed})
"""
)


def compose_exec_python(script: str, *args: str, timeout: float = 300.0) -> dict:
    """Run a driver inside the musica container and return its JSON payload.

    Written here rather than added to `harness.DockerControl` because another
    agent owns that file this wave; the duplication is deliberate and small.
    """
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "musica",
        "python",
        "-c",
        script,
        *args,
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_JSON_SENTINEL):
            return json.loads(line[len(_JSON_SENTINEL) :])
    raise AssertionError(
        "in-container driver produced no result\n"
        f"exit={proc.returncode}\nstdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def run_id() -> str:
    """Shared with the other stage files when the runner exports
    `MUSICA_LIVE_RUN_ID`; otherwise unique to this process."""
    return os.environ.get("MUSICA_LIVE_RUN_ID") or f"import-{int(time.time())}"


@pytest.fixture(scope="session")
def music_host_root() -> Path:
    """Host path of the music tree — `MUSIC_HOST_DIR` from the repo `.env`,
    the same value docker compose bind-mounts at `/music`."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        pytest.skip(f"no .env at {env_file}; cannot locate the music tree")
    value = parse_env(env_file.read_text()).get("MUSIC_HOST_DIR", "")
    if not value:
        pytest.skip("MUSIC_HOST_DIR is not set in .env")
    root = Path(value).expanduser()
    if not root.is_dir():
        pytest.skip(f"MUSIC_HOST_DIR points at {root}, which does not exist")
    return root


@pytest.fixture(scope="session")
def live_paths(music_host_root: Path) -> dict[str, Path]:
    """The live tree roots, read from the container's own config so the tests
    grade the paths the app actually uses rather than a second copy of the
    layout that can drift."""
    payload = compose_exec_python(
        _PREAMBLE
        + """
_emit({
    "searches": str(_cfg.paths.searches_path),
    "discovery": str(_cfg.paths.discovery_path),
    "download": str(_cfg.paths.download_path),
    "library": str(_cfg.paths.library_path),
    "beets_enabled": bool(_cfg.beets.enabled),
    "beets_binary": _cfg.beets.binary,
})
""",
        timeout=120.0,
    )
    if not payload["beets_enabled"]:
        pytest.skip("beets is disabled in the live config; S7-S10 grade beets")

    paths = {
        "music_root": music_host_root,
        "container_searches": Path(payload["searches"]),
        "container_discovery": Path(payload["discovery"]),
        "container_download": Path(payload["download"]),
    }
    for name, container in (
        ("searches", payload["searches"]),
        ("discovery", payload["discovery"]),
        ("download", payload["download"]),
        ("library", payload["library"]),
    ):
        host = container_to_host(container, music_host_root)
        assert host is not None, (
            f"config's {name} path {container!r} is not under "
            f"{CONTAINER_MUSIC_ROOT}; the host mapping cannot be derived"
        )
        paths[name] = host
    paths["complete"] = paths["download"] / COMPLETE_SUBDIR
    return paths


@pytest.fixture
def ledger(stack, artifact_root: Path) -> dict[str, list[DownloadRecord]]:
    """Corpus track -> the `downloads` rows it produced, this session.

    Prefers `<artifact_root>/download_ledger.jsonl` when the download wave
    wrote one (see the module docstring); otherwise reconstructs from
    `musica.db`.
    """
    tracks = tracks_in_run_order()
    reconstructed = build_ledger(stack.db.downloads(), stack.db.searches(), tracks)

    explicit = artifact_root / "download_ledger.jsonl"
    if not explicit.exists():
        return reconstructed

    by_id = {r["id"]: r for r in stack.db.downloads()}
    searches_by_id = {s["id"]: s for s in stack.db.searches()}
    declared: dict[str, list[DownloadRecord]] = {track_key(t): [] for t in tracks}
    for line in explicit.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        key = f"{entry.get('artist')} - {entry.get('title')}"
        row = by_id.get(entry.get("transfer_id"))
        if key in declared and row is not None:
            declared[key].append(
                _row_to_record(row, searches_by_id.get(row.get("search_id")))
            )
    # A declared ledger that says nothing about a track falls back to the
    # reconstruction rather than silently reporting "never downloaded".
    return {k: (v or reconstructed.get(k, [])) for k, v in declared.items()}


@contextmanager
def timed() -> Iterator[dict]:
    """Wall time of the grading itself. See the module docstring on latency."""
    box = {"latency_s": 0.0}
    start = time.monotonic()
    try:
        yield box
    finally:
        box["latency_s"] = round(time.monotonic() - start, 3)


def _recent_musica_log(stack, lines: str = "4000") -> str:
    """Bounded tail of musica's log. `stack.logs.raw()` is unbounded and a
    long live run makes it tens of megabytes."""
    try:
        return stack.docker.logs("musica", tail=lines)
    except Exception as exc:  # pragma: no cover - live-only failure mode
        return f"<could not read musica logs: {exc}>"


def _log_lines_about(log: str, needle: str, limit: int = 6) -> list[str]:
    return [line for line in log.splitlines() if needle in line][-limit:]


def _pick_landed(
    records: list[DownloadRecord],
) -> DownloadRecord | None:
    """The row that actually produced a file, preferring a real import.

    A track downloaded twice has several completed rows; the one worth grading
    S7/S8/S9 against is the one with a target_dir. When *none* has one, the
    first completed row is returned so the failure names a concrete row.
    """
    completed = [r for r in records if r.state == "completed"]
    if not completed:
        return None
    with_target = [r for r in completed if r.target_dir.strip()]
    return with_target[-1] if with_target else completed[0]


def _skip_downstream(scorecard, stage: Stage, *, scenario: str, run_id: str, why: str):
    scorecard.skip_from(stage, scenario=scenario, run_id=run_id, why=why)


# Per-tier accumulators for the summary tests. Module-level on purpose: the
# report generator can recompute these from the scorecard JSONL, but a summary
# StageResult in the same run is what makes the number visible without
# post-processing. Do not run this file under pytest-xdist.
_S7_STATS: dict[str, dict[str, int]] = {}
_S8_STATS: dict[str, dict[str, int]] = {}


def _bump(store: dict[str, dict[str, int]], tier: str, key: str, amount: int = 1):
    store.setdefault(tier, {}).setdefault(key, 0)
    store[tier][key] += amount


CORPUS_TRACKS = tracks_in_run_order()


# ===========================================================================
# S7 — beets import
# ===========================================================================


@pytest.mark.parametrize("track", CORPUS_TRACKS, ids=track_id)
def test_s7_beets_import(track, stack, scorecard, ledger, live_paths, run_id):
    """S7: did beets import this download, exit 0, and land it in the right
    tree?

    Three witnesses have to agree, because any one of them lies on its own:

    1. the `downloads` row (`file_moved` / `target_dir`) — musica's claim,
    2. the file on disk at that target — the only thing that is actually true,
    3. the beets library row — which is what the *next* download's dedup will
       consult, and which is the thing that was silently wrong.

    `matched` vs `asis` (P6.6-4's `downloads.import_unmatched`) is recorded per
    track and aggregated per tier by `test_s7_matched_ratio_by_tier`.
    """
    scenario = f"S7 import {track_key(track)}"
    records = ledger[track_key(track)]

    with timed() as t:
        if not records:
            scorecard.grade(
                Stage.S7_BEETS_IMPORT,
                False,
                scenario=scenario,
                run_id=run_id,
                track=track_key(track),
                tier=track.tier.value,
                latency_s=t["latency_s"],
                detail=(
                    "no downloads row for this corpus track — S1-S6 never "
                    "delivered a file, so the import path was never exercised"
                ),
                evidence={"ledger_empty": True},
            )
            _skip_downstream(
                scorecard,
                Stage.S8_TAGS_CORRECT,
                scenario=scenario,
                run_id=run_id,
                why="S7 had no download to import",
            )
            pytest.skip(f"no download landed for {track_key(track)} in this session")

        record = _pick_landed(records)
        assert record is not None
        source = slskd_source_path(
            live_paths["download"], record.username, record.filename
        )
        source_exists = source.exists()
        target_dir = container_to_host(record.target_dir, live_paths["music_root"])
        expected_tree = live_paths["discovery" if record.is_rec else "searches"]

        log = _recent_musica_log(stack)
        failure_lines = _log_lines_about(log, "beet import failed")
        skip_lines = _log_lines_about(log, "already in the library")

        problems: list[str] = []

        strand = stranding_defect(record, source_exists)
        if strand:
            problems.append(strand)

        if not record.file_moved:
            problems.append(
                f"downloads row {record.id} is state={record.state!r} with "
                f"file_moved=0 — the import never completed"
            )

        target_file: Path | None = None
        if target_dir is None and record.target_dir.strip():
            problems.append(
                f"target_dir {record.target_dir!r} is not under "
                f"{CONTAINER_MUSIC_ROOT}; it cannot be resolved on the host"
            )
        elif target_dir is not None:
            if not target_dir.is_dir():
                problems.append(
                    f"downloads row {record.id} points at target_dir "
                    f"{record.target_dir!r} but {target_dir} does not exist "
                    f"on disk"
                )
            else:
                audio = [
                    p
                    for p in sorted(target_dir.iterdir())
                    if p.suffix.lower() in AUDIO_EXTENSIONS
                ]
                if not audio:
                    problems.append(
                        f"{target_dir} exists but contains no audio file"
                    )
                else:
                    target_file = _best_target_file(audio, track)
            if not is_under(target_dir, expected_tree):
                problems.append(
                    f"landed in the wrong tree: {record.target_dir!r} is not "
                    f"under {expected_tree} "
                    f"(is_rec_download={record.is_rec} routes to "
                    f"{'Discovery' if record.is_rec else 'Searches'})"
                )

        if source_exists and record.target_dir.strip():
            problems.append(
                f"beets used `move: yes`, so a real import consumes the "
                f"source — but {source} is still on disk alongside a "
                f"non-empty target_dir"
            )

        # matched vs asis, from two independent sources.
        beets_row = _beets_row_for(target_file, record.profile, live_paths)
        beets_matched = bool(beets_row and is_mbid(beets_row.get("mb_trackid")))
        row_matched = not record.import_unmatched
        if beets_row is None and target_file is not None:
            problems.append(
                f"{target_file} is on disk but has NO row in the "
                f"{record.profile} beets library — it is invisible to dedup, "
                f"so the next copy of this track will import again"
            )
        if beets_row is not None and beets_matched != row_matched:
            problems.append(
                f"musica and beets disagree about the import mode: "
                f"downloads.import_unmatched={record.import_unmatched} says "
                f"{'asis' if record.import_unmatched else 'matched'}, the "
                f"beets row's mb_trackid says "
                f"{'matched' if beets_matched else 'asis'}"
            )

        # The beets row is authoritative for matched-vs-`asis`: an MBID either
        # is in the library or is not. `import_unmatched` defaults to 0, so
        # trusting it alone would score every never-imported download as
        # "matched" — the exact kind of flattering default this suite exists
        # to remove.
        if beets_row is not None:
            mode = "matched" if beets_matched else "asis"
        elif record.import_unmatched:
            mode = "asis"
        else:
            mode = "unknown"
        _bump(_S7_STATS, track.tier.value, mode)
        _bump(_S7_STATS, track.tier.value, "total")

        import_lag = None
        if target_file is not None and record.completed_at:
            try:
                import_lag = round(target_file.stat().st_mtime - record.completed_at, 2)
            except OSError:
                import_lag = None

        evidence = {
            "download_row": asdict(record),
            "source_path": str(source),
            "source_exists": source_exists,
            "target_dir_host": str(target_dir) if target_dir else None,
            "target_file": str(target_file) if target_file else None,
            "expected_tree": str(expected_tree),
            "import_mode": mode,
            "import_unmatched_column": record.import_unmatched,
            "beets_row": _jsonable_beets_row(beets_row),
            "beets_log_import_failures": failure_lines,
            "beets_log_duplicate_skips": skip_lines,
            "import_lag_s_estimate": import_lag,
            "duplicate_download_rows": len(records),
        }

    ok = not problems
    scorecard.grade(
        Stage.S7_BEETS_IMPORT,
        ok,
        scenario=scenario,
        run_id=run_id,
        track=track_key(track),
        tier=track.tier.value,
        latency_s=t["latency_s"],
        detail=(
            f"imported {mode} into {record.profile}"
            if ok
            else " | ".join(problems)
        ),
        evidence=evidence,
    )
    if not ok:
        _skip_downstream(
            scorecard,
            Stage.S8_TAGS_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why=f"S7 failed: {problems[0]}",
        )
    assert ok, (
        f"S7 beets import FAILED for {track_key(track)} "
        f"[{track.tier.value}; stresses: {track.stresses}]\n  - "
        + "\n  - ".join(problems)
    )


def _best_target_file(audio: list[Path], track: Track) -> Path:
    """Pick the file in a target dir that is this track. A target dir can hold
    several tracks off the same album once more than one has been imported."""
    for path in audio:
        if title_key(path.stem) == title_key(track.title):
            return path
    for path in audio:
        if title_key(track.title) in title_key(path.stem):
            return path
    return audio[0]


def _beets_row_for(
    target_file: Path | None, profile: str, live_paths: dict[str, Path]
) -> dict | None:
    if target_file is None:
        return None
    tree = live_paths[profile]
    for row in beets_rows(profile, tree):
        if same_path(row["host_path"], target_file):
            return row
    return None


def _jsonable_beets_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k not in {"path", "host_path"}}
    out["host_path"] = str(row["host_path"])
    return out


def test_s7_matched_ratio_by_tier(scorecard, run_id):
    """The matched-vs-`asis` ratio per tier — one of the statistics the user
    asked for.

    Recorded as its own StageResult so the number exists in the scorecard
    without post-processing. It is a *report*, not a claim: a low matched ratio
    on RARE is expected (MusicBrainz genuinely does not know some of it), a low
    one on POPULAR is a finding.
    """
    if not _S7_STATS:
        pytest.skip("no S7 results in this session")
    summary = {}
    for tier, counts in _S7_STATS.items():
        matched = counts.get("matched", 0)
        asis = counts.get("asis", 0)
        classified = matched + asis
        summary[tier] = {
            "matched": matched,
            "asis": asis,
            # Downloads that never produced a beets row at all: neither
            # matched nor asis, because no import happened. Counted
            # separately so they cannot inflate either side of the ratio.
            "unknown_no_beets_row": counts.get("unknown", 0),
            "total": counts.get("total", 0),
            "matched_ratio": (
                round(matched / classified, 3) if classified else None
            ),
        }
    scorecard.grade(
        Stage.S7_BEETS_IMPORT,
        True,
        scenario="S7 matched/asis ratio by tier",
        run_id=run_id,
        detail="; ".join(
            f"{tier}: {v['matched']} matched / {v['asis']} asis "
            f"/ {v['unknown_no_beets_row']} never imported"
            for tier, v in summary.items()
        ),
        evidence={"by_tier": summary},
    )


# ===========================================================================
# S8 — tags correct
# ===========================================================================


@pytest.mark.parametrize("track", CORPUS_TRACKS, ids=track_id)
def test_s8_tags_correct(track, probes, scorecard, ledger, live_paths, run_id):
    """S8: are the tags **in the file** what the user asked for?

    Read with mutagen through `probes.tags`, never from beets' library — the
    whole premise of this suite is that beets' opinion of a file and the file
    itself have diverged.
    """
    scenario = f"S8 tags {track_key(track)}"
    records = ledger[track_key(track)]
    record = _pick_landed(records) if records else None

    with timed() as t:
        if record is None or not record.target_dir.strip():
            scorecard.record(
                _unreached(
                    Stage.S8_TAGS_CORRECT,
                    scenario,
                    run_id,
                    track,
                    "no imported file for this track (S7 did not produce one)",
                )
            )
            _skip_downstream(
                scorecard,
                Stage.S9_PLACEMENT_CORRECT,
                scenario=scenario,
                run_id=run_id,
                why="S8 had no file to read",
            )
            pytest.skip(f"no imported file for {track_key(track)}")

        target_dir = container_to_host(record.target_dir, live_paths["music_root"])
        if target_dir is None or not target_dir.is_dir():
            scorecard.record(
                _unreached(
                    Stage.S8_TAGS_CORRECT,
                    scenario,
                    run_id,
                    track,
                    f"target_dir {record.target_dir!r} does not exist on disk",
                )
            )
            pytest.skip("target dir missing; S7 already reported it")

        audio = [
            p for p in sorted(target_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio:
            scorecard.record(
                _unreached(
                    Stage.S8_TAGS_CORRECT,
                    scenario,
                    run_id,
                    track,
                    f"{target_dir} holds no audio file",
                )
            )
            pytest.skip("no audio at the target; S7 already reported it")

        path = _best_target_file(audio, track)
        tags = probes.tags.read(path)
        grade = grade_tags(tags, track)

        # Cross-check against the probe's own grading. A disagreement is a
        # harness finding, not a musica finding, so it is recorded rather than
        # asserted.
        try:
            probe_ok, probe_reason = probes.tags.grade(path, track)
        except Exception as exc:  # pragma: no cover - live-only
            probe_ok, probe_reason = None, f"TagProbe.grade raised {exc!r}"

        _bump(_S8_STATS, track.tier.value, "files")
        _bump(_S8_STATS, track.tier.value, "fields_ok", grade.fields_ok)
        _bump(_S8_STATS, track.tier.value, "fields_graded", grade.graded_fields)
        if grade.ok:
            _bump(_S8_STATS, track.tier.value, "files_ok")

        evidence = {
            "path": str(path),
            "tags": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in vars(tags).items()
            },
            "per_field": grade.per_field,
            "not_applicable": grade.not_applicable,
            "expected": {
                "albumartist": track.expect_albumartist,
                "album": track.expect_album,
                "title": track.title,
            },
            "tagprobe_grade": {"ok": probe_ok, "reason": probe_reason},
            "stresses": track.stresses,
        }

    scorecard.grade(
        Stage.S8_TAGS_CORRECT,
        grade.ok,
        scenario=scenario,
        run_id=run_id,
        track=track_key(track),
        tier=track.tier.value,
        latency_s=t["latency_s"],
        detail=grade.summary(),
        evidence=evidence,
    )
    if not grade.ok:
        _skip_downstream(
            scorecard,
            Stage.S9_PLACEMENT_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why=f"S8 failed: {grade.reasons[0]}",
        )
    assert grade.ok, (
        f"S8 tags WRONG for {track_key(track)} [{track.tier.value}] at {path}\n  - "
        + "\n  - ".join(grade.reasons)
    )


def _unreached(stage: Stage, scenario: str, run_id: str, track: Track, why: str):
    """A stage that was never reached for this track — SKIP, never PASS."""
    return StageResult(
        stage=stage,
        verdict=Verdict.SKIP,
        scenario=scenario,
        run_id=run_id,
        track=track_key(track),
        tier=track.tier.value,
        detail=why,
    )


def test_s8_tag_accuracy_by_tier(scorecard, run_id):
    """Per-tier tag accuracy — files fully correct, and the field-level rate.

    Both numbers matter: "0/4 files fully correct" and "22/24 fields correct"
    describe very different problems, and only reporting the first is how a
    single missing MBID reads as "tagging is broken".
    """
    if not _S8_STATS:
        pytest.skip("no S8 results in this session")
    summary = {
        tier: {
            "files": c.get("files", 0),
            "files_ok": c.get("files_ok", 0),
            "file_accuracy": (
                round(c.get("files_ok", 0) / c["files"], 3) if c.get("files") else None
            ),
            "fields_ok": c.get("fields_ok", 0),
            "fields_graded": c.get("fields_graded", 0),
            "field_accuracy": (
                round(c.get("fields_ok", 0) / c["fields_graded"], 3)
                if c.get("fields_graded")
                else None
            ),
        }
        for tier, c in _S8_STATS.items()
    }
    scorecard.grade(
        Stage.S8_TAGS_CORRECT,
        True,
        scenario="S8 tag accuracy by tier",
        run_id=run_id,
        detail="; ".join(
            f"{tier}: {v['files_ok']}/{v['files']} files, "
            f"{v['fields_ok']}/{v['fields_graded']} fields"
            for tier, v in summary.items()
        ),
        evidence={"by_tier": summary},
    )


# ===========================================================================
# S9 — placement correct (strict canonical)
# ===========================================================================


@pytest.mark.parametrize("track", CORPUS_TRACKS, ids=track_id)
def test_s9_placement_per_track(track, scorecard, ledger, live_paths, run_id):
    """S9, per track: is this file at `<tree>/<canonical artist>/<album>/…`?

    Strict canonical, per the user's explicit decision. The failure message
    names the specific defect — a feat. clause in the artist folder, a case
    variant, a missing album level — because "placement wrong" is precisely
    the unactionable phrasing this suite exists to replace.
    """
    scenario = f"S9 placement {track_key(track)}"
    records = ledger[track_key(track)]
    record = _pick_landed(records) if records else None

    with timed() as t:
        if record is None or not record.target_dir.strip():
            scorecard.record(
                _unreached(
                    Stage.S9_PLACEMENT_CORRECT,
                    scenario,
                    run_id,
                    track,
                    "no imported file to place",
                )
            )
            pytest.skip(f"no imported file for {track_key(track)}")

        target_dir = container_to_host(record.target_dir, live_paths["music_root"])
        tree = live_paths["discovery" if record.is_rec else "searches"]
        if target_dir is None or not target_dir.is_dir():
            scorecard.record(
                _unreached(
                    Stage.S9_PLACEMENT_CORRECT,
                    scenario,
                    run_id,
                    track,
                    f"target_dir {record.target_dir!r} is not on disk",
                )
            )
            pytest.skip("target dir missing; S7 already reported it")

        audio = [
            p for p in sorted(target_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio:
            scorecard.record(
                _unreached(
                    Stage.S9_PLACEMENT_CORRECT,
                    scenario,
                    run_id,
                    track,
                    f"{target_dir} holds no audio file",
                )
            )
            pytest.skip("no audio at the target; S7 already reported it")

        path = _best_target_file(audio, track)
        grade = grade_placement(path, tree, track)

        evidence = {
            "path": str(path),
            "tree_root": str(tree),
            "expected_artist_dir": grade.expected_artist_dir,
            "actual_artist_dir": grade.actual_artist_dir,
            "reasons": grade.reasons,
            "stresses": track.stresses,
        }

    scorecard.grade(
        Stage.S9_PLACEMENT_CORRECT,
        grade.ok,
        scenario=scenario,
        run_id=run_id,
        track=track_key(track),
        tier=track.tier.value,
        latency_s=t["latency_s"],
        detail=(
            f"canonical: {grade.expected_artist_dir}/…"
            if grade.ok
            else " | ".join(grade.reasons)
        ),
        evidence=evidence,
    )
    if not grade.ok:
        _skip_downstream(
            scorecard,
            Stage.S10_DEDUP_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why=f"S9 failed: {grade.reasons[0]}",
        )
    assert grade.ok, (
        f"S9 placement WRONG for {track_key(track)} [{track.tier.value}]\n"
        f"  file: {path}\n  - " + "\n  - ".join(grade.reasons)
    )


def test_s9_tree_audit(probes, scorecard, live_paths, run_id):
    """S9, tree-wide: the whole music tree, graded strict canonical.

    **This test is expected to fail today.** That is the point — it is the
    measurement of the thing the user described as "directory management is
    barely understandable". A clean run means the tree genuinely is clean.

    Every defect class is named individually rather than collapsed into "the
    audit was not clean": which artist fragmented into which folders, which
    files are stranded under `downloads/complete`, which directories are
    empty.
    """
    scenario = "S9 whole-tree audit"
    with timed() as t:
        audit = probes.fs.audit()
        detail = describe_audit_defects(audit)
        evidence = {
            "root": str(audit.root),
            "audio_file_count": len(audit.audio_files),
            "artist_folder_variants": {
                k: sorted(v) for k, v in audit.artist_folder_variants.items()
            },
            "stranded_downloads": [str(p) for p in audit.stranded_downloads],
            "partial_files": [str(p) for p in audit.partial_files],
            "stray_files": [str(p) for p in audit.stray_files],
            "empty_dirs": [str(p) for p in audit.empty_dirs],
        }

    scorecard.grade(
        Stage.S9_PLACEMENT_CORRECT,
        audit.clean,
        scenario=scenario,
        run_id=run_id,
        latency_s=t["latency_s"],
        detail=detail,
        evidence=evidence,
    )
    if not audit.clean:
        _skip_downstream(
            scorecard,
            Stage.S10_DEDUP_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why=f"S9 tree audit failed: {detail[:200]}",
        )
    assert audit.clean, (
        f"S9 strict-canonical audit of {audit.root} found defects:\n  - "
        + detail.replace("; ", "\n  - ")
    )


def test_s9_beets_libraries_match_disk(probes, scorecard, run_id):
    """S9/S10 hinge: does each beets library still describe reality?

    This is the check that did not exist, and its absence is the root of the
    whole strand: `duplicate_action: skip` and
    `BeetsService._find_cross_profile_duplicate` both trust these rows, so a
    row whose file is gone silently eats the next download of that track.

    Ground truth 2026-08-12, before any fix: searches held 15 rows for 0 files
    and discovery 35 rows for 1 file.
    """
    scenario = "S9 beets/disk reconciliation"
    with timed() as t:
        reports = {p: probes.beets.reconcile(p) for p in PROFILES}
        inconsistent = {p: r for p, r in reports.items() if not r.consistent}
        detail = (
            "; ".join(
                f"{p}: {len(r.rows_without_files)} row(s) point at files that "
                f"do not exist, {len(r.files_without_rows)} file(s) have no "
                f"row (of {r.total_rows} rows / {r.total_files} files)"
                for p, r in inconsistent.items()
            )
            or "every library row has a file and every file has a row"
        )
        evidence = {
            p: {
                "total_rows": r.total_rows,
                "total_files": r.total_files,
                "rows_without_files": r.rows_without_files[:40],
                "rows_without_files_count": len(r.rows_without_files),
                "files_without_rows": [str(x) for x in r.files_without_rows[:40]],
                "files_without_rows_count": len(r.files_without_rows),
            }
            for p, r in reports.items()
        }

    ok = not inconsistent
    scorecard.grade(
        Stage.S9_PLACEMENT_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        latency_s=t["latency_s"],
        detail=detail,
        evidence=evidence,
    )
    assert ok, (
        "beets' library and the disk disagree — every stale row is a future "
        "false 'already in the library' skip that strands a new download:\n  "
        + detail.replace("; ", "\n  ")
    )


# ===========================================================================
# S10 — dedup correct
# ===========================================================================


@dataclass
class Donor:
    """A real audio file already in a tree, used as the source for the dedup
    scenarios instead of queueing more downloads."""

    path: Path
    profile: str
    tree: Path
    other_profile: str
    other_tree: Path
    mb_trackid: str
    beets_id: int
    container_path: PurePosixPath


@pytest.fixture
def donor(live_paths, scorecard, run_id) -> Donor:
    """One real, MusicBrainz-matched file already on disk.

    **What the runner must supply:** at least one corpus track that imported
    *matched* (an `asis` file has no `mb_trackid`, and
    `_find_cross_profile_duplicate` never checks a NULL, so it would make the
    cross-tree case vacuous). Any matched file in either tree will do — S10
    copies it, never moves it, except in the stale-row case which moves the
    original aside and restores it.

    Set `MUSICA_LIVE_DEDUP_DONOR` to a host path to pin a specific file.
    """
    pinned = os.environ.get("MUSICA_LIVE_DEDUP_DONOR")
    candidates: list[Donor] = []
    for profile in PROFILES:
        tree = live_paths[profile]
        other = "discovery" if profile == "searches" else "searches"
        for row in beets_rows(profile, tree):
            path = row["host_path"]
            if not is_mbid(row.get("mb_trackid")) or not path.is_file():
                continue
            if pinned and not same_path(path, Path(pinned)):
                continue
            container = host_to_container(path, live_paths["music_root"])
            candidates.append(
                Donor(
                    path=path,
                    profile=profile,
                    tree=tree,
                    other_profile=other,
                    other_tree=live_paths[other],
                    mb_trackid=row["mb_trackid"],
                    beets_id=row["id"],
                    container_path=container,
                )
            )
    if not candidates:
        scorecard.skip_from(
            Stage.S10_DEDUP_CORRECT,
            scenario="S10 dedup",
            run_id=run_id,
            why=(
                "no MusicBrainz-matched file exists in either tree, so the "
                "dedup scenarios cannot be manufactured without queueing a "
                "download"
            ),
        )
        pytest.skip(
            "S10 needs one MusicBrainz-matched file already imported. Run the "
            "S1-S6 download wave first, or pin one with MUSICA_LIVE_DEDUP_DONOR."
        )
    return candidates[0]


@dataclass
class DedupSandbox:
    """Staging + cleanup for one S10 scenario.

    Everything S10 creates lives under one synthetic slskd peer directory and
    is removed on teardown, and every beets row added during the scenario is
    pruned, so a dedup test never leaves the tree dirtier than it found it —
    which matters because S9 grades that same tree.
    """

    live_paths: dict[str, Path]
    staging_host: Path
    staging_container: PurePosixPath
    before_added: dict[str, float]
    before_files: set[Path]
    _counter: int = 0

    def stage(self, donor: Donor, label: str) -> tuple[Path, PurePosixPath]:
        """Copy the donor under the synthetic peer, exactly where slskd would
        have put a completed download."""
        self._counter += 1
        name = f"{self._counter:02d}-{label}{donor.path.suffix}"
        host = self.staging_host / name
        container = self.staging_container / name
        compose_exec_python(
            _STAGE_DRIVER, str(donor.container_path), str(container), timeout=120.0
        )
        assert host.is_file(), f"staged copy did not appear on the host at {host}"
        return host, container

    def import_copy(self, container_path: PurePosixPath, *, is_rec: bool) -> dict:
        """Run the real `BeetsService.import_file` on a staged copy."""
        return compose_exec_python(
            _IMPORT_DRIVER,
            str(container_path),
            "rec" if is_rec else "manual",
            timeout=300.0,
        )


@pytest.fixture
def sandbox(live_paths, probes) -> Iterator[DedupSandbox]:
    staging_host = live_paths["complete"] / STAGING_PEER
    staging_container = (
        PurePosixPath(str(live_paths["container_download"]))
        / COMPLETE_SUBDIR
        / STAGING_PEER
    )
    box = DedupSandbox(
        live_paths=live_paths,
        staging_host=staging_host,
        staging_container=staging_container,
        before_added={p: beets_max_added(p) for p in PROFILES},
        before_files=probes.fs.snapshot(),
    )
    try:
        yield box
    finally:
        # 1. anything the scenario created under the music tree. Removed
        #    from inside the container so the unlink runs as the same user
        #    that created the file.
        try:
            created = probes.fs.snapshot() - box.before_files
        except Exception:  # pragma: no cover - live-only failure mode
            created = set()
        leftovers = [
            str(host_to_container(p, live_paths["music_root"]))
            for p in created
            if p.exists() and is_under(p, live_paths["music_root"])
        ]
        if leftovers:
            compose_exec_python(_RMTREE_DRIVER, *leftovers, timeout=120.0)
        # 2. the staging peer directory, whatever state it is in
        if staging_host.exists():
            shutil.rmtree(staging_host, ignore_errors=True)
        # 3. beets rows the scenario added. Mirrors
        #    BeetsService._delete_since — a row left behind here is a stale
        #    row, i.e. the exact defect S10(c) tests for.
        for profile, before in box.before_added.items():
            beets_prune_since(profile, before)


def _tree_copies(live_paths: dict[str, Path], mb_trackid: str) -> dict[str, list[Path]]:
    """Every file on disk, per tree, whose beets row carries this recording."""
    out: dict[str, list[Path]] = {}
    for profile in PROFILES:
        tree = live_paths[profile]
        out[profile] = [
            row["host_path"]
            for row in beets_rows(profile, tree)
            if row.get("mb_trackid") == mb_trackid and row["host_path"].is_file()
        ]
    return out


def _stranded_under_complete(live_paths: dict[str, Path]) -> list[Path]:
    root = live_paths["complete"] / STAGING_PEER
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]


def test_s10a_same_track_twice_same_tree(
    donor, sandbox, scorecard, live_paths, run_id
):
    """S10(a): the same track downloaded twice into the SAME tree.

    Needs from the runner: one matched file already in a tree (see `donor`).
    No download is queued — a copy of that file is staged under a synthetic
    slskd peer and run through the real `BeetsService.import_file`.

    Two separate claims, asserted separately so the failure is legible:

    1. exactly one playable copy survives in the tree, and
    2. the redundant source is not left stranded under `downloads/complete`.

    (2) is currently a **known spec disagreement**: `BeetsService.import_file`
    deliberately leaves a skipped duplicate on disk ("disposal is TrashPurge's
    job"), and there is no TrashPurge — `grep -r TrashPurge app/` finds
    nothing. Under the strict end-state spec for this suite that is a stranded
    file, so it is graded as one and named as such.
    """
    scenario = "S10a duplicate into the same tree"
    with timed() as t:
        _, container = sandbox.stage(donor, "same-tree")
        result = sandbox.import_copy(container, is_rec=(donor.profile == "discovery"))

        copies = _tree_copies(live_paths, donor.mb_trackid)
        same_tree_copies = copies[donor.profile]
        stranded = _stranded_under_complete(live_paths)

        problems: list[str] = []
        if len(same_tree_copies) != 1:
            problems.append(
                f"{len(same_tree_copies)} playable copies of mb_trackid "
                f"{donor.mb_trackid} in the {donor.profile} tree, expected "
                f"exactly 1: " + ", ".join(str(p) for p in same_tree_copies)
            )
        if stranded:
            problems.append(
                f"{len(stranded)} redundant download(s) left stranded under "
                f"downloads/complete: " + ", ".join(str(p) for p in stranded)
                + " — beets skipped the import and nothing disposes of the "
                "source (there is no TrashPurge in app/)"
            )
        if result["ok"] and result["target_path"]:
            problems.append(
                f"beets imported a second copy to {result['target_path']} "
                f"instead of recognising the duplicate"
            )

        evidence = {
            "donor": str(donor.path),
            "mb_trackid": donor.mb_trackid,
            "import_result": result,
            "copies_by_tree": {k: [str(p) for p in v] for k, v in copies.items()},
            "stranded_under_complete": [str(p) for p in stranded],
        }

    ok = not problems
    scorecard.grade(
        Stage.S10_DEDUP_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        latency_s=t["latency_s"],
        detail="exactly one copy, nothing stranded" if ok else " | ".join(problems),
        evidence=evidence,
    )
    assert ok, "S10(a) same-tree dedup FAILED:\n  - " + "\n  - ".join(problems)


def test_s10b_same_track_two_trees(donor, sandbox, scorecard, live_paths, run_id):
    """S10(b): the same track downloaded into DIFFERENT trees.

    This is the case `BeetsService._find_cross_profile_duplicate` exists for:
    beets' own `duplicate_action: skip` only ever consults the *importing*
    profile's library.db, so a rec and a manual search for the same track land
    in both trees undetected.

    Needs from the runner: the same matched donor as S10(a). The copy is
    imported into the *other* profile from the one holding the donor.
    """
    scenario = "S10b duplicate across trees"
    with timed() as t:
        _, container = sandbox.stage(donor, "cross-tree")
        result = sandbox.import_copy(
            container, is_rec=(donor.other_profile == "discovery")
        )

        copies = _tree_copies(live_paths, donor.mb_trackid)
        total = sum(len(v) for v in copies.values())
        stranded = _stranded_under_complete(live_paths)
        holders = probes_find_profiles(live_paths, donor.mb_trackid)

        problems: list[str] = []
        if total != 1:
            problems.append(
                f"{total} playable copies of mb_trackid {donor.mb_trackid} "
                f"across both trees, expected exactly 1: "
                + "; ".join(
                    f"{k}={[str(p) for p in v]}" for k, v in copies.items() if v
                )
            )
        if len(holders) > 1:
            problems.append(
                f"the recording now has a library row in {sorted(holders)} — "
                f"a cross-profile duplicate row survives even if only one file "
                f"does, and it will falsely skip the next import"
            )
        if stranded:
            problems.append(
                f"{len(stranded)} file(s) left stranded under "
                f"downloads/complete: " + ", ".join(str(p) for p in stranded)
            )
        if not result["duplicate"] and result["ok"]:
            problems.append(
                f"the cross-profile check did not fire: beets reported a "
                f"successful import into {donor.other_profile} at "
                f"{result['target_path']} while {donor.profile} already holds "
                f"mb_trackid {donor.mb_trackid}"
            )

        evidence = {
            "donor": str(donor.path),
            "donor_profile": donor.profile,
            "imported_into": donor.other_profile,
            "mb_trackid": donor.mb_trackid,
            "import_result": result,
            "copies_by_tree": {k: [str(p) for p in v] for k, v in copies.items()},
            "profiles_holding_row": sorted(holders),
            "stranded_under_complete": [str(p) for p in stranded],
        }

    ok = not problems
    scorecard.grade(
        Stage.S10_DEDUP_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        latency_s=t["latency_s"],
        detail="one copy across both trees" if ok else " | ".join(problems),
        evidence=evidence,
    )
    assert ok, "S10(b) cross-tree dedup FAILED:\n  - " + "\n  - ".join(problems)


def probes_find_profiles(live_paths: dict[str, Path], mb_trackid: str) -> set[str]:
    """Which profiles hold a library row for this recording.

    Read from the library DBs directly rather than through
    `BeetsProbe.find_by_mb_trackid` so the S10 assertion does not depend on
    the probe implementation it is meant to corroborate.
    """
    holders = set()
    for profile in PROFILES:
        for row in beets_rows(profile, live_paths[profile]):
            if row.get("mb_trackid") == mb_trackid:
                holders.add(profile)
    return holders


def test_s10c_stale_row_does_not_strand_a_new_download(
    donor, sandbox, scorecard, live_paths, run_id
):
    """S10(c) — THE REGRESSION TEST. A library row whose file is gone must not
    cause a new download to be skipped and stranded.

    This is the defect that put five files in `downloads/complete/soulseek/`
    with `downloads.file_moved=1, target_dir=''` while the beets libraries
    claimed dozens of items for a nearly empty disk.

    The stale state is **manufactured deterministically** rather than waited
    for: the donor's own file is moved aside (its library row left behind),
    which is exactly "the file was deleted behind beets' back". A copy of it
    is then staged under a synthetic slskd peer and imported through the real
    `BeetsService.import_file`.

    A correct system re-imports it — a row with no file is not a reason to
    refuse a file. Today beets' `duplicate_action: skip` consults the stale
    row, skips, leaves the source in place, and (through
    `DownloadMonitor._import_via_beets`) marks it handled with an empty
    target_dir. So this test is expected to FAIL until the fix lands, and to
    keep passing afterwards.

    Needs from the runner: the same matched donor. The donor file is restored
    to its original path in teardown whatever happens.
    """
    scenario = "S10c stale library row must not strand a new download"
    quarantine_host = sandbox.staging_host.parent / f"{STAGING_PEER}-quarantine"
    quarantine_container = (
        sandbox.staging_container.parent / f"{STAGING_PEER}-quarantine"
    )
    parked = quarantine_container / donor.path.name
    moved = False

    try:
        with timed() as t:
            # 1. Stage the "new download" *before* disturbing anything, so the
            #    copy is definitely of a complete file.
            _, container = sandbox.stage(donor, "stale-row")

            # 2. Manufacture the stale row: the file goes, the row stays.
            compose_exec_python(
                _MOVE_DRIVER, str(donor.container_path), str(parked), timeout=120.0
            )
            moved = True
            assert not donor.path.exists(), (
                f"could not manufacture the stale-row state: {donor.path} is "
                f"still on disk after the move"
            )
            stale_rows = [
                row
                for row in beets_rows(donor.profile, donor.tree)
                if row.get("mb_trackid") == donor.mb_trackid
                and not row["host_path"].is_file()
            ]
            assert stale_rows, (
                "expected at least one library row pointing at the now-missing "
                "file; the state under test was not created"
            )

            # 3. Import the new copy. This must NOT be skipped.
            result = sandbox.import_copy(
                container, is_rec=(donor.profile == "discovery")
            )

            stranded = _stranded_under_complete(live_paths)
            landed = (
                container_to_host(result["target_path"], live_paths["music_root"])
                if result["target_path"]
                else None
            )

            problems: list[str] = []
            if result["duplicate"]:
                problems.append(
                    f"beets SKIPPED the import as a duplicate because of a "
                    f"stale library row ({len(stale_rows)} row(s) in "
                    f"{donor.profile} point at files that no longer exist, "
                    f"e.g. {stale_rows[0]['stored_path']!r}). This is the "
                    f"false-skip: a new download is refused because of a row "
                    f"whose file is gone."
                )
            if not result["ok"]:
                problems.append(
                    f"the import did not produce a file: error="
                    f"{result['error']!r}"
                )
            if landed is None or not landed.is_file():
                problems.append(
                    f"beets reported target_path={result['target_path']!r} but "
                    f"nothing is on disk there"
                )
            if result["source_exists_after"]:
                problems.append(
                    f"the source is still at {result['source']} — with "
                    f"`move: yes` a real import consumes it, so this file is "
                    f"stranded. DownloadMonitor would now call "
                    f"mark_file_moved(id, '') and never look at it again."
                )
            if stranded:
                problems.append(
                    f"{len(stranded)} file(s) left under downloads/complete: "
                    + ", ".join(str(p) for p in stranded)
                )

            evidence = {
                "donor": str(donor.path),
                "mb_trackid": donor.mb_trackid,
                "stale_rows": [
                    {"id": r["id"], "path": r["stored_path"]} for r in stale_rows[:10]
                ],
                "import_result": result,
                "landed_host_path": str(landed) if landed else None,
                "stranded_under_complete": [str(p) for p in stranded],
            }

        ok = not problems
        scorecard.grade(
            Stage.S10_DEDUP_CORRECT,
            ok,
            scenario=scenario,
            run_id=run_id,
            latency_s=t["latency_s"],
            detail=(
                "a stale row did not block the new import"
                if ok
                else " | ".join(problems)
            ),
            evidence=evidence,
        )
        assert ok, (
            "S10(c) REGRESSION: a stale beets library row stranded a new "
            "download:\n  - " + "\n  - ".join(problems)
        )
    finally:
        if moved:
            # Restore the donor whatever happened, before the sandbox teardown
            # prunes rows — the row for this file must keep describing reality.
            if not donor.path.exists():
                compose_exec_python(
                    _MOVE_DRIVER, str(parked), str(donor.container_path), timeout=120.0
                )
            if quarantine_host.exists():
                shutil.rmtree(quarantine_host, ignore_errors=True)


def test_s10d_download_rows_tell_the_truth(
    stack, scorecard, ledger, live_paths, run_id
):
    """S10(d): does `downloads.file_moved` / `target_dir` describe what really
    happened?

    The one S10 claim that can only be made about *real* transfers, because
    the columns are written by `DownloadMonitor`, not by `BeetsService`. It
    grades every corpus download this session produced against the three-way
    truth: the row, the source under `downloads/complete`, and the target on
    disk.

    Needs from the runner: the S1-S6 wave, nothing else.

    The failure this catches verbatim: `file_moved=1` with an empty
    `target_dir` while the file still sits under
    `downloads/complete/soulseek/`. Five rows on the live stack looked exactly
    like that on 2026-08-12.
    """
    scenario = "S10d downloads rows vs. disk"
    with timed() as t:
        problems: list[str] = []
        rows_examined = 0
        details: list[dict] = []

        for key, records in ledger.items():
            for record in records:
                if record.state != "completed":
                    continue
                rows_examined += 1
                source = slskd_source_path(
                    live_paths["download"], record.username, record.filename
                )
                source_exists = source.exists()
                target_dir = container_to_host(
                    record.target_dir, live_paths["music_root"]
                )
                entry = {
                    "track": key,
                    "id": record.id,
                    "file_moved": record.file_moved,
                    "target_dir": record.target_dir,
                    "source": str(source),
                    "source_exists": source_exists,
                    "target_exists": bool(target_dir and target_dir.is_dir()),
                }
                details.append(entry)

                strand = stranding_defect(record, source_exists)
                if strand:
                    problems.append(f"[{key}] {strand}")
                    continue
                if record.file_moved and target_dir is not None and not target_dir.is_dir():
                    problems.append(
                        f"[{key}] downloads row {record.id} claims file_moved=1 "
                        f"and target_dir={record.target_dir!r}, but that "
                        f"directory does not exist on disk"
                    )
                if record.file_moved and record.target_dir.strip() and source_exists:
                    problems.append(
                        f"[{key}] downloads row {record.id} claims the file "
                        f"moved to {record.target_dir!r}, but the source is "
                        f"still at {source} — two copies, or a move that only "
                        f"half happened"
                    )

        evidence = {"rows_examined": rows_examined, "rows": details[:60]}

    if rows_examined == 0:
        scorecard.skip_from(
            Stage.S10_DEDUP_CORRECT,
            scenario=scenario,
            run_id=run_id,
            why="no completed corpus downloads in this session",
        )
        pytest.skip("no completed corpus downloads to grade")

    ok = not problems
    scorecard.grade(
        Stage.S10_DEDUP_CORRECT,
        ok,
        scenario=scenario,
        run_id=run_id,
        latency_s=t["latency_s"],
        detail=(
            f"all {rows_examined} completed rows agree with disk"
            if ok
            else " | ".join(problems[:10])
        ),
        evidence=evidence,
    )
    assert ok, (
        f"S10(d) the downloads table does not describe what is on disk "
        f"({len(problems)} of {rows_examined} completed rows):\n  - "
        + "\n  - ".join(problems)
    )
