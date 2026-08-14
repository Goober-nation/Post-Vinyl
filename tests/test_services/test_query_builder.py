"""
Unit tests for the search query pipeline (P6.5-6).

Covers feat-clause truncation, parenthetical handling, word selection,
the re-query ladder, exclusion-list behavior, and the empty fallback.
"""

from app.services.query_builder import (
    REMIX_QUALIFIERS,
    STOP_WORDS,
    build_search_queries,
    paren_qualifiers,
    select_words,
    strip_feat,
)

# Live-confirmed 2026-08-12: the tokenizer treated accented letters and
# periods as hard separators, so an accented or dotted-acronym word could
# shatter into fragments too short to survive the length filter and vanish
# from the query entirely.


class TestStripFeat:
    def test_truncates_at_feat(self):
        assert strip_feat("Alesso feat. Katy Perry") == "Alesso "

    def test_truncates_at_ft(self):
        assert strip_feat("Artist ft. Other") == "Artist "

    def test_truncates_at_featuring(self):
        assert strip_feat("A feat featuring B") == "A "

    def test_no_feat_unchanged(self):
        assert strip_feat("Queen - Bohemian Rhapsody") == "Queen - Bohemian Rhapsody"

    def test_with_and_vs_not_truncated(self):
        """with/x/vs are deliberately NOT feat delimiters (legitimate titles)."""
        assert strip_feat("A with B") == "A with B"
        assert strip_feat("A vs B") == "A vs B"
        assert strip_feat("A x B") == "A x B"

    def test_case_insensitive(self):
        assert strip_feat("Alesso FEAT. Katy Perry") == "Alesso "


class TestSelectWords:
    def test_picks_longest_first(self):
        assert select_words("Strawberry Fields Forever") == [
            "strawberry",
            "forever",
            "fields",
        ]

    def test_drops_stopwords(self):
        words = select_words("The End of the World")
        assert "the" not in words
        assert "of" not in words
        assert "world" in words

    def test_drops_remix_qualifiers(self):
        assert "remix" not in select_words("Song (Remix)")
        assert "live" not in select_words("Song Live")

    def test_paren_contents_excluded(self):
        """Cruft like (Official Video) must never win the pick."""
        assert select_words("Sunrise (Official Video)") == ["sunrise"]
        assert select_words("Heroes (We Could Be)") == ["heroes"]

    def test_paren_contents_excluded_from_selection_even_with_qualifier(self):
        """The qualifier is carried separately (paren_qualifiers), not in
        the selection words."""
        assert select_words("Heroes (We Could Be) (Remix)") == ["heroes"]

    def test_feat_clause_truncated_before_selection(self):
        """The featured artist must not win the longest-word pick."""
        assert select_words("Alesso feat. Katy Perry") == ["alesso"]

    def test_drops_short_and_digit_only_tokens(self):
        assert select_words("Mr A B 2001") == []

    def test_new_exclusion_words_dropped_from_selection(self):
        for word in ("acapella", "karaoke", "mashup", "bootleg", "tribute", "loop"):
            assert word not in select_words(f"Song ({word})")
            assert word in REMIX_QUALIFIERS

    def test_existing_stopwords_present(self):
        assert "soundtrack" in STOP_WORDS
        assert "ost" in STOP_WORDS
        assert "score" in STOP_WORDS


class TestAccentAndDotFolding:
    """The 2026-08-12 fix. Both were previously live-confirmed to make a
    query word vanish entirely, not just come out slightly wrong."""

    def test_accented_word_no_longer_vanishes(self):
        """Before the fix: 'björk' split into 'bj'/'rk' on the umlaut, both
        <=2 chars, both dropped — select_words('Björk') returned []."""
        assert select_words("Björk") == ["bjork"]
        assert select_words("Jóga") == ["joga"]

    def test_dotted_acronym_no_longer_vanishes(self):
        """Before the fix: 'p.o.v.' split into 'p'/'o'/'v' at every period,
        all 1 char, all dropped — select_words('P.O.V.') returned []."""
        assert select_words("P.O.V.") == ["pov"]
        assert select_words("R.E.M.") == ["rem"]

    def test_clipse_pov_no_longer_drops_the_title(self):
        """The exact live failure: build_search_queries('P.O.V.', 'Clipse')
        used to return ['clipse'] — the title contributed zero words, so the
        empty-fallback rule silently searched for the artist alone."""
        queries = build_search_queries("P.O.V.", "Clipse")
        assert queries[0] == "pov clipse"
        assert "clipse" != queries[0]

    def test_multi_word_accented_artist_keeps_both_words(self):
        """'Sigur Rós' used to yield only ['sigur'] ('rós' shattered into
        'r'/'s', both dropped) — now both survive as real words."""
        assert select_words("Sigur Rós") == ["sigur", "ros"]

    def test_ordinary_sentence_periods_are_unaffected(self):
        """A period followed by a space is not a dotted acronym — dropping
        the period must not accidentally fuse two real words together."""
        assert select_words("Mr. Fantastic") == ["fantastic"]

    def test_non_accented_text_is_unchanged(self):
        assert select_words("Strawberry Fields Forever") == [
            "strawberry",
            "forever",
            "fields",
        ]


class TestParenQualifiers:
    def test_detects_remix(self):
        assert paren_qualifiers("Heroes (We Could Be) (Remix)") == ["remix"]

    def test_detects_cover(self):
        assert paren_qualifiers("Song (Cover)") == ["cover"]

    def test_multi_word_qualifier_in_parens(self):
        assert paren_qualifiers("Song (8D Audio)") == ["8d audio"]

    def test_no_qualifier_returns_empty(self):
        assert paren_qualifiers("Heroes (We Could Be)") == []
        assert paren_qualifiers("Song (Official Video)") == []

    def test_empty_input(self):
        assert paren_qualifiers("") == []


class TestBuildSearchQueries:
    def test_two_word_base_query(self):
        assert build_search_queries("Heroes (We Could Be)", "Alesso") == [
            "heroes alesso"
        ]

    def test_qualifier_rung_first_then_dropped(self):
        assert build_search_queries("Heroes (We Could Be) (Remix)", "Alesso") == [
            "heroes remix alesso",
            "heroes alesso",
        ]

    def test_feat_truncation_on_both_fields(self):
        queries = build_search_queries(
            "Heroes (Official Video)", "Alesso feat. Katy Perry"
        )
        assert queries == ["heroes alesso"]
        assert "katy" not in queries[0]

    def test_ladder_order_two_words_each(self):
        assert build_search_queries("Sunrise Sunset", "John Doe") == [
            "sunrise john",
            "sunset john",
            "sunrise doe",
            "sunset doe",
        ]

    def test_ladder_skips_missing_words(self):
        """Artist with one word: only 1-1 and 2-1 rungs exist."""
        assert build_search_queries("Sunrise Sunset", "Alesso") == [
            "sunrise alesso",
            "sunset alesso",
        ]

    def test_empty_fallback_uses_raw_track(self):
        assert build_search_queries("Track X", "Artist Y") == ["Track X"]

    def test_single_word_rungs_when_track_empty(self):
        """One empty field skips every 2-word rung, so the ladder degrades
        to the other field's single words — not to the raw string, which
        isn't word-capped."""
        assert build_search_queries("", "Some Artist") == ["some"]

    def test_single_word_rungs_when_artist_unusable(self):
        """Artist yields no usable word (<=2 chars or all stop words). The
        old behavior here was the raw multi-word title, which is exactly
        the shape P-MB-4 proved returns zero results."""
        assert build_search_queries("The Sound of Silence", "U2") == [
            "silence",
            "sound",
        ]

    def test_accented_artist_is_no_longer_unusable(self):
        """Before the 2026-08-12 accent-folding fix, 'Björk' tokenized to
        nothing and this fell into the single-word-rung fallback, producing
        the artist-less query 'army'. Folded to 'bjork' it is a normal
        artist word, and the real 2-word rung is used instead — strictly
        more specific, not merely different."""
        assert build_search_queries("Army of Me", "Björk") == ["army bjork"]

    def test_qualifier_only_query_when_artist_empty(self):
        """No artist words — the qualifier rung becomes 2 words, and the
        pair-ladder has no rungs left to drop down to."""
        assert build_search_queries("Heroes (Remix)", "") == ["heroes remix"]

    def test_dedupes_repeated_rungs(self):
        """Dedupe runs within a query as well as across rungs — the old
        behavior emitted the degenerate query "heroes heroes"."""
        assert build_search_queries("Heroes", "Heroes") == ["heroes"]
        assert build_search_queries("Album Leaf", "The Album Leaf") == ["leaf"]

    def test_never_more_than_three_words(self):
        queries = build_search_queries(
            "Strawberry Fields Forever (Remix)", "Queen of the Night"
        )
        for q in queries:
            assert len(q.split()) <= 3


class TestQualifierMatchingRegressions:
    """Regressions from the P6.5 code review (2026-08-11)."""

    def test_single_word_qualifiers_match_on_word_boundaries(self):
        """Substring matching turned '(Credits)' into the qualifier 'edit'
        and '(Alive)' into 'live', each costing a wasted slskd search on a
        bogus 3-word rung."""
        assert paren_qualifiers("Stayin Alive (Credits)") == []
        assert paren_qualifiers("Song (Alive)") == []
        assert paren_qualifiers("Song (Official Video)") == []

    def test_multi_word_qualifiers_still_match_as_substrings(self):
        assert paren_qualifiers("Song (Sped Up)") == ["sped up"]
        assert paren_qualifiers("Song (VIP Mix)") == ["vip mix"]

    def test_real_qualifiers_still_detected(self):
        assert paren_qualifiers("Heroes (We Could Be) (Remix)") == ["remix"]

    def test_qualifier_order_is_deterministic(self):
        """paren_qualifiers used to iterate a frozenset, so with several
        qualifiers present qualifiers[0] — and therefore the 3-word rung —
        changed between process restarts under hash randomization."""
        assert paren_qualifiers("Song (Live Acoustic Remix)") == [
            "acoustic",
            "remix",
            "live",
        ]
        assert build_search_queries("Rhapsody (Live Acoustic Remix)", "Freddie")[0] == (
            "rhapsody acoustic freddie"
        )

    def test_bogus_qualifier_no_longer_adds_a_rung(self):
        """'(Credits)' previously produced the 3-word rung 'stayin edit
        gees' ahead of the real 2-word ladder."""
        assert build_search_queries("Stayin Alive (Credits)", "Bee Gees") == [
            "stayin gees",
            "alive gees",
            "stayin bee",
            "alive bee",
        ]
