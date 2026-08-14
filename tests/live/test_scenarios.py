"""
Full-pipeline journeys, as pytest entry points.

The journeys themselves live in `scenarios.py`; this module only decides
*which* ones run and against which corpus tracks. Keeping the two apart is
what lets `run_suite.py` drive the same journeys several times with a shared
download budget while a developer can still run exactly one of them by name:

    pytest tests/live/test_scenarios.py -k u1 --live

**These do not fail when the pipeline fails.** A journey that dies at S5
records the failure in the scorecard and returns — `report.py` is where a
broken pipeline shows up, not a red pytest line. A test here fails only when
the *harness* is broken, which is a different problem and deserves to look
different. This is deliberate: the user's question is "what fraction of runs
work", and a suite that aborts on the first failure cannot answer it.

The one thing every test asserts is that the journey recorded *something*.
A journey that silently grades nothing is a harness bug that would otherwise
show up as a suspiciously clean report.
"""

from __future__ import annotations

import pytest

from tests.live.corpus import Track
from tests.live.scenarios import (
    ScenarioContext,
    concurrent_track_set,
    default_tracks_for,
    run_scenario,
    u1_manual_pipeline,
    u2_deep_cuts,
    u3_comfort_zone,
    u4_fresh_picks,
    u5_duplicate_download,
    u6_peer_failure_retry,
    u7_crash_recovery,
    u8_stale_beets_row,
    u9_playlist_lifecycle,
    u10_concurrent_downloads,
)


def _ids(tracks: list[Track]) -> list[str]:
    return [f"{t.tier.value}-{t.artist} - {t.title}" for t in tracks]


def _graded(ctx: ScenarioContext, scenario: str) -> int:
    """How many stages this scenario recorded in this run."""
    return sum(
        1
        for r in ctx.scorecard.results
        if r.scenario == scenario and r.run_id == ctx.run_id
    )


def _assert_recorded(ctx: ScenarioContext, scenario: str, abort: str | None) -> None:
    """The harness-level assertion: the journey left evidence behind.

    Note what this does *not* assert — that the journey succeeded. `abort` is
    printed rather than raised, so a failing pipeline produces a full
    scorecard instead of one red test and twelve unmeasured stages.
    """
    graded = _graded(ctx, scenario)
    assert graded, (
        f"{scenario} graded no stages at all — that is a harness fault, not a "
        f"pipeline finding. Check the scorecard fixture and the Grader wiring."
    )
    if abort:
        print(f"\n[live] {scenario} stopped early: {abort} ({graded} stages graded)")


# ---------------------------------------------------------------------------
# U1 — the journey the user actually described
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "track",
    default_tracks_for("U1_manual_pipeline"),
    ids=_ids(default_tracks_for("U1_manual_pipeline")),
)
def test_u1_manual_pipeline(scenario_ctx: ScenarioContext, track: Track) -> None:
    """Type a name -> tagged, correctly placed, findable, playable track.

    Parametrized over the whole corpus in run order (popular, then awkward,
    then rare), so the per-tier pass ratios in the report come from this one
    test more than any other.
    """
    abort = run_scenario(u1_manual_pipeline, scenario_ctx, track)
    _assert_recorded(scenario_ctx, "U1_manual_pipeline", abort)


# ---------------------------------------------------------------------------
# U2-U4 — the three recommendation categories
# ---------------------------------------------------------------------------


def test_u2_deep_cuts(scenario_ctx: ScenarioContext) -> None:
    """A Deep Cuts pull fills a playlist and downloads what is missing."""
    abort = run_scenario(u2_deep_cuts, scenario_ctx)
    _assert_recorded(scenario_ctx, "U2_deep_cuts", abort)


def test_u3_comfort_zone(scenario_ctx: ScenarioContext) -> None:
    """Comfort Zone — the 1000-track pool. Disabled in the user's config by
    default; `run_suite.py` enables it for the run and restores it after."""
    abort = run_scenario(u3_comfort_zone, scenario_ctx)
    _assert_recorded(scenario_ctx, "U3_comfort_zone", abort)


def test_u4_fresh_picks(scenario_ctx: ScenarioContext) -> None:
    """Fresh Picks, where availability is the measurement.

    Brand-new releases are largely absent from Soulseek — a previous live
    pull had 5/5 fail. Scoring that as a musica defect would be wrong, so
    this journey records the availability rate as its own datum.
    """
    abort = run_scenario(u4_fresh_picks, scenario_ctx)
    _assert_recorded(scenario_ctx, "U4_fresh_picks", abort)


# ---------------------------------------------------------------------------
# U5-U8 — the failure modes the user has been living with
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "track",
    default_tracks_for("U5_duplicate"),
    ids=_ids(default_tracks_for("U5_duplicate")),
)
def test_u5_duplicate_download(scenario_ctx: ScenarioContext, track: Track) -> None:
    """One song downloaded three times must leave exactly one copy on disk."""
    abort = run_scenario(u5_duplicate_download, scenario_ctx, track)
    _assert_recorded(scenario_ctx, "U5_duplicate", abort)


@pytest.mark.parametrize(
    "track",
    default_tracks_for("U6_peer_retry"),
    ids=_ids(default_tracks_for("U6_peer_retry")),
)
def test_u6_peer_failure_retry(scenario_ctx: ScenarioContext, track: Track) -> None:
    """A dead peer is retried elsewhere — without starting a new search."""
    abort = run_scenario(u6_peer_failure_retry, scenario_ctx, track)
    _assert_recorded(scenario_ctx, "U6_peer_retry", abort)


@pytest.mark.parametrize(
    "track",
    default_tracks_for("U7_crash_recovery"),
    ids=_ids(default_tracks_for("U7_crash_recovery")),
)
def test_u7_crash_recovery(scenario_ctx: ScenarioContext, track: Track) -> None:
    """SIGKILL mid-transfer: no partial file, no orphan, no lost row.

    Kills the musica container. Runs last in the suite ordering for that
    reason — see `run_suite.py`.
    """
    abort = run_scenario(u7_crash_recovery, scenario_ctx, track)
    _assert_recorded(scenario_ctx, "U7_crash_recovery", abort)


@pytest.mark.parametrize(
    "track",
    default_tracks_for("U8_stale_beets_row"),
    ids=_ids(default_tracks_for("U8_stale_beets_row")),
)
def test_u8_stale_beets_row(scenario_ctx: ScenarioContext, track: Track) -> None:
    """The regression test for the defect that ate the user's downloads.

    A beets library row whose file was deleted behind its back must not make
    a fresh download look like a duplicate — which stranded it in
    `downloads/complete` with `file_moved=1` and an empty `target_dir`.
    """
    abort = run_scenario(u8_stale_beets_row, scenario_ctx, track)
    _assert_recorded(scenario_ctx, "U8_stale_beets_row", abort)


# ---------------------------------------------------------------------------
# U9-U10 — playlist lifecycle and concurrency
# ---------------------------------------------------------------------------


def test_u9_playlist_lifecycle(scenario_ctx: ScenarioContext) -> None:
    """A playlist deleted in Navidrome comes back only when there is
    something to put in it — the user's stated desired behaviour."""
    abort = run_scenario(u9_playlist_lifecycle, scenario_ctx)
    _assert_recorded(scenario_ctx, "U9_playlist_lifecycle", abort)


def test_u10_concurrent_downloads(scenario_ctx: ScenarioContext) -> None:
    """Several downloads at once, each landing in its own correct place.

    Concurrency is where the old `_move_file()` globbing defect used to bite,
    so this is the journey that would catch a regression of that shape.
    """
    abort = run_scenario(u10_concurrent_downloads, scenario_ctx, concurrent_track_set())
    _assert_recorded(scenario_ctx, "U10_concurrent", abort)
