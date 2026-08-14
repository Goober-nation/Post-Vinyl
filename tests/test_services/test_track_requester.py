"""
Tests for the shared search-and-queue driver (app/services/track_requester.py).

Covers `artist_words`, `is_viable_candidate`, and `run_ladder` — the ladder
loop extracted from RecPuller and shared with the MusicBrainz resolve job.
Uses a scripted fake SearchService; no DB or network.
"""

from datetime import datetime, timezone

import pytest

from app.exceptions import SlskdConnectionError
from app.services import track_requester
from app.services.interfaces.search import SearchJob, SearchResult


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _make_config(threshold=0.6, min_words=1):
    class SearchCfg:
        pass

    search = SearchCfg()
    search.pass_ratio_threshold = threshold
    search.artist_match_min_words = min_words

    class Cfg:
        pass

    cfg = Cfg()
    cfg.search = search
    return cfg


def _result(username, filename, size=1000, free_slot=True):
    return SearchResult(
        username=username,
        filename=filename,
        size=size,
        has_free_slot=free_slot,
        upload_speed=None,
        bitrate="320",
        duration=180,
    )


class FakeSearchService:
    """Scripted SearchService: results (and optional raised errors) per query."""

    def __init__(self, results_by_query=None, raise_on_query=None):
        self._results_by_query = results_by_query or {}
        self._raise_on_query = raise_on_query or {}
        self.search_queries: list[str] = []
        self._next_id = 1
        self._query_by_search_id: dict[str, str] = {}

    def search(self, query, artist=None):
        self.search_queries.append(query)
        if query in self._raise_on_query:
            raise self._raise_on_query[query]
        sid = f"search-{self._next_id}"
        self._next_id += 1
        self._query_by_search_id[sid] = query
        return SearchJob(
            search_id=sid,
            query=query,
            artist=artist,
            created_at=datetime.now(timezone.utc),
            status="searching",
        )

    def get_results(self, search_id):
        query = self._query_by_search_id[search_id]
        return list(self._results_by_query.get(query, []))


# ---------------------------------------------------------------------------
# artist_words
# ---------------------------------------------------------------------------


class TestArtistWords:
    def test_feat_clause_truncated(self):
        assert track_requester.artist_words("Alesso feat. Katy Perry") == ["alesso"]

    def test_stop_words_dropped(self):
        words = track_requester.artist_words("The Beatles")
        assert "beatles" in words
        assert "the" not in words

    def test_accents_folded(self):
        assert track_requester.artist_words("Björk") == ["bjork"]

    def test_stop_word_and_single_char_name_yields_empty(self):
        # "artist" is a stop word, "z" is one char — no filter words remain,
        # which downstream disables the artist-containment check.
        assert track_requester.artist_words("Artist Z") == []

    def test_digits_dropped(self):
        assert track_requester.artist_words("123") == []

    def test_alphanumeric_token_kept(self):
        assert track_requester.artist_words("A2") == ["a2"]


# ---------------------------------------------------------------------------
# is_viable_candidate
# ---------------------------------------------------------------------------


class TestIsViableCandidate:
    def test_viable_when_artist_word_present_and_audio_extension(self):
        result = _result("peer1", "Queen - Bohemian Rhapsody.mp3")
        assert track_requester.is_viable_candidate(result, ["queen"]) is True

    def test_rejects_when_artist_word_absent(self):
        result = _result("peer1", "unrelated - song.mp3")
        assert track_requester.is_viable_candidate(result, ["zebra"]) is False

    def test_empty_artist_words_disables_artist_filter(self):
        result = _result("peer1", "any old file.flac")
        assert track_requester.is_viable_candidate(result, []) is True

    def test_rejects_remix_qualifier(self):
        result = _result("peer1", "Track (Remix).mp3")
        assert track_requester.is_viable_candidate(result, ["track"]) is False

    def test_rejects_multiword_remix_qualifier_substring(self):
        result = _result("peer1", "Track [Sped Up].mp3")
        assert track_requester.is_viable_candidate(result, ["track"]) is False

    def test_rejects_non_audio_extension(self):
        result = _result("peer1", "Track - Song.txt")
        assert track_requester.is_viable_candidate(result, ["track"]) is False

    def test_min_words_requires_two_artist_words(self):
        # Both words present -> viable under min_words=2.
        both = _result("peer1", "John Doe - Sunrise.mp3")
        assert track_requester.is_viable_candidate(both, ["john", "doe"], min_words=2) is True
        # Only one of two words -> rejected under min_words=2.
        one = _result("peer1", "John - Sunrise.mp3")
        assert track_requester.is_viable_candidate(one, ["john", "doe"], min_words=2) is False

    def test_min_words_capped_at_artist_word_count(self):
        # A single-word artist can never satisfy min_words=2 — the filter
        # degrades to "any word" instead of demanding a word that doesn't exist.
        result = _result("peer1", "Björk - Jóga.mp3")
        assert track_requester.is_viable_candidate(result, ["bjork"], min_words=2) is True

    def test_default_min_words_is_one(self):
        # Back-compat: without an explicit min_words the old "any word" rule
        # still applies.
        one = _result("peer1", "John - Sunrise.mp3")
        assert track_requester.is_viable_candidate(one, ["john", "doe"]) is True


# ---------------------------------------------------------------------------
# run_ladder
# ---------------------------------------------------------------------------

TRACK = "Sunrise Sunset"
ARTIST = "John Doe"
RUNGS = ["sunrise john", "sunset john", "sunrise doe", "sunset doe"]


class TestRunLadder:
    def test_ladder_walks_rungs_in_order_when_all_miss(self):
        svc = FakeSearchService(results_by_query={q: [] for q in RUNGS})
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(), TRACK, ARTIST
        )

        assert svc.search_queries == RUNGS
        assert best_job is not None  # last rung's job (all empty)
        assert best_filtered == []
        assert search_error is None

    def test_threshold_early_stop(self):
        svc = FakeSearchService(
            results_by_query={
                "sunrise john": [
                    _result("peer1", "John Doe - Sunrise Sunset.mp3"),
                ]
            }
        )
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(threshold=0.6), TRACK, ARTIST
        )

        # 1/1 viable = ratio 1.0 >= 0.6 -> stop after rung 0.
        assert svc.search_queries == ["sunrise john"]
        assert best_job is not None
        assert best_job.search_id == "search-1"
        assert len(best_filtered) == 1
        assert search_error is None

    def test_best_ratio_fallback_when_no_rung_clears_threshold(self):
        svc = FakeSearchService(
            results_by_query={
                "sunrise john": [
                    _result("p1", "John Doe - Sunrise Sunset.mp3"),
                    _result("p2", "unrelated track.mp3"),
                ],
                "sunset john": [_result("p3", "unrelated track.mp3")],
                "sunrise doe": [_result("p4", "unrelated track.mp3")],
                "sunset doe": [_result("p5", "unrelated track.mp3")],
            }
        )
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(threshold=0.6), TRACK, ARTIST
        )

        # Rung 0 had the only viable result (ratio 0.5, below threshold) —
        # the ladder ran to the end and fell back to it.
        assert svc.search_queries == RUNGS
        assert best_job is not None
        assert best_job.search_id == "search-1"
        assert [r.filename for r in best_filtered] == ["John Doe - Sunrise Sunset.mp3"]
        assert search_error is None

    def test_rung_zero_error_propagates(self):
        svc = FakeSearchService(
            raise_on_query={
                "sunrise john": SlskdConnectionError("http://slskd:5030", "boom")
            }
        )
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(), TRACK, ARTIST
        )

        assert best_job is None
        assert best_filtered == []
        assert search_error is not None
        assert "Cannot connect" in search_error

    def test_later_rung_error_falls_back_to_best_seen(self):
        svc = FakeSearchService(
            results_by_query={
                "sunrise john": [
                    _result("p1", "John Doe - Sunrise Sunset.mp3"),
                    _result("p2", "unrelated track.mp3"),
                ],
            },
            raise_on_query={
                "sunset john": SlskdConnectionError("http://slskd:5030", "boom"),
            },
        )
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(threshold=0.6), TRACK, ARTIST
        )

        # Rung 0 (ratio 0.5) ran, rung 1 raised -> fall back to rung 0's
        # results; the rung-1 error is logged, not returned.
        assert svc.search_queries == ["sunrise john", "sunset john"]
        assert best_job is not None
        assert best_job.search_id == "search-1"
        assert [r.filename for r in best_filtered] == ["John Doe - Sunrise Sunset.mp3"]
        assert search_error is None

    def test_artist_match_min_words_from_config_filters_candidates(self):
        # min_words=2: a candidate carrying only one artist word ("John") is
        # dropped, so rung 0's single "John - ..." result is filtered out and
        # the ladder falls through to a rung whose results satisfy it.
        svc = FakeSearchService(
            results_by_query={
                "sunrise john": [_result("p1", "John - Sunrise Sunset.mp3")],
                "sunset john": [
                    _result("p2", "John Doe - Sunrise Sunset.mp3"),
                    _result("p3", "unrelated track.mp3"),
                ],
            }
        )
        best_job, best_filtered, search_error = track_requester.run_ladder(
            svc, _make_config(threshold=0.6, min_words=2), TRACK, ARTIST
        )

        # Rung 0 ratio 0 (0/1 viable) -> advance; rung 1 ratio 0.5 -> below
        # threshold, but it is the best-ratio rung and is returned.
        assert svc.search_queries == RUNGS
        assert best_job is not None
        assert best_job.search_id == "search-2"
        assert [r.filename for r in best_filtered] == ["John Doe - Sunrise Sunset.mp3"]
        assert search_error is None
