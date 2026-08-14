"""
`BeetsProbe` — the per-profile beets libraries, and whether they match reality.

`reconcile()` is the check whose absence let stale rows start eating
downloads. beets' `duplicate_action: skip` trusts its own library database
completely: a row that points at a file which no longer exists still counts
as "already in the library", so the next download of that track is skipped
silently, forever, and nothing anywhere reports it. The reverse gap is just
as bad — a file with no row is invisible to the dedup check and will be
downloaded again.

**The path gotcha, which decides whether any of this is right.** beets 2.x
stores `items.path`

- as **bytes**, not text, and
- **relative to the profile's configured `directory:`**, not absolute
  (beets' own migration is named `items-relative_path`; the handling this
  mirrors is `app/services/beets.py:_latest_item`).

and the `directory:` is a *container* path (`/music/Searches`) that has to
be mapped to the host before anything can be opened. Get any one of those
three wrong and every reconciliation reports a total mismatch — every row
missing its file, every file missing its row — which looks exactly like the
catastrophic bug this is meant to detect.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from tests.live.probes.contract import BeetsProbe, BeetsReconciliation
from tests.live.probes.fs import is_audio
from tests.live.probes.paths import beets_profiles_dir, to_host, tree_path

#: The profiles `app/services/beets.py` manages. Kept in step with its
#: `_PROFILES`; a profile missing here would simply never be reconciled.
PROFILES: tuple[str, ...] = ("searches", "discovery")

#: Columns worth carrying into a test. `path` is handled separately.
_ITEM_COLUMNS = (
    "id",
    "albumartist",
    "artist",
    "album",
    "title",
    "track",
    "mb_trackid",
    "mb_albumid",
    "mb_releasetrackid",
    "format",
    "bitrate",
    "length",
    "added",
    "mtime",
)

_DIRECTORY_RE = re.compile(r"^\s*directory\s*:\s*(?P<dir>.+?)\s*$", re.MULTILINE)

#: macOS and Windows compare filenames case-insensitively, so two spellings
#: of the same path are the same file there and must not be reported as a
#: mismatch. On Linux they are genuinely different files.
_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


def path_key(path: Path | str) -> str:
    """Comparison key for a filesystem path.

    Unicode normalisation is not optional here: macOS hands back decomposed
    (NFD) filenames from `os.walk` while beets stores whatever the tag said,
    usually composed (NFC). Without this, "Jóga" and "Björk" reconcile as
    both a missing file *and* an unknown file — two defects invented out of
    one encoding difference.
    """
    text = unicodedata.normalize("NFC", str(path))
    return text.casefold() if _CASE_INSENSITIVE_FS else text


class LiveBeetsProbe(BeetsProbe):
    def __init__(self, profiles_dir: Path | None = None) -> None:
        self.profiles_dir = Path(profiles_dir) if profiles_dir else beets_profiles_dir()

    # -- locating things ---------------------------------------------------

    def library_db(self, profile: str) -> Path:
        return self.profiles_dir / f"{profile}.db"

    def profile_dir(self, profile: str) -> Path:
        """Host path of the tree this profile files music into.

        Read from the profile's own YAML when it exists — that is the value
        beets actually used — and only falls back to `config.toml` when the
        profile has never been written (a fresh reset, before the first
        import).
        """
        yaml_path = self.profiles_dir / f"{profile}.yaml"
        if yaml_path.exists():
            match = _DIRECTORY_RE.search(yaml_path.read_text())
            if match:
                return to_host(match.group("dir").strip().strip("\"'"))
        return tree_path(profile)

    def profiles(self) -> list[str]:
        """Profiles that actually have a library database on disk."""
        return [p for p in PROFILES if self.library_db(p).exists()]

    # -- reading -----------------------------------------------------------

    def _query(self, profile: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        db = self.library_db(profile)
        if not db.exists():
            return []
        # Read-only, so nothing here can corrupt a library the running
        # container may be writing to at the same moment.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def _row_to_item(self, row: sqlite3.Row, profile: str, base: Path) -> dict:
        raw = row["path"]
        raw_text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        path = Path(raw_text)
        if not path.is_absolute():
            path = base / path
        else:
            # Older beets layouts stored an absolute *container* path.
            path = to_host(path)
        item = {key: row[key] for key in _ITEM_COLUMNS if key in row}
        item["profile"] = profile
        item["path"] = path
        item["raw_path"] = raw_text
        item["exists"] = path.exists()
        return item

    def items(self, profile: str) -> list[dict]:
        base = self.profile_dir(profile)
        rows = self._query(
            profile,
            "SELECT * FROM items ORDER BY added",
        )
        return [self._row_to_item(r, profile, base) for r in rows]

    def find_by_mb_trackid(self, mb_trackid: str) -> dict[str, list[dict]]:
        """Which profiles hold this recording.

        An empty or NULL `mb_trackid` never matches anything: unmatched
        (`asis`) imports all share "no id", and treating that as equality
        would make every unmatched track a duplicate of every other one.
        """
        if not mb_trackid:
            return {}
        found: dict[str, list[dict]] = {}
        for profile in self.profiles():
            base = self.profile_dir(profile)
            rows = self._query(
                profile,
                "SELECT * FROM items WHERE mb_trackid = ?",
                (mb_trackid,),
            )
            if rows:
                found[profile] = [self._row_to_item(r, profile, base) for r in rows]
        return found

    # -- the check that was missing ---------------------------------------

    def reconcile(self, profile: str) -> BeetsReconciliation:
        base = self.profile_dir(profile)
        rows = self.items(profile)

        rows_without_files = [str(item["path"]) for item in rows if not item["exists"]]
        row_keys = {path_key(item["path"]) for item in rows}

        files_without_rows: list[Path] = []
        total_files = 0
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if not path.is_file() or not is_audio(path):
                    continue
                total_files += 1
                if path_key(path) not in row_keys:
                    files_without_rows.append(path)

        return BeetsReconciliation(
            profile=profile,
            rows_without_files=sorted(rows_without_files),
            files_without_rows=files_without_rows,
            total_rows=len(rows),
            total_files=total_files,
        )
