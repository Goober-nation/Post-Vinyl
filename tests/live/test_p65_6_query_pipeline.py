"""
P6.5-6 — search query construction against real slskd.

The word-cap claim can only be checked by reading musica's own logs: the REST
API never echoes back the string it sent to slskd. `LogScraper.ladder_attempts`
parses the `query '...' -> N results, M viable (pass ratio X)` lines.

What is and isn't assertable here matters. The *mechanism* is deterministic and
gets real assertions — word counts, rung ordering, early-stop on threshold.
The *outcomes* are not: which peers answer a given query depends on who is
online right now, so "this query returns results" is a datapoint to collect
across repeated runs (see `collect_pass_ratios.py`), never a pass/fail gate.
Asserting on it would produce a suite that fails for reasons that have nothing
to do with the code.
"""

from __future__ import annotations

import itertools

import pytest

# Titles chosen to exercise the pipeline's branches, not for popularity:
# multi-word, feat clause, paren qualifier, paren cruft, non-ASCII.
PIPELINE_CASES = [
    ("Smells Like Teen Spirit", "Nirvana"),
    ("Heroes (We Could Be)", "Alesso feat. Tove Lo"),
    ("Blinding Lights (Remix)", "The Weeknd"),
    ("Bohemian Rhapsody (Official Video)", "Queen"),
]


def _recs_enabled(stack) -> bool:
    status = stack.client.recs_status()
    return status["listenbrainz_enabled"] and any(
        status[f"{c}_enabled"] for c in ("comfort_zone", "fresh_picks", "deep_cuts")
    )


class TestWordCap:
    """P-MB-4's finding is the whole reason this pipeline exists: 3+-word
    combined queries returned zero, consistently."""

    def test_no_query_exceeds_the_cap(self, stack, since_now):
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled — the ladder only runs on pulls")

        pull = stack.client.pull_recs()
        if pull.status == 409:
            pytest.skip("a rec pull was already running")

        stack.events.wait_for(
            lambda e: e.type == "rec.pull_completed",
            timeout=900,
            description="rec.pull_completed",
        )
        attempts = stack.logs.ladder_attempts(since=since_now())
        if not attempts:
            pytest.skip("pull queued nothing — no ladder attempts to inspect")

        stack.marker("ladder_attempts", count=len(attempts))
        over_cap = [a for a in attempts if a.word_count > 3]
        assert not over_cap, (
            f"queries exceeded the 3-word ceiling: {[a.query for a in over_cap]}"
        )

        # 3 words is only legal for the paren-qualifier rung; everything else
        # must be 1-2. We can't tell from the log line alone which rung a
        # query was, so this is reported rather than asserted.
        three_word = [a.query for a in attempts if a.word_count == 3]
        stack.marker("three_word_queries", queries=three_word)
        print(
            f"\n[live] 3-word queries (should all be paren-qualifier rungs): {three_word}"
        )

    def test_raw_multiword_titles_are_never_sent(self, stack, since_now):
        """Regression for review finding #3 — the empty fallback used to emit
        the untouched title whenever the artist yielded no usable word."""
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled")

        attempts = stack.logs.ladder_attempts(since="30m")
        if not attempts:
            pytest.skip("no recent ladder attempts in the logs")

        # Raw titles keep their original capitalization; pipeline output is
        # always lowercased. A capitalized multi-word query is the tell.
        raw_looking = [
            a.query for a in attempts if a.word_count > 2 and a.query != a.query.lower()
        ]
        assert not raw_looking, (
            f"raw, unprocessed titles were sent to slskd: {raw_looking}"
        )


class TestLadderBehavior:
    def test_ladder_stops_early_once_threshold_is_cleared(self, stack, since_now):
        """A rung clearing the pass ratio must end the walk — otherwise every
        track costs up to 5 full slskd search cycles."""
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled")

        threshold = float(
            stack.client.get_config().get("search", {}).get("pass_ratio_threshold", 0.6)
        )
        attempts = stack.logs.ladder_attempts(since="30m")
        if not attempts:
            pytest.skip("no recent ladder attempts in the logs")

        # Walk the sequence: nothing may follow a rung that cleared, until
        # the next track's ladder starts. We can't segment tracks perfectly
        # from the log alone, so this checks the weaker but still meaningful
        # property: a clearing rung is never immediately followed by another.
        for prev, nxt in itertools.pairwise(attempts):
            if prev.ratio >= threshold:
                assert prev.query.split()[0] != nxt.query.split()[0], (
                    f"ladder continued past a clearing rung: {prev} -> {nxt}"
                )

    def test_pull_wall_time_is_recorded(self, stack):
        """Not a pass/fail — the ladder can cost up to 5 searches per track,
        and this is the number that says whether that's tolerable."""
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled")

        pull = stack.client.pull_recs()
        if pull.status == 409:
            pytest.skip("a rec pull was already running")

        start = stack.timeline.elapsed()
        completed = stack.events.wait_for(
            lambda e: e.type == "rec.pull_completed",
            timeout=1800,
            description="rec.pull_completed",
        )
        duration = stack.timeline.elapsed() - start
        attempts = stack.logs.ladder_attempts(since=f"{int(duration) + 10}s")

        stack.marker(
            "pull_timing",
            seconds=round(duration, 1),
            searches=len(attempts),
            queued=completed.data.get("queued"),
            to_download=completed.data.get("to_download"),
        )
        print(
            f"\n[live] pull took {duration:.0f}s, "
            f"{len(attempts)} slskd searches, "
            f"queued {completed.data.get('queued')}"
        )


class TestManualSearchPath:
    """Manual searches don't go through the ladder — they send the query
    verbatim. Worth pinning so a future refactor doesn't silently change it."""

    @pytest.mark.parametrize("track,artist", PIPELINE_CASES)
    def test_manual_search_sends_the_query_as_given(self, stack, track, artist):
        job = stack.client.search(track, artist=artist)
        assert job["query"] == track
        detail = stack.client.search_detail(job["search_id"])
        stack.marker(
            "manual_search",
            track=track,
            artist=artist,
            results=len(detail["results"]),
        )
        print(f"\n[live] {track!r} + {artist!r} -> {len(detail['results'])} results")
