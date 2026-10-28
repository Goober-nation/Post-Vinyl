"""Query shapes and order (#91)."""

from __future__ import annotations

import pytest

from app.core.strategy import album_query, select_words, track_queries, usable_album


def test_order_l0_ta_t():
    assert track_queries("Karma Police", "Radiohead") == [
        ("L0", "police radiohead"),
        ("TA", "Karma Police Radiohead"),
        ("T", "Karma Police"),
    ]


def test_l0_carries_bracket_qualifier():
    assert track_queries("Midnight City (Live)", "M83")[0] == (
        "L0",
        "midnight live m83",
    )


def test_duplicates_dropped():
    # one-word title, artist with no usable word: L0 equals T
    assert track_queries("Yesterday", "U2") == [
        ("L0", "yesterday"),
        ("TA", "Yesterday U2"),
    ]


def test_no_title_word_falls_back_to_artist():
    assert track_queries("21", "Adele")[0] == ("L0", "adele")


def test_words_keep_their_script():
    assert select_words("Высоцкий") == ["высоцкий"]
    assert select_words("이은미") == ["이은미"]  # 2+ letters outside Latin
    assert select_words("Café Tacvba") == ["tacvba", "café"]
    assert select_words("Go") == []  # Latin needs 3+


@pytest.mark.parametrize(
    ("album", "artist", "usable"),
    [
        ("OK Computer", "Radiohead", True),
        ("Weezer", "Weezer", False),  # self-titled
        ("Massive Attack", "Massive Attack", False),
        ("Greatest Hits", "Queen", False),  # generic
        ("The Best Of", "Sade", False),
        ("Unplugged", "Eric Clapton", False),
        ("7", "Madonna", False),  # digits only
        ("21", "Adele", False),
        ("X", "Ed Sheeran", False),  # one character
    ],
)
def test_usable_album(album, artist, usable):
    assert usable_album(album, artist) is usable


def test_album_query():
    assert album_query("OK Computer", "Radiohead") == ("LW", "computer radiohead")
    assert album_query("Unplugged in New York", "Nirvana") == ("LW", "york nirvana")
    assert album_query("Greatest Hits", "Queen") == ("BA", "Greatest Hits Queen")
    assert album_query("Weezer", "Weezer") == ("BA", "Weezer")
