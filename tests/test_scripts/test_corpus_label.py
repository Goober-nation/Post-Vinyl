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


def test_duration_off_by_more_than_6s():
    f = _f("Radiohead\\OK Computer\\Karma Police.flac", 271)
    assert lb.label_track_file(f, KP).label == "duration"


def test_duration_within_tolerance():
    for length in (258, 270):
        f = _f("Radiohead\\OK Computer\\Karma Police.flac", length)
        assert lb.label_track_file(f, KP).label == "correct"


def test_missing_length_is_unverified_not_correct():
    # Decided 2026-10-27: every v2 trap false positive had no length.
    f = _f("Radiohead\\OK Computer\\Karma Police.flac", None)
    assert lb.label_track_file(f, KP).label == "no_length"
    peer = lb.label_peer({"username": "p", "files": [f]}, KP)
    assert not peer.helpful and peer.unverified


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


def test_title_words_out_of_order_do_not_match():
    seed = {"type": "track", "artist": "The xx", "title": "Midnight City"}
    f = _f("Doobie Brothers\\1975 09 xx Memphis TN\\04- South City Midnight Lady.flac")
    assert lb.label_track_file(f, seed).label != "correct"


def test_short_artist_must_be_its_own_segment():
    seed = {"type": "track", "artist": "X", "title": "Los Angeles"}
    ok = _f("X - Los Angeles (1980)\\01 - Los Angeles.mp3")
    assert lb.label_track_file(ok, seed).label == "correct"
    ok2 = _f("Music\\X\\Los Angeles\\Los Angeles.mp3")
    assert lb.label_track_file(ok2, seed).label == "correct"
    bad = _f("Compilations\\Volume X\\Los Angeles.mp3")
    assert lb.label_track_file(bad, seed).label == "no_artist"


def test_bracketed_title_parts_can_move():
    seed = {"type": "track", "artist": "Lil Nas X", "title": "Old Town Road (Remix)"}
    f = _f("Lil Nas X\\7\\Old Town Road (feat. Billy Ray Cyrus) [Remix].mp3")
    assert lb.label_track_file(f, seed).label == "correct"


def test_title_inside_longer_title_still_needs_phrase():
    seed = {"type": "track", "artist": "Muse", "title": "Intro"}
    f = _f("Muse\\Absolution\\01 - Intro.flac")
    assert lb.label_track_file(f, seed).label == "correct"


# --- search test v2 (#76): real filenames from the 2026-10-27 runs ---

BI2 = {"track": "С тобой", "artist": "Би-2", "album": "Мяу кисс ми", "length_s": 238}


@pytest.mark.parametrize(
    "name",
    [
        "@@sykcl\\shared\\rock\\russian\\Би-2\\2001 - Мяу кисс ми\\09 - С тобой.mp3",
        "G:\\БИ-2\\2001 - Мяу Кисс Ми\\09. С тобой.m4a",
        "@@cntiu\\mp3\\Би-2\\2001 - Мяу Кисс Ми\\09 - С Тобой.mp3",
    ],
)
def test_v2_cyrillic_correct(name):
    assert lb.label_track_file(_f(name, 238), BI2).label == "correct"


def test_v2_cyrillic_short_i_is_not_folded():
    # NFKD folding turned й into и; "За тобой" must still not match "С тобой".
    assert lb.norm("Рыжий") == "рыжий"
    name = "G:\\БИ-2\\2013 - Нечетный воин 3\\08. За тобой.m4a"
    assert lb.label_track_file(_f(name, 238), BI2).label == "no_title"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("Би-2\\Drum[a]\\05 - С Тобой(Nova Bossa Mix By Da_Pop).mp3", "excluded:mix"),
        (
            "Би-2\\...и корабль плывет\\CD 4 - LIVE\\07 - С тобой (Москва).mp3",
            "excluded:live",
        ),
        ("Би-2\\Karaoke\\С тобой.mp3", "excluded:karaoke"),
        ("Би-2\\С тобой (Sped Up).mp3", "excluded:sped up"),
        ("Би-2\\С тобой (Nightcore).mp3", "excluded:nightcore"),
        ("Би-2\\С тобой (instrumental).mp3", "excluded:instrumental"),
    ],
)
def test_v2_excluded_versions(name, reason):
    assert lb.label_track_file(_f(name, 238), BI2).label == reason


def test_v2_original_mix_is_the_song():
    seed = {"track": "Feel It", "artist": "Cosmic Gate", "length_s": 382}
    name = "Cosmic Gate\\Feel It\\01 - Cosmic Gate - Feel It (Original Mix).flac"
    assert lb.label_track_file(_f(name, 382), seed).label == "correct"


def test_v2_requested_extended_mix_matches_only_itself():
    seed = {"track": "Feel It (extended mix)", "artist": "Cosmic Gate", "length_s": 382}
    ok = "Cosmic Gate\\Feel It\\01 Cosmic Gate - Feel It (Extended Mix).flac"
    assert lb.label_track_file(_f(ok, 382), seed).label == "correct"


def test_v2_hangul_and_kana_titles():
    seed = {"track": "강도와 꽃다발", "artist": "이은미", "length_s": 200}
    name = "이은미\\Ma Non Tanto\\03 이은미 - 강도와 꽃다발.flac"
    assert lb.label_track_file(_f(name, 200), seed).label == "correct"
    seed = {"track": "強盗と花束", "artist": "ヨルシカ", "length_s": 190}
    name = "ヨルシカ\\創作\\02. 強盗と花束.flac"
    assert lb.label_track_file(_f(name, 190), seed).label == "correct"


def test_v2_accent_folded_both_sides():
    seed = {"track": "Hoppípolla", "artist": "Sigur Rós", "length_s": 270}
    name = "SIGUR ROS\\Takk\\03 - hoppipolla.MP3"
    assert lb.label_track_file(_f(name, 270), seed).label == "correct"


def test_v2_row_counts_free_slots_and_formats():
    seed = {"id": "s", "track": "Dreams", "artist": "Fleetwood Mac", "length_s": 257}
    peers = [
        {
            "username": "a",
            "hasFreeUploadSlot": True,
            "files": [_f("Fleetwood Mac\\Rumours\\02 Dreams.flac", 257)],
        },
        {
            "username": "b",
            "hasFreeUploadSlot": False,
            "files": [
                {**_f("Fleetwood Mac\\Rumours\\02 Dreams.mp3", 257), "bitRate": 320}
            ],
        },
        {"username": "c", "files": [_f("Dokken\\In My Dreams.mp3", 257)]},
    ]
    rec = {"query": "Dreams", "responses": peers, "uses": [{"seed": "s", "shape": "T"}]}
    (row,) = lb.score_record(rec, {"s": seed})
    assert row["helpful_peers"] == 2 and row["helpful_free_slot"] == 1
    assert row["formats"] == {"lossless": 1, "mp3 320": 1}


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("G.O.M.D", "gomd"),
        ("P.O.V.", "pov"),
        ("R.E.M.", "rem"),
        ("J. Cole", "j cole"),  # initial + space is not an acronym
        ("Matty Wood$", "matty woods"),
    ],
)
def test_v2_norm_acronyms_and_symbols(raw, want):
    assert lb.norm(raw) == want


def test_v2_acronym_title_matches_undotted_file():
    seed = {"track": "G.O.M.D", "artist": "J. Cole", "length_s": 301}
    for name in (
        "music\\J. Cole\\08-j_cole-gomd.flac",
        "J Cole\\08 - GOMD - J Cole.flac",
    ):
        assert lb.label_track_file(_f(name, 301), seed).label == "correct"


def test_v2_cyrillic_version_word_matches_english_file():
    seed = {
        "track": "Герой не моего романа (ремикс)",
        "artist": "Юлия Началова",
        "length_s": 284,
    }
    name = (
        "Retro Remix Quality Vol.56 (2018)\\"
        "40. Юлия Началова - Герой не моего романа (remix).mp3"
    )
    assert lb.label_track_file(_f(name, 284), seed).label == "correct"


def test_v2_russian_remix_is_excluded():
    seed = {"track": "Неземная", "artist": "Макс Барских", "length_s": 202}
    name = "Макс Барских\\Неземная (ремикс).mp3"
    assert lb.label_track_file(_f(name, 202), seed).label == "excluded:remix"


def test_v2_live_album_two_folders_up_is_excluded():
    seed = {"track": "Sliver", "artist": "Nirvana", "length_s": 136}
    name = "Nirvana - Live At The Paramount (2011) LP\\Music\\10. Sliver .flac"
    assert lb.label_track_file(_f(name, 136), seed).label == "excluded:live"


@pytest.mark.parametrize(
    "name",
    [
        "Music\\Tagged with cover art Jul 2017\\Massive Attack - Mezzanine\\03 - Teardrop.flac",
        "Música\\OST\\Movie OST\\Drive\\Massive Attack - Teardrop.mp3",
        "--==Ambient&ChillOut==--\\Massive Attack - Mezzanine\\03 - Teardrop.mp3",
    ],
)
def test_v2_category_folders_one_level_up_are_not_versions(name):
    seed = {"track": "Teardrop", "artist": "Massive Attack", "length_s": 330}
    assert lb.label_track_file(_f(name, 330), seed).label == "correct"
