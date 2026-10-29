"""track_state / file_state transitions (#93)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import states as s

MIGRATION = (
    Path(__file__).parents[2] / "app" / "stores" / "migrations" / "001_initial.sql"
).read_text()


def _check_list(column: str) -> set[str]:
    m = re.search(
        rf"{column}\s+TEXT.*?CHECK \({column} IN \((.*?)\)\)", MIGRATION, re.DOTALL
    )
    assert m, column
    return set(re.findall(r"'(\w+)'", m.group(1)))


def test_state_lists_match_migration():
    assert s.TRACK_STATES == _check_list("track_state")
    assert s.FILE_STATES == _check_list("file_state")


def test_tables_cover_every_state():
    assert set(s.TRACK_TRANSITIONS) == s.TRACK_STATES
    assert set(s.FILE_TRANSITIONS) == s.FILE_STATES
    for table, states in (
        (s.TRACK_TRANSITIONS, s.TRACK_STATES),
        (s.FILE_TRANSITIONS, s.FILE_STATES),
    ):
        for targets in table.values():
            assert targets <= states


def test_happy_path():
    st = "requested"
    st = s.start_search(st)
    st = s.search_done(st, found=True)
    st = s.start_download(st)
    assert s.download_done(st) == "downloaded"
    f = "downloaded"
    for step in (
        s.file_verified,
        s.file_tagged,
        s.file_moved,
        s.file_indexed,
        s.file_deleted,
    ):
        f = step(f)
    assert f == "deleted"


def test_retry_after_failure():
    st = s.download_failed("midDownload", bad=True)
    assert st == "badDownload"
    assert s.start_search(s.re_request(st)) == "searching"


@pytest.mark.parametrize(
    "old", ["requested", "reRequested", "searching", "found", "midDownload"]
)
def test_drop_before_end(old):
    assert s.drop(old) == "dropped"


@pytest.mark.parametrize(
    ("fn", "old"),
    [
        (s.start_download, "requested"),  # skips the search
        (s.drop, "downloaded"),  # already ended
        (s.re_request, "searching"),  # still running
        (s.search_done, "found"),
        (s.file_indexed, "downloaded"),
        (s.file_verified, "deleted"),
        (s.file_tagged, "moved"),
    ],
)
def test_illegal_jumps_raise(fn, old):
    kwargs = {"found": True} if fn is s.search_done else {}
    with pytest.raises(s.IllegalTransition):
        fn(old, **kwargs)


def test_tagging_may_be_skipped():
    assert s.file_moved("verified") == "moved"


def test_unknown_state_raises():
    with pytest.raises(s.IllegalTransition):
        s.track("bogus", "searching")
    with pytest.raises(s.IllegalTransition):
        s.file_replaced("bogus")


@pytest.mark.parametrize("old", sorted(s.FILE_STATES))
def test_replace_resets_any_state(old):
    assert s.file_replaced(old) == "downloaded"


def test_release_only_from_searching() -> None:
    """Crash or app restart (user, 2026-10-29): searching goes back to requested."""
    assert s.release("searching") == "requested"
    for old in ("requested", "found", "midDownload", "downloaded"):
        with pytest.raises(s.IllegalTransition):
            s.release(old)
