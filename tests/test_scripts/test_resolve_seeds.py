"""Tests for the seed-list MusicBrainz resolver (#34)."""

from unittest.mock import MagicMock

import tomllib

from app.services.interfaces.musicbrainz import (
    MBArtist,
    MBRecording,
    MBRelease,
    MBReleaseGroup,
)
from scripts.corpus import resolve_seeds as rs


def _rec(mbid, title, artist, releases=(), length_ms=200_000):
    return MBRecording(
        mbid=mbid,
        title=title,
        artist_credit=artist,
        artist=artist,
        length_ms=length_ms,
        releases=list(releases),
    )


def _rel(title, rg, status="Official", primary_type="Album"):
    return MBRelease(
        mbid=f"rel-{rg}",
        title=title,
        status=status,
        primary_type=primary_type,
        release_group_mbid=rg,
    )


class TestNorm:
    def test_accents_case_punctuation(self):
        assert rs.norm("Sigur Rós") == rs.norm("sigur ros")
        assert rs.norm("AC/DC") == rs.norm("ac dc")

    def test_non_latin_survives(self):
        assert rs.norm("椎名林檎") == "椎名林檎"
        assert rs.norm("Кино") == "кино"

    def test_remaster_suffix_is_same_song(self):
        assert rs.norm_title("Dreams (2004 Remaster)") == rs.norm_title("Dreams")
        assert rs.norm_title("Dreams - Remastered 2004") == rs.norm_title("Dreams")

    def test_live_and_remix_are_not_stripped(self):
        assert rs.norm_title("Creep (Acoustic)") != rs.norm_title("Creep")
        assert rs.norm_title("Once in a Lifetime (live)") != rs.norm_title(
            "Once in a Lifetime"
        )

    def test_names_match_feat_prefix(self):
        assert rs.names_match("Daft Punk", "Daft Punk feat. Pharrell Williams")
        assert not rs.names_match("Can", "Candlemass")


class TestFileEditing:
    TEXT = (
        "# header\n\n"
        '[[seed]]\ntype = "track"\nartist = "A"\ntitle = "T"\n\n'
        "# ── next bucket ──\n"
        '[[seed]]\ntype = "artist"\nartist = "B"\n'
    )

    def test_roundtrip_is_lossless(self):
        pre, blocks = rs.split_blocks(self.TEXT)
        assert rs.join_blocks(pre, blocks) == self.TEXT

    def test_insert_lands_before_trailing_comment(self):
        pre, blocks = rs.split_blocks(self.TEXT)
        blocks[0] = rs.set_keys(blocks[0], {"mbid": "x", "duration": 5})
        out = rs.join_blocks(pre, blocks)
        assert 'title = "T"\nmbid = "x"\nduration = 5\n\n# ── next bucket' in out
        assert tomllib.loads(out)["seed"][0]["mbid"] == "x"

    def test_existing_key_is_replaced(self):
        _, blocks = rs.split_blocks(self.TEXT)
        b = rs.set_keys(blocks[1], {"artist": "C"})
        assert 'artist = "C"' in b and 'artist = "B"' not in b


class TestResolve:
    def test_track_on_album_is_ok(self):
        mb = MagicMock()
        mb.search_recording.return_value = [
            _rec("live", "Roads", "Portishead", [_rel("Roseland NYC Live", "rg-live")]),
            _rec("studio", "Roads", "Portishead", [_rel("Dummy", "rg-dummy")], 305_400),
        ]
        r = rs.resolve(
            mb,
            {
                "type": "track",
                "artist": "Portishead",
                "title": "Roads",
                "album": "Dummy",
            },
        )
        assert r.values == {
            "mbid": "studio",
            "album_mbid": "rg-dummy",
            "duration": 305,
            "mb_status": "ok",
        }

    def test_track_off_album_is_ambiguous(self):
        mb = MagicMock()
        mb.search_recording.return_value = [
            _rec("x", "Roads", "Portishead", [_rel("Other", "rg")])
        ]
        r = rs.resolve(
            mb,
            {
                "type": "track",
                "artist": "Portishead",
                "title": "Roads",
                "album": "Dummy",
            },
        )
        assert r.values["mb_status"] == "ambiguous" and r.values["mbid"] == "x"

    def test_track_wrong_artist_is_unresolved(self):
        mb = MagicMock()
        mb.search_recording.return_value = [_rec("x", "Run", "Snow Patrol")]
        r = rs.resolve(
            mb,
            {
                "type": "track",
                "artist": "Leona Lewis",
                "title": "Run",
                "album": "Spirit",
            },
        )
        assert r.values == {
            "mb_status": "unresolved",
            "mb_note": "no recording matched title+artist",
        }

    def test_tagged_mbid_is_verified_not_searched(self):
        mb = MagicMock()
        mb.lookup_recording.return_value = _rec(
            "m", "I THINK", "Tyler, The Creator", [_rel("IGOR", "rg")]
        )
        seed = {
            "type": "track",
            "artist": "Tyler, The Creator",
            "title": "I THINK",
            "album": "IGOR",
            "mbid": "m",
            "duration": 212,
        }
        r = rs.resolve(mb, seed)
        mb.search_recording.assert_not_called()
        assert r.values == {"album_mbid": "rg", "duration": 212, "mb_status": "ok"}

    def test_album_with_track_count(self):
        mb = MagicMock()
        mb.search_release_group.return_value = [
            MBReleaseGroup(
                mbid="rg",
                title="Mezzanine",
                artist="Massive Attack",
                primary_type="Album",
                score=100,
            )
        ]
        mb.lookup_release_group_tracks.return_value = [object()] * 11
        r = rs.resolve(
            mb, {"type": "album", "artist": "Massive Attack", "album": "Mezzanine"}
        )
        assert r.values == {"mbid": "rg", "track_count": 11, "mb_status": "ok"}

    def test_artist_name_collision_is_ambiguous(self):
        mb = MagicMock()
        mb.search_artist.return_value = [
            MBArtist(mbid="a1", name="Low", disambiguation="US slowcore", score=100),
            MBArtist(mbid="a2", name="Low", disambiguation="UK", score=90),
            MBArtist(mbid="a3", name="Lower", score=80),
        ]
        r = rs.resolve(mb, {"type": "artist", "artist": "Low"})
        assert r.values["mb_status"] == "ambiguous" and r.values["mbid"] == "a1"

    def test_negative_seed_that_exists_is_flagged(self):
        mb = MagicMock()
        mb.search_recording.return_value = [_rec("x", "Home", "Nirvana")]
        r = rs.resolve(
            mb,
            {"type": "track", "artist": "Nirvana", "title": "Home", "expect": "none"},
        )
        assert r.values["mb_status"] == "trap_exists"

    def test_negative_seed_ok(self):
        mb = MagicMock()
        mb.search_recording.return_value = [_rec("x", "Wonderwall", "Oasis")]
        r = rs.resolve(
            mb,
            {
                "type": "track",
                "artist": "Radiohead",
                "title": "Wonderwall",
                "expect": "none",
            },
        )
        assert r.values == {"mb_status": "trap_ok"}


def test_main_skips_resolved_and_writes(tmp_path, monkeypatch):
    seeds = tmp_path / "seeds.toml"
    seeds.write_text(
        '[[seed]]\ntype = "artist"\nbucket = "b"\nartist = "Can"\nmb_status = "ok"\n\n'
        '[[seed]]\ntype = "artist"\nbucket = "b"\nartist = "Air"\n'
    )
    mb = MagicMock()
    mb.search_artist.return_value = [MBArtist(mbid="air", name="Air", score=100)]
    monkeypatch.setattr(rs, "MusicBrainzClient", lambda _cfg: mb)
    assert rs.main(["--seeds", str(seeds), "--report", str(tmp_path / "r.md")]) == 0
    data = tomllib.loads(seeds.read_text())["seed"]
    assert data[1] == {**data[1], "mbid": "air", "mb_status": "ok"}
    assert mb.search_artist.call_count == 1


def test_track_found_via_album_tracklist_before_recording_search():
    mb = MagicMock()
    mb.search_release_group.return_value = [
        MBReleaseGroup(
            mbid="rg-okc", title="OK Computer", artist="Radiohead", primary_type="Album"
        )
    ]
    mb.lookup_release_group_tracks.return_value = [
        _rec("a", "Airbag", "Radiohead"),
        _rec("kp", "Karma Police", "Radiohead", length_ms=264_000),
    ]
    seed = {
        "type": "track",
        "artist": "Radiohead",
        "title": "Karma Police",
        "album": "OK Computer",
    }
    r = rs.resolve(mb, seed)
    mb.search_recording.assert_not_called()
    assert r.values == {
        "mbid": "kp",
        "album_mbid": "rg-okc",
        "duration": 264,
        "mb_status": "ok",
    }


def test_remaster_suffix_is_stripped_from_search():
    mb = MagicMock()
    mb.search_release_group.return_value = []
    mb.search_recording.return_value = []
    rs.resolve(
        mb,
        {"type": "track", "artist": "Fleetwood Mac", "title": "Dreams (2004 Remaster)"},
    )
    assert mb.search_recording.call_args.args[0] == "Dreams"
