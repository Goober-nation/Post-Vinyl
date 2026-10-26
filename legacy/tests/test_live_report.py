"""
Unit tests for the live-suite report aggregation.

These run without the stack, on synthetic scorecard rows. That matters more
here than almost anywhere else in the project: `report.py` is what turns a
run into the numbers the user makes decisions from, and aggregation logic
that silently miscounts would produce a confident, wrong report. There is
precedent for this concern — see `tests/test_live_harness.py`, which exists
because a parser returning `[]` would have made every live assertion vacuous.

The distinction under test throughout is the one the whole exercise rests on:
**never-reached is not the same as failed, and neither is a pass.**
"""

from __future__ import annotations

from tests.live import report
from tests.live.probes.contract import Stage, Verdict
from tests.live.scenarios import (
    FAIL_EXTERNAL,
    SKIP_BUDGET,
    SKIP_NOT_APPLICABLE,
    SKIP_PRECONDITION,
)


def row(
    stage: Stage,
    verdict: Verdict,
    *,
    detail: str = "",
    scenario: str = "U1_manual_pipeline",
    run_id: str = "rep1",
    track: str | None = "Kendrick Lamar - Alright",
    tier: str | None = "popular",
    latency_s: float | None = None,
) -> dict:
    return {
        "stage": stage.value,
        "verdict": verdict.value,
        "detail": detail,
        "scenario": scenario,
        "run_id": run_id,
        "track": track,
        "tier": tier,
        "latency_s": latency_s,
        "evidence": {},
    }


class TestClassify:
    def test_pass_and_plain_fail(self):
        assert report.classify(row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS)) == "pass"
        assert report.classify(row(Stage.S5_TRANSFER_COMPLETED, Verdict.FAIL)) == "fail"

    def test_external_failure_is_not_blamed_on_musica(self):
        """An empty Soulseek is not a defect in this repo, and mixing the two
        would put unfixable items at the top of the defect list."""
        outcome = report.classify(
            row(
                Stage.S3_RESULTS_RELEVANT,
                Verdict.FAIL,
                detail=f"{FAIL_EXTERNAL}no peer had the track",
            )
        )
        assert outcome == "fail_external"

    def test_skip_reasons_are_kept_distinct(self):
        cases = {
            SKIP_NOT_APPLICABLE: "skip_not_applicable",
            SKIP_BUDGET: "skip_budget",
            SKIP_PRECONDITION: "skip_precondition",
            "": "skip_downstream",
        }
        for prefix, expected in cases.items():
            got = report.classify(
                row(Stage.S12_PLAYLIST_CORRECT, Verdict.SKIP, detail=f"{prefix}because")
            )
            assert got == expected, f"{prefix!r} classified as {got}"

    def test_harness_error_is_its_own_thing(self):
        assert (
            report.classify(row(Stage.S7_BEETS_IMPORT, Verdict.ERROR))
            == "harness_error"
        )


class TestStageStats:
    def test_never_reached_stage_has_no_pass_ratio(self):
        """The critical case: a stage nobody reached must report `None`, not
        0%. Printing 0% would accuse a stage that was never tested."""
        stats = report.stage_stats(
            [row(Stage.S11_NAVIDROME_INDEXED, Verdict.SKIP, detail="")]
        )
        st = stats[Stage.S11_NAVIDROME_INDEXED.value]
        assert st.measured == 0
        assert st.pass_ratio is None
        assert st.skipped_downstream == 1

    def test_pass_ratio_counts_only_measured_rows(self):
        rows = [
            row(Stage.S5_TRANSFER_COMPLETED, Verdict.PASS),
            row(Stage.S5_TRANSFER_COMPLETED, Verdict.FAIL),
            row(Stage.S5_TRANSFER_COMPLETED, Verdict.SKIP, detail=""),
            row(Stage.S5_TRANSFER_COMPLETED, Verdict.SKIP, detail=SKIP_BUDGET + "out"),
        ]
        st = report.stage_stats(rows)[Stage.S5_TRANSFER_COMPLETED.value]
        assert st.measured == 2
        assert st.pass_ratio == 0.5
        assert st.skipped_budget == 1

    def test_blame_ratio_excludes_network_failures(self):
        rows = [
            row(Stage.S3_RESULTS_RELEVANT, Verdict.PASS),
            row(
                Stage.S3_RESULTS_RELEVANT,
                Verdict.FAIL,
                detail=f"{FAIL_EXTERNAL}nobody online",
            ),
        ]
        st = report.stage_stats(rows)[Stage.S3_RESULTS_RELEVANT.value]
        assert st.pass_ratio == 0.5  # of everything measured
        assert st.blame_ratio == 1.0  # of what musica could control

    def test_latencies_only_from_measured_rows(self):
        rows = [
            row(Stage.S2_SEARCH_COMPLETED, Verdict.PASS, latency_s=10.0),
            row(Stage.S2_SEARCH_COMPLETED, Verdict.PASS, latency_s=30.0),
            row(Stage.S2_SEARCH_COMPLETED, Verdict.SKIP, detail="", latency_s=999.0),
        ]
        st = report.stage_stats(rows)[Stage.S2_SEARCH_COMPLETED.value]
        assert st.latencies == [10.0, 30.0]
        assert st.latency(0.5) == 10.0
        assert st.to_dict()["latency_max"] == 30.0


class TestFunnel:
    def test_not_applicable_stages_are_excluded_from_the_denominator(self):
        """U9 not doing a search is not U9 failing a search."""
        rows = [
            row(
                Stage.S1_SEARCH_ACCEPTED,
                Verdict.SKIP,
                detail=f"{SKIP_NOT_APPLICABLE}U9 does not search",
                scenario="U9_playlist_lifecycle",
                track=None,
                tier=None,
            ),
            row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS),
        ]
        step = next(
            s
            for s in report.funnel(rows)
            if s["stage"] == Stage.S1_SEARCH_ACCEPTED.value
        )
        assert step["journeys_applicable"] == 1
        assert step["journeys_reached"] == 1
        assert step["reach_ratio"] == 1.0

    def test_journeys_are_counted_once_per_track_and_run(self):
        rows = [
            row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS, track="A", run_id="rep1"),
            row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS, track="A", run_id="rep2"),
            row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS, track="B", run_id="rep1"),
        ]
        step = next(
            s
            for s in report.funnel(rows)
            if s["stage"] == Stage.S1_SEARCH_ACCEPTED.value
        )
        assert step["journeys_reached"] == 3


class TestDefects:
    def test_rare_tier_failures_outrank_more_frequent_popular_ones(self):
        """The user's explicit weighting: rare > awkward > popular."""
        rows = [
            row(
                Stage.S9_PLACEMENT_CORRECT,
                Verdict.FAIL,
                detail="feat clause in albumartist",
                tier="popular",
                track="P",
            ),
            row(
                Stage.S9_PLACEMENT_CORRECT,
                Verdict.FAIL,
                detail="feat clause in albumartist",
                tier="popular",
                track="P2",
            ),
            row(
                Stage.S8_TAGS_CORRECT,
                Verdict.FAIL,
                detail="album is the peer folder name",
                tier="rare",
                track="R",
            ),
        ]
        ranked = report.defects(rows)
        # popular x2 = 2.0, rare x1 = 3.0 -> rare first despite being rarer
        assert ranked[0]["stage"] == Stage.S8_TAGS_CORRECT.value
        assert ranked[0]["score"] == 3.0
        assert ranked[1]["occurrences"] == 2

    def test_external_defects_sort_below_everything_musica_owns(self):
        rows = [
            row(
                Stage.S3_RESULTS_RELEVANT,
                Verdict.FAIL,
                detail=f"{FAIL_EXTERNAL}nobody had it",
                tier="rare",
            ),
            row(
                Stage.S9_PLACEMENT_CORRECT,
                Verdict.FAIL,
                detail="bad folder",
                tier="popular",
            ),
        ]
        ranked = report.defects(rows)
        assert ranked[0]["external"] is False
        assert ranked[-1]["external"] is True

    def test_identical_failures_are_grouped_not_repeated(self):
        rows = [
            row(
                Stage.S9_PLACEMENT_CORRECT,
                Verdict.FAIL,
                detail="bad folder",
                track=f"T{i}",
            )
            for i in range(5)
        ]
        ranked = report.defects(rows)
        assert len(ranked) == 1
        assert ranked[0]["occurrences"] == 5
        assert len(ranked[0]["examples"]) == 3  # capped, not unbounded


class TestScenarioCoverage:
    def test_a_scenario_that_never_ran_is_reported_as_such(self):
        """Silence must not read as success — this is the most dangerous
        failure mode a report of this kind has."""
        coverage = report.scenario_coverage(
            [row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS)]
        )
        by_id = {c["id"]: c for c in coverage}
        assert by_id["U1_manual_pipeline"]["ran"] is True
        assert by_id["U7_crash_recovery"]["ran"] is False
        assert by_id["U7_crash_recovery"]["journeys"] == 0


class TestRendering:
    def test_summary_and_markdown_survive_an_empty_run(self):
        summary = report.build_summary([])
        assert summary["totals"]["graded_stages"] == 0
        text = report.render_markdown(summary)
        assert "NEVER RAN" in text  # every registered scenario is flagged

    def test_markdown_reports_all_thirteen_stages(self):
        summary = report.build_summary([row(Stage.S1_SEARCH_ACCEPTED, Verdict.PASS)])
        text = report.render_markdown(summary)
        for stage in Stage:
            assert stage.value[:3] in text

    def test_never_reached_renders_as_dash_not_zero_percent(self):
        summary = report.build_summary(
            [row(Stage.S13_USER_CAN_FIND, Verdict.SKIP, detail="")]
        )
        text = report.render_markdown(summary)
        s13 = next(ln for ln in text.splitlines() if "user can find it" in ln)
        assert "—" in s13
        assert "0%" not in s13
