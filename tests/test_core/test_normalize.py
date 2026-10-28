"""Tests for the slskd query-name rules (#70, decided in #71): one block per rule."""

import pytest

from app.core import normalize as n

NO_RULES: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Dracula (with JENNIE)", "Dracula"),
        ("Lose Control [feat. X & Y]", "Lose Control"),
        ("Song feat. Somebody", "Song"),
        ("Song ft. Somebody", "Song"),
        ("Run with the Hunted", "Run with the Hunted"),  # unbracketed "with" stays
        ("Featuring Doom", "Featuring Doom"),  # leading word, not a credit
    ],
)
def test_feat(raw, want):
    assert n.query_title(raw) == want


def test_feat_off():
    assert (
        n.query_title("Dracula (with JENNIE)", rules=NO_RULES)
        == "Dracula (with JENNIE)"
    )


@pytest.mark.parametrize(
    ("raw", "names", "want"),
    [
        ("Stateside + Zara Larsson", ["PinkPantheress", "Zara Larsson"], "Stateside"),
        ("des fleurs x stromae", ["Pierre", "Stromae"], "des fleurs"),
        ("Song × A & B", ["Main", "A", "B"], "Song"),
        ("Malcolm X", ["Somebody"], "Malcolm X"),  # X isn't a credited artist
        ("Rock & Roll", ["Led Zeppelin"], "Rock & Roll"),
    ],
)
def test_title_credits(raw, names, want):
    assert n.query_title(raw, [n.Credit(x) for x in names]) == want


def test_main_artist_skips_featured():
    credits = n.credits_from_joined([("A", " feat. "), ("B", " & "), ("C", "")])
    assert [c.featured for c in credits] == [False, True, True]
    assert n.main_artist(credits) == "A"


def test_main_artist_is_first_of_equal_credits():
    credits = n.credits_from_joined([("Quevedo", " & "), ("Elvis Crespo", "")])
    assert n.main_artist(credits) == "Quevedo"


def test_with_join_phrase_is_featured():
    credits = n.credits_from_joined([("Tame Impala", " with "), ("JENNIE", "")])
    assert n.main_artist(credits) == "Tame Impala"


@pytest.mark.parametrize(
    ("raw", "want"),
    [("The Beatles", "Beatles"), ("the National", "National"), ("The", "The")],
)
def test_the(raw, want):
    assert n.main_artist([n.Credit(raw)]) == want


def test_the_off():
    assert n.main_artist([n.Credit("The Beatles")], NO_RULES) == "The Beatles"


@pytest.mark.parametrize(
    "raw",
    [
        "Высоцкий",
        "이은미",
        "Café Tacvba",
        "Yellow Submarine (Remastered 2009)",
        "Simon & Garfunkel",
        "Don't Stop Me Now",
        "LA GRACIOSA",
    ],
)
def test_everything_else_kept_as_written(raw):
    assert n.query_title(raw) == raw


def test_query_names_keeps_full_credit():
    credits = n.credits_from_joined([("Koe Wetzel", " feat. "), ("Ella Langley", "")])
    q = n.query_names("Jaded", credits, "  9 lives ", "Koe Wetzel feat. Ella Langley")
    assert q == n.QueryNames(
        "Jaded", "Koe Wetzel", "9 lives", "Koe Wetzel feat. Ella Langley"
    )


def test_source_spelling_kept():
    # MusicBrainz writes U+2010; folding is the matcher's job, not the query's
    jay = n.query_names("Empire State of Mind", [n.Credit("JAY‐Z")], None)
    assert jay.artist == "JAY‐Z"
    assert n.query_names("с тобой", [n.Credit("Би-2")], None).artist == "Би-2"
