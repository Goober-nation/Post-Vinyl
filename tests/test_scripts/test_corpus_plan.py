"""Tests for the harvest query plan (#34)."""

from pathlib import Path

import tomllib

from scripts.corpus import plan

SEEDS = Path(__file__).parents[1] / "fixtures" / "corpus" / "seeds.toml"


def _shapes(seed):
    return dict(plan.seed_queries(seed))


class TestFold:
    def test_symbols_map_to_letters(self):
        assert plan.fold_query("$uicideboy$") == "suicideboys"

    def test_accents_folded(self):
        assert plan.fold_query("Sigur Rós") == "Sigur Ros"

    def test_punctuation_becomes_space(self):
        assert plan.fold_query("AC/DC") == "AC DC"


class TestDelimiters:
    def test_slash_split(self):
        assert plan.delimiter_parts("Heavy Water/I'd Rather Be Sleeping") == [
            "Heavy Water",
            "I'd Rather Be Sleeping",
        ]

    def test_dash_and_parens(self):
        assert plan.delimiter_parts("Shine On You Crazy Diamond (Parts I-V)") == [
            "Shine On You Crazy Diamond",
            "Parts I-V",
        ]

    def test_hyphen_inside_word_is_not_a_delimiter(self):
        assert plan.delimiter_parts("blink-182") == []

    def test_no_delimiter(self):
        assert plan.delimiter_parts("Karma Police") == []


class TestTwoLongest:
    def test_two_title_words(self):
        assert plan.two_longest("Love Will Tear Us Apart", "Substance") == "apart love"

    def test_one_word_title_borrows_album_word(self):
        assert plan.two_longest("Roads", "Dummy") == "roads dummy"

    def test_short_words_dont_count(self):
        # "One" is the only word >2 chars in "One"; album supplies the second.
        assert plan.two_longest("One", "Achtung Baby") == "one achtung"


class TestSeedQueries:
    def test_track_shapes(self):
        s = _shapes(
            {
                "type": "track",
                "artist": "Sigur Rós",
                "title": "Hoppípolla",
                "album": "Takk...",
            }
        )
        assert s["T"] == "Hoppípolla"
        assert s["A"] == "Sigur Rós"
        assert s["B"] == "Takk..."
        assert s["F:T"] == "Hoppipolla"
        assert s["F:A"] == "Sigur Ros"
        assert s["F:B"] == "Takk"
        assert "L0" in s and "W2" in s
        assert not any(k.startswith("D") for k in s)

    def test_no_mixed_full_title_and_artist_query(self):
        seed = {"type": "track", "artist": "Radiohead", "title": "Karma Police"}
        assert "Karma Police Radiohead" not in _shapes(seed).values()

    def test_fold_only_when_it_changes_text(self):
        s = _shapes({"type": "artist", "artist": "Radiohead"})
        assert s == {"A": "Radiohead"}

    def test_album_shapes(self):
        s = _shapes({"type": "album", "artist": "Massive Attack", "album": "Mezzanine"})
        assert s["B"] == "Mezzanine" and s["A"] == "Massive Attack"
        assert s["LW"] == "mezzanine massive"
        assert s["W2"] == "mezzanine massive"

    def test_negative_seed_without_album_has_no_b(self):
        s = _shapes(
            {"type": "track", "artist": "Sigur Rós", "title": "Toxic", "expect": "none"}
        )
        assert "B" not in s and s["T"] == "Toxic"


def test_plan_dedupes_queries_and_keeps_every_use():
    seeds = [
        {
            "type": "track",
            "artist": "Radiohead",
            "title": "Karma Police",
            "album": "OK Computer",
        },
        {"type": "artist", "artist": "Radiohead"},
    ]
    p = plan.build_plan(seeds)
    radiohead = [q for q in p["queries"] if q["query"] == "Radiohead"]
    assert len(radiohead) == 1
    assert {u["shape"] for u in radiohead[0]["uses"]} == {"A"}
    assert len(radiohead[0]["uses"]) == 2


def test_sample_spreads_across_buckets():
    seeds = tomllib.loads(SEEDS.read_text())["seed"]
    picked = plan.sample_seeds(seeds, 12)
    assert len(picked) == 12
    assert all(s["type"] == "track" for s in picked)
    assert len({s["bucket"] for s in picked}) == 12


def test_real_seed_list_plans_cleanly():
    seeds = tomllib.loads(SEEDS.read_text())["seed"]
    p = plan.build_plan(seeds)
    assert len(p["seeds"]) == len(seeds)
    assert all(q["query"].strip() for q in p["queries"])
    assert len({q["qid"] for q in p["queries"]}) == len(p["queries"])
