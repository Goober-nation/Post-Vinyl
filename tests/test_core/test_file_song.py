"""Names from peer paths (#108); paths taken from live slskd responses."""

from __future__ import annotations

import pytest

from app.core.file_song import UNKNOWN_ARTIST, names_from_path


@pytest.mark.parametrize(
    ("path", "title", "artist"),
    [
        (r"music\cd rips\Sigur Rós - Takk… (2005) [00946 337 252 2 5]\03. Sigur Rós - Hoppípolla.flac",
         "Hoppípolla", "Sigur Rós"),
        (r"@@ghjjn\Musiikki\Sigur Rós\Takk\03 Hoppípolla.mp3", "Hoppípolla", "Sigur Rós"),
        (r"shared\Sigur Rós\Inni\06. Hoppípolla.flac", "Hoppípolla", "Sigur Rós"),
        # the old CD3\ mapping bug's path shape: disc folder, backslashes
        (r"danielbasta\Radiohead\Pablo Honey\CD3\18 Radiohead - Creep (Radio Edit).mp3",
         "Creep (Radio Edit)", "Radiohead"),
        (r"Music\Radiohead\OK Computer\CD1\04 Exit Music.flac", "Exit Music", "Radiohead"),
        (r"x\Boards of Canada - Music Has the Right to Children\10. Roygbiv.flac",
         "Roygbiv", "Boards of Canada"),
        (r"1-03 Teardrop.flac", "Teardrop", UNKNOWN_ARTIST),
        (r"Music\1999.mp3", "1999", UNKNOWN_ARTIST),  # a title that is a number stays
        (r"Roygbiv - Boards of Canada.mp3", "Boards of Canada", "Roygbiv"),  # a guess
        # from the test slskd's kept searches (2026-10-30): 2 302 of 24 695
        # right-song paths parse better than with a split on the first " - "
        (r"music\Sigur Rós\Takk… (2005)\12 Vinyl 01\Sigur Rós - Takk… - 02 - Glósóli.flac",
         "Glósóli", "Sigur Rós"),
        (r"music\Sigur Rós - Takk…\Sigur Rós - 03 - Hoppípolla.mp3", "Hoppípolla", "Sigur Rós"),
        (r"x\Sigur Rós - Takk…\01_03 - Hoppípolla.flac", "Hoppípolla", "Sigur Rós"),
        (r"x\Nujabes - Free Soul Nujabes - First Collection - 03 - Feather.mp3",
         "Feather", "Nujabes"),
        (r"MUSIC\EDM\MASSIVE ATTACK\MASSIVE ATTACK - TEARDROP - 08A - 154.mp3",
         "TEARDROP - 08A - 154", "MASSIVE ATTACK"),  # key + BPM: no split there
    ],
)  # fmt: skip
def test_names_from_path(path: str, title: str, artist: str) -> None:
    assert names_from_path(path) == (title, artist)
