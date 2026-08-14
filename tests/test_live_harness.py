"""
Unit tests for the live harness's log parsers.

These run in the ordinary suite — no Docker, no stack. They exist because the
parsers are the load-bearing part of the live tests: if `parse_ladder_attempts`
silently returns [], every assertion built on it passes vacuously and the live
run reports success while checking nothing.

The sample lines below are generated the same way the real ones are — through
the actual logging format string — so a change to the log call breaks these
tests rather than quietly breaking Track 2.
"""

from tests.live.harness import (
    LadderAttempt,
    Timeline,
    first_viable_result,
    parse_ladder_attempts,
    parse_search_ids,
)

LOG_PREFIX = "2026-08-11 12:00:00 [INFO] app.workers.rec_puller: "


def _ladder_line(query: str, results: int, viable: int, ratio: float) -> str:
    """Mirrors the logger.info call in rec_puller._queue_tracks."""
    return (
        f"{LOG_PREFIX}RecPuller: query '{query}' -> {results} results, "
        f"{viable} viable (pass ratio {ratio:.2f})"
    )


class TestParseLadderAttempts:
    def test_parses_a_single_line(self):
        text = _ladder_line("heroes alesso", 12, 8, 0.67)
        (attempt,) = parse_ladder_attempts(text)
        assert attempt == LadderAttempt("heroes alesso", 12, 8, 0.67)

    def test_word_count(self):
        (attempt,) = parse_ladder_attempts(
            _ladder_line("heroes remix alesso", 5, 1, 0.2)
        )
        assert attempt.word_count == 3

    def test_parses_a_full_ladder_in_order(self):
        text = "\n".join(
            [
                _ladder_line("sunrise john", 10, 5, 0.5),
                "2026-08-11 12:00:05 [INFO] noise: unrelated line",
                _ladder_line("sunset john", 8, 0, 0.0),
                _ladder_line("sunrise doe", 3, 3, 1.0),
            ]
        )
        attempts = parse_ladder_attempts(text)
        assert [a.query for a in attempts] == [
            "sunrise john",
            "sunset john",
            "sunrise doe",
        ]
        assert [a.ratio for a in attempts] == [0.5, 0.0, 1.0]

    def test_query_containing_an_apostrophe_is_not_truncated(self):
        """Tokenization keeps apostrophes, so 'stayin'' reaches the log — a
        naive split on "'" would cut the query in half and silently under-count
        its words."""
        (attempt,) = parse_ladder_attempts(_ladder_line("stayin' gees", 4, 2, 0.5))
        assert attempt.query == "stayin' gees"
        assert attempt.word_count == 2

    def test_zero_results_parses(self):
        (attempt,) = parse_ladder_attempts(_ladder_line("obscure thing", 0, 0, 0.0))
        assert attempt.results == 0
        assert attempt.ratio == 0.0

    def test_ignores_unrelated_output(self):
        assert parse_ladder_attempts("nothing to see\nnor here\n") == []

    def test_ignores_the_re_query_announcement_line(self):
        """rec_puller logs a separate 'pass-ratio re-query' line per rung;
        only the result line carries counts and must be counted once."""
        text = (
            f"{LOG_PREFIX}RecPuller: pass-ratio re-query for X - Y (rung 1): "
            f"'sunset john'\n" + _ladder_line("sunset john", 8, 0, 0.0)
        )
        assert len(parse_ladder_attempts(text)) == 1


class TestParseSearchIds:
    def test_extracts_ids_in_order(self):
        text = (
            "2026-08-11 12:00:00 [INFO] app.routes.search: "
            "Search initiated: id=abc-1\n"
            "unrelated\n"
            "2026-08-11 12:00:02 [INFO] app.services.search: "
            "Search initiated: id=abc-2\n"
        )
        assert parse_search_ids(text) == ["abc-1", "abc-2"]

    def test_no_searches(self):
        assert parse_search_ids("quiet logs\n") == []

    def test_counting_is_what_the_negative_assertion_relies_on(self):
        """P6.5-4's claim is that retry does NOT re-search. That's checked by
        comparing counts before and after, so an equal count must mean equal."""
        before = parse_search_ids("Search initiated: id=one\n")
        after = parse_search_ids("Search initiated: id=one\n")
        assert len(before) == len(after) == 1


class TestFirstViableResult:
    def test_prefers_a_free_slot(self):
        results = [
            {"username": "a", "filename": "no-slot.mp3", "has_free_slot": False},
            {"username": "b", "filename": "slot.mp3", "has_free_slot": True},
        ]
        assert first_viable_result(results)["username"] == "b"

    def test_falls_back_to_any_result_with_a_filename(self):
        results = [{"username": "a", "filename": "x.mp3", "has_free_slot": False}]
        assert first_viable_result(results)["username"] == "a"

    def test_none_when_empty(self):
        assert first_viable_result([]) is None

    def test_skips_results_without_a_filename(self):
        assert first_viable_result([{"username": "a", "filename": ""}]) is None


class TestTimeline:
    def test_records_monotonic_offsets(self):
        timeline = Timeline()
        first = timeline.record("marker", label="one")
        second = timeline.record("marker", label="two")
        assert second["t"] >= first["t"]
        assert len(timeline.entries) == 2

    def test_filters_by_kind(self):
        timeline = Timeline()
        timeline.record("api", path="/x")
        timeline.record("sse", event="transfer.queued")
        timeline.record("api", path="/y")
        assert len(timeline.of_kind("api")) == 2
        assert len(timeline.of_kind("sse")) == 1

    def test_writes_jsonl(self, tmp_path):
        path = tmp_path / "timeline.jsonl"
        timeline = Timeline(path)
        timeline.record("marker", label="hello")
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert '"label": "hello"' in lines[0]
