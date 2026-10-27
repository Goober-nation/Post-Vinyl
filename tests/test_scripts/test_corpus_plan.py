"""Tests for the search test v2 query plan (#75)."""

import json
from pathlib import Path

import pytest

from scripts.corpus import plan

SEEDS = Path(__file__).parents[1] / "fixtures" / "search_test_v2" / "seeds.json"


def _shapes(track, artist, album=""):
    seed = {"id": "x", "track": track, "artist": artist, "album": album}
    return dict(plan.seed_queries(seed))


class TestSelectWords:
    @pytest.mark.parametrize(
        ("text", "want"),
        [
            ("Love Will Tear Us Apart", ["apart", "love", "will", "tear"]),
            ("Высоцкий", ["высоцкий"]),
            ("強盗と花束", ["強盗と花束"]),
            ("안녕", ["안녕"]),  # 2 letters count outside Latin script
            ("Hoppípolla", ["hoppípolla"]),  # accents kept
            ("Ain’t in LA", ["ain’t"]),  # apostrophe keeps the word whole
        ],
    )
    def test_words(self, text, want):
        assert plan.select_words(text) == want

    def test_drops_feat_brackets_stopwords_short_and_digits(self):
        assert plan.select_words("Feel It (extended mix) feat. Somebody") == ["feel"]
        assert plan.select_words("The 1975 of Us") == []


class TestLadder:
    def test_l0_l1(self):
        assert plan.ladder("Love Will Tear Us Apart", "Joy Division") == [
            "apart division",
            "love division",
        ]

    def test_qualifier_goes_into_l0(self):
        assert plan.ladder("Feel It (extended mix)", "Cosmic Gate") == [
            "feel extended cosmic"
        ]

    def test_original_script(self):
        assert plan.ladder("С тобой", "Би-2") == ["тобой би"]

    def test_title_without_words_uses_artist(self):
        assert plan.ladder("Intro", "Muse") == ["muse"]


class TestFold:
    def test_symbols_map_to_letters(self):
        assert plan.fold_query("$uicideboy$") == "suicideboys"

    def test_accents_folded(self):
        assert plan.fold_query("Hoppípolla") == "Hoppipolla"

    def test_non_latin_marks_survive(self):
        assert plan.fold_query("丸の内サディスティック") == "丸の内サディスティック"
        assert plan.fold_query("Судно (Борис Рыжий)") == "Судно Борис Рыжий"


class TestSeedQueries:
    def test_shapes(self):
        s = _shapes("Sábado, Distrito Federal", "Chava Flores", "Mi pueblo")
        assert s == {
            "L0": "distrito flores",
            "L1": "federal flores",
            "T": "Sábado, Distrito Federal",
            "F:T": "Sabado Distrito Federal",
            "TA": "Sábado, Distrito Federal Chava Flores",
            "B": "Mi pueblo",
        }

    def test_no_fold_when_only_punctuation_or_case_changes(self):
        assert "F:T" not in _shapes("Back, Baby", "Jessica Pratt")
        assert "F:T" not in _shapes("Высоцкий", "x")

    def test_no_album_no_b(self):
        assert "B" not in _shapes("Toxic", "Sigur Rós")

    def test_dropped_shapes_are_gone(self):
        keys = set(_shapes("Karma Police", "Radiohead", "OK Computer"))
        assert keys == {"L0", "L1", "T", "TA", "B"}


def test_plan_dedupes_queries_and_keeps_every_use():
    seeds = [
        {"id": "a", "track": "Dreams", "artist": "Fleetwood Mac", "album": "Rumours"},
        {"id": "b", "track": "Dreams", "artist": "The Cranberries", "album": ""},
    ]
    p = plan.build_plan(seeds)
    t = next(q for q in p["queries"] if q["query"] == "Dreams")
    assert t["uses"] == [{"seed": "a", "shape": "T"}, {"seed": "b", "shape": "T"}]


def test_real_seed_list_plans_cleanly():
    data = json.loads(SEEDS.read_text())
    seeds = data["seeds"] + data["traps"]
    p = plan.build_plan(seeds)
    planned = {u["seed"] for q in p["queries"] for u in q["uses"]}
    assert planned == {s["id"] for s in seeds}
    assert all(q["query"].strip() for q in p["queries"])
