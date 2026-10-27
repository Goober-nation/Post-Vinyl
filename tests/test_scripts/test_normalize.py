"""Tests for the normalization prototype (#70): one block per rule."""

import pytest

from scripts.corpus import normalize as n

NO_RULES: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Dracula (with JENNIE)", "dracula"),
        ("Lose Control [feat. X & Y]", "lose control"),
        ("Song feat. Somebody", "song"),
        ("Song ft. Somebody", "song"),
        ("Run with the Hunted", "run with the hunted"),  # unbracketed "with" stays
        ("Featuring Doom", "featuring doom"),  # no leading space: title starts with it
    ],
)
def test_feat_title(raw, want):
    assert n.norm_title(raw) == want


def test_feat_off_keeps_credit():
    assert (
        n.norm_title("Dracula (with JENNIE)", n.ALL_RULES - {"feat"})
        == "dracula with jennie"
    )


def test_featured_artists_dropped_from_joined_credit():
    credits = n.credits_from_joined([("A", " feat. "), ("B", " & "), ("C", "")])
    assert n.norm_artists(credits) == ("a",)
    assert n.norm_artists(credits, n.ALL_RULES - {"feat"}) == ("a", "b", "c")


def test_main_artists_are_a_sorted_set():
    credits = n.credits_from_joined([("Quevedo", " & "), ("Elvis Crespo", "")])
    assert n.norm_artists(credits) == ("elvis crespo", "quevedo")


@pytest.mark.parametrize(
    "raw",
    [
        "Yellow Submarine (Remastered 2009)",
        "Yellow Submarine - Remastered 2009",
        "Yellow Submarine (2009 Remaster)",
        "Yellow Submarine - 2009 - Remaster",
        "Yellow Submarine [Album Version]",
        "Yellow Submarine (1966)",
        "Yellow Submarine (Deluxe Edition) (Remastered)",
    ],
)
def test_version_suffixes(raw):
    assert n.norm_title(raw) == "yellow submarine"


@pytest.mark.parametrize(
    "raw",
    ["Song (Live)", "Song - Radio Edit", "Song (Remix)", "Song (Acoustic)"],
)
def test_version_keeps_different_versions(raw):
    assert n.norm_title(raw) != n.norm_title("Song")


def test_year_only_title_survives():
    assert n.norm_title("1999") == "1999"


def test_amp():
    assert n.norm_text("Simon & Garfunkel") == "simon and garfunkel"
    assert n.norm_text("Simon & Garfunkel", n.ALL_RULES - {"amp"}) == "simon garfunkel"


def test_the_only_for_artists():
    assert n.norm_artist("The Beatles") == "beatles"
    assert n.norm_artist("The The") == "the"
    assert n.norm_title("The Wall") == "the wall"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Don't Stop Me Now", "dont stop me now"),
        ("Don’t Stop Me Now", "dont stop me now"),
        ("a‐ha", "a ha"),  # U+2010 hyphen
        ("Время-река", "время река"),
        ("Esto es ¡FA!", "esto es fa"),
    ],
)
def test_punct(raw, want):
    assert n.norm_text(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Soirée mondaine", "soiree mondaine"),
        ("Beyoncé", "beyonce"),
        ("Йорш", "йорш"),  # Cyrillic й keeps its breve
        ("Ёлка", "ёлка"),
        ("椎名林檎", "椎名林檎"),
        ("ポルノグラフィティ", "ポルノグラフィティ"),  # kana voicing marks stay
    ],
)
def test_accents_latin_only(raw, want):
    assert n.norm_text(raw) == want


def test_no_transliteration():
    assert n.norm_text("Кино") != n.norm_text("Kino")


def test_case():
    assert n.norm_text("LA GRACIOSA") == "la graciosa"
    assert n.norm_text("LA GRACIOSA", n.ALL_RULES - {"case"}) == "LA GRACIOSA"


def test_no_rules_is_whitespace_only():
    assert n.norm_title("  Song  (feat. X) ", NO_RULES) == "Song (feat. X)"
