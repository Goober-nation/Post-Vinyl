"""Tests for the corpus labeller (#34) — the rules a wrong verdict would corrupt."""

import pytest

from scripts.corpus import label as lb

KP = {
    "type": "track",
    "artist": "Radiohead",
    "title": "Karma Police",
    "album": "OK Computer",
    "duration": 264,
}


def _f(name, length=None, **kw):
    return {"filename": name, "length": length, **kw}


@pytest.mark.parametrize(
    "name",
    [
        "@@x\\Music\\Radiohead\\OK Computer\\06 - Karma Police.flac",
        "Radiohead - OK Computer (1997)\\06. Radiohead - Karma Police.mp3",
        "Radiohead\\OK Computer OKNOTOK\\06 Karma Police (Remastered).flac",
        "RADIOHEAD - KARMA POLICE.MP3",
    ],
)
def test_correct(name):
    assert lb.label_track_file(_f(name, 264), KP).label == "correct"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("Radiohead\\Live at Glastonbury\\Karma Police.mp3", "excluded:live"),
        ("Radiohead\\OK Computer\\Karma Police (Live).mp3", "excluded:live"),
        ("Covers\\Radiohead - Karma Police (Acoustic Cover).mp3", "excluded:cover"),
        ("Radiohead\\Karma Police (Extended Mix).flac", "excluded:mix"),
        ("Radiohead\\Karma Police (A Cappella).flac", "excluded:a cappella"),
        ("Radiohead\\Karma Police - Home Session.flac", "excluded:home session"),
        ("Radiohead\\Karma Police (Radio Edit).flac", "excluded:edit"),
        ("Radiohead\\Livebox (CD3)\\Karma Police.mp3", "excluded:live"),
        ("Radiohead\\Live1997\\Karma Police.mp3", "excluded:live"),
        ("Radiohead\\OK Computer\\Airbag.flac", "no_title"),
        ("Karma Police\\01 Track.flac", "no_title"),  # title only in folder
        ("Various\\Karma Police.flac", "no_artist"),
        ("Radiohead\\OK Computer\\cover.jpg", "not_audio"),
    ],
)
def test_wrong(name, reason):
    assert lb.label_track_file(_f(name, 264), KP).label == reason


def test_duration_off_by_more_than_3s():
    f = _f("Radiohead\\OK Computer\\Karma Police.flac", 270)
    assert lb.label_track_file(f, KP).label == "duration"


def test_duration_within_tolerance_or_missing():
    for length in (261, 267, None):
        f = _f("Radiohead\\OK Computer\\Karma Police.flac", length)
        assert lb.label_track_file(f, KP).label == "correct"


def test_excluded_word_allowed_when_seed_has_it():
    seed = {
        "type": "track",
        "artist": "Talking Heads",
        "title": "Once in a Lifetime (live)",
        "album": "Stop Making Sense",
    }
    f = _f("Talking Heads\\Stop Making Sense (Live)\\Once in a Lifetime (Live).flac")
    assert lb.label_track_file(f, seed).label == "correct"


def test_ambient_album_is_not_excluded():
    seed = {
        "type": "track",
        "artist": "Aphex Twin",
        "title": "Rhubarb",
        "album": "Selected Ambient Works Volume II",
    }
    f = _f("Aphex Twin\\Selected Ambient Works Volume II\\Rhubarb.flac")
    assert lb.label_track_file(f, seed).label == "correct"


def test_word_inside_longer_word_is_not_excluded():
    # "live" inside "Oliver"/"Lively" or "mix" inside "Mixtape" must not count.
    seed = {"type": "track", "artist": "Oliver Tree", "title": "Life Goes On"}
    f = _f("Oliver Tree\\Ugly Is Beautiful\\Life Goes On.mp3")
    assert lb.label_track_file(f, seed).label == "correct"


def test_non_latin():
    seed = {"type": "track", "artist": "宇多田ヒカル", "title": "First Love"}
    f = _f("宇多田ヒカル\\First Love\\03 First Love.flac")
    assert lb.label_track_file(f, seed).label == "correct"


def test_accents_and_symbols():
    seed = {"type": "track", "artist": "Sigur Rós", "title": "Hoppípolla"}
    assert (
        lb.label_track_file(_f("Sigur Ros\\Takk\\Hoppipolla.mp3"), seed).label
        == "correct"
    )


def test_peer_counts_once_and_locked_files_dont_help():
    peer = {
        "username": "p",
        "files": [
            _f("Radiohead\\OK Computer\\Karma Police.flac", 264, bitRate=None),
            _f("Radiohead\\Best Of\\Karma Police.mp3", 264, bitRate=320),
        ],
        "lockedFiles": [_f("Radiohead\\x\\Karma Police.flac", 264)],
    }
    p = lb.label_peer(peer, KP)
    assert p.helpful and len(p.correct) == 2 and p.locked == 1
    assert p.best["ext"] == "flac"  # lossless outranks 320 mp3

    locked_only = {"username": "q", "files": [], "lockedFiles": peer["lockedFiles"]}
    assert not lb.label_peer(locked_only, KP).helpful


def test_album_peer_needs_most_of_the_tracklist():
    seed = {
        "type": "album",
        "artist": "Massive Attack",
        "album": "Mezzanine",
        "track_count": 11,
    }
    full = [_f(f"Massive Attack\\Mezzanine (1998)\\{i:02d}.flac") for i in range(1, 11)]
    assert lb.label_peer({"username": "a", "files": full}, seed).helpful
    partial = full[:5]
    assert not lb.label_peer({"username": "b", "files": partial}, seed).helpful
    live = [_f(f"Massive Attack\\Mezzanine Live\\{i:02d}.flac") for i in range(1, 12)]
    assert not lb.label_peer({"username": "c", "files": live}, seed).helpful


def test_score_record_counts_peers_not_files():
    seeds = {"s": {**KP, "id": "s"}}
    record = {
        "query": "karma radiohead",
        "stop_reason": "complete",
        "uses": [{"seed": "s", "shape": "L1"}],
        "responses": [
            {"username": "a", "files": [_f("Radiohead\\Karma Police.flac", 264)] * 40},
            {"username": "b", "files": [_f("Radiohead\\Karma Police (Live).flac")]},
            {"username": "c", "files": [_f("Radiohead\\Karma Police.mp3", 263)]},
        ],
    }
    (row,) = lb.score_record(record, seeds)
    assert row["peers"] == 3 and row["helpful_peers"] == 2
    assert row["family"] == "L1+"


@pytest.mark.parametrize("folder", ["Liverpool Sessions", "Lively Up", "Oliver"])
def test_live_lookalikes_not_excluded(folder):
    f = _f(f"Radiohead\\{folder}\\Karma Police.flac", 264)
    assert lb.label_track_file(f, KP).label == "correct"


def test_plan_uses_are_merged_by_qid():
    rec = {"qid": "q", "uses": [{"seed": "album", "shape": "LW"}]}
    plan = {"queries": [{"qid": "q", "uses": [{"seed": "track", "shape": "LW"}]}]}
    lb.merge_plan_uses([rec], [plan, plan])
    assert rec["uses"] == [
        {"seed": "album", "shape": "LW"},
        {"seed": "track", "shape": "LW"},
    ]
