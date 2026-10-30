"""File matcher (#90). The benchmark's labeller tests (tests/test_scripts/
test_corpus_label.py) cover the same rules through label.py; these cover the
app-facing API and what changed when the rules moved here."""

from __future__ import annotations

import pytest

from app.core.matcher import Song, album_folder_ok, is_match, label_file

KP = Song("Karma Police", "Radiohead", "OK Computer", 264)


def test_correct_with_backslash_path_and_empty_extension_field():
    # slskd's extension field is often empty: the filename decides
    assert is_match("@@x\\Radiohead\\OK Computer\\06 - Karma Police.FLAC", 264, KP)


@pytest.mark.parametrize(
    ("name", "length", "want"),
    [
        ("Radiohead\\Karma Police.mp3", None, "no_length"),
        ("Radiohead\\Karma Police.mp3", 271, "duration"),
        ("Radiohead\\Karma Police.mp3", 270, "correct"),  # ±6 s
        ("Radiohead\\Karma Police.jpg", 264, "not_audio"),
        ("Radiohead\\Police Karma.mp3", 264, "no_title"),
        ("Various\\Karma Police.mp3", 264, "no_artist"),
        (
            "Radiohead\\Karma Police (Made Famous By Radiohead).mp3",
            264,
            "excluded:made famous by",
        ),
        ("Radiohead\\Karma Police (Backing Track).mp3", 264, "excluded:backing track"),
        ("Radiohead\\Karma Police (VIP Mix).mp3", 264, "excluded:mix"),
        ("Radiohead\\Livebox\\Karma Police.mp3", 264, "excluded:live"),
        ("Radiohead\\Liverpool\\Karma Police.mp3", 264, "correct"),
        ("Radiohead\\Karma Police (Mazaruni Dub One).mp3", 264, "correct"),
        (
            "Radiohead\\OK Computer (drumless edition)\\Karma Police.mp3",
            264,
            "excluded:drumless",
        ),
        (
            "Radiohead\\Karma Police (BBC Radio One Session).mp3",
            264,
            "excluded:radio one session",
        ),
        ("Radiohead\\Karma Police - Audiobook.mp3", 264, "excluded:audiobook"),
        ("Radiohead\\Podcast 12\\Karma Police.mp3", 264, "excluded:podcast"),
        ("Radiohead\\Karma Police (Voiceover).mp3", 264, "excluded:voiceover"),
        ("Radiohead\\The Abbey Road Sessions\\Karma Police.mp3", 264, "correct"),
    ],
)
def test_labels(name, length, want):
    assert label_file(name, length, KP) == want


def test_locked_never_counts():
    assert label_file("Radiohead\\Karma Police.mp3", 264, KP, locked=True) == "locked"


def test_song_without_length_skips_duration():
    song = Song("Karma Police", "Radiohead")
    assert is_match("Radiohead\\Karma Police.mp3", None, song)
    assert is_match("Radiohead\\Karma Police.mp3", 999, song)


def test_song_that_is_a_version_keeps_it():
    song = Song("Old Town Road (Remix)", "Lil Nas X", length_s=None)
    assert is_match(
        "Lil Nas X\\Old Town Road (feat. Billy Ray Cyrus) [Remix].mp3", None, song
    )
    assert not is_match("Lil Nas X\\Old Town Road.mp3", None, song)


def test_accents_and_hyphens_fold_on_both_sides():
    song = Song("Hoppípolla", "Sigur Rós")
    assert is_match("Sigur Ros\\Takk\\02 Hoppipolla.flac", None, song)
    jay = Song("Empire State of Mind", "JAY‐Z")  # U+2010 from MusicBrainz
    assert is_match("Jay-Z\\Empire State of Mind.mp3", None, jay)


def test_non_latin_kept_whole():
    song = Song("с тобой", "Би-2")
    assert is_match("Би-2\\С тобой.mp3", None, song)
    assert not is_match("Би-2\\С тобои.mp3", None, song)  # й is not и


def test_short_artist_must_be_own_segment():
    xx = Song("Intro", "xx")
    assert is_match("The xx\\xx\\01 Intro.mp3", None, xx)
    assert not is_match("Bootlegs\\1975 09 xx Memphis\\Intro.mp3", None, xx)


def test_album_folder():
    files = [f"{i:02} Track.flac" for i in range(1, 11)] + ["cover.jpg"]
    folder = "Music\\Radiohead\\OK Computer (1997)"
    assert album_folder_ok(
        folder, files, "OK Computer", "Radiohead", 12
    )  # 10 ≥ 80% of 12
    assert not album_folder_ok(folder, files, "OK Computer", "Radiohead", 14)
    assert not album_folder_ok(folder + " Live", files, "OK Computer", "Radiohead", 10)
    assert album_folder_ok("VA\\Now 50", files, "Now 50", "Various Artists", 10)


@pytest.mark.parametrize(
    "name",
    [
        "Live Music\\Radiohead\\1998-04-06 - Pacific Coliseum\\Side A\\02 - Karma Police.flac",
        "Radiohead\\2016.07.02 St. Gallen\\25- Karma Police.flac",
        "Live Music\\Radiohead\\Karma Police.flac",  # "live" any folder up
    ],
)
def test_concert_recordings_excluded(name):
    assert label_file(name, 264, KP) == "excluded:live"


def test_live_song_keeps_dated_folders():
    song = Song("Karma Police (Live)", "Radiohead", length_s=264)
    name = "Radiohead\\1998-04-06 - Pacific Coliseum\\02 - Karma Police (Live).flac"
    assert label_file(name, 264, song) == "correct"


def test_year_only_folder_is_not_a_concert():
    assert is_match("Radiohead\\1997 - OK Computer\\06 Karma Police.flac", 264, KP)
