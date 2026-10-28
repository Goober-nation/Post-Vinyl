"""The clients working together on real services (milestone 3).

Runs only with ``PV_LIVE=1`` and the isolated test stack up (test slskd and
test Navidrome, credentials from ``.env.test``); never the real ``.env``:

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live -v
"""

from __future__ import annotations

import os
import time

import pytest
import requests

from app.clients.catalog import CatalogNotFound
from app.clients.deezer import DeezerClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.navidrome import NavidromeClient, Session
from app.clients.slskd import SlskdClient

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
SLSKD_URL = os.environ.get("PV_TEST_SLSKD_URL", "http://127.0.0.1:8191")
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")
LENGTH_TOLERANCE_S = 3  # the matcher's duration rule (design notes §3)


@pytest.fixture(scope="module")
def mb() -> MusicBrainzClient:
    return MusicBrainzClient()


@pytest.fixture(scope="module")
def dz() -> DeezerClient:
    return DeezerClient()


@pytest.fixture(scope="module")
def slskd() -> SlskdClient:
    client = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    deadline = time.monotonic() + 120
    while not (state := client.app_state()).logged_in or not state.has_parent:
        assert time.monotonic() < deadline, f"test slskd not ready: {state}"
        time.sleep(3)
    return client


@pytest.fixture(scope="module")
def navidrome() -> tuple[NavidromeClient, Session]:
    client = NavidromeClient(NAVIDROME_URL)
    session = client.login(
        os.environ["NAVIDROME_USERNAME"], os.environ["NAVIDROME_PASSWORD"]
    )
    return client, session


def test_deezer_and_musicbrainz_agree_by_isrc(
    mb: MusicBrainzClient, dz: DeezerClient
) -> None:
    """A Deezer chart song resolves to the same song in MusicBrainz by ISRC."""
    agreed = 0
    for chart_track in dz.chart_tracks(10)[:5]:
        deezer_song = dz.get_track(chart_track.id)
        try:
            recordings = mb.tracks_by_isrc(deezer_song.isrcs[0])
        except CatalogNotFound:  # chart songs can be too new for MusicBrainz
            continue
        lengths = [r.duration_s for r in recordings if r.duration_s]
        assert deezer_song.duration_s is not None
        if any(abs(x - deezer_song.duration_s) <= LENGTH_TOLERANCE_S for x in lengths):
            agreed += 1
    assert agreed >= 2, f"only {agreed} of 5 chart songs matched across catalogs"


def test_catalog_song_found_on_soulseek_with_matching_length(
    mb: MusicBrainzClient, slskd: SlskdClient
) -> None:
    """Search slskd with catalog names; some files must match the length."""
    [song] = [
        t
        for t in mb.tracks_by_isrc("USEE10001994")  # a known MusicBrainz ISRC
        if t.duration_s
    ][:1]
    search_id = slskd.start_search(f"{song.title} {song.artist}", 300, 3)
    deadline = time.monotonic() + 60
    while not slskd.get_search(search_id).is_complete:
        if time.monotonic() > deadline:
            slskd.cancel_search(search_id)
            time.sleep(2)
            break
        time.sleep(1)
    files = [f for r in slskd.get_responses(search_id) for f in r.files]
    assert song.duration_s is not None
    matching = [
        f
        for f in files
        if f.length is not None
        and abs(f.length - song.duration_s) <= LENGTH_TOLERANCE_S
    ]
    assert len(matching) >= 3, (
        f"{len(files)} files, {len(matching)} with matching length"
    )


def test_library_song_found_in_both_catalogs(
    navidrome: tuple[NavidromeClient, Session],
    mb: MusicBrainzClient,
    dz: DeezerClient,
) -> None:
    """A song in the test library resolves by path, then in both catalogs."""
    client, session = navidrome
    # discover any library song (the client has no list call by design)
    rows = requests.get(
        f"{NAVIDROME_URL}/api/song",
        params={"_end": 1},
        headers={"X-ND-Authorization": f"Bearer {session.jwt}"},
        timeout=15,
    ).json()
    assert rows, "test library is empty"
    song = client.find_song_by_path(session, rows[0]["path"])
    assert song is not None and song.id == rows[0]["id"]
    title = song.title.split(" (")[0]  # drop a version suffix like "(Radio Edit)"
    for catalog in (mb, dz):
        # search ranking varies between calls (MusicBrainz), so look deep
        hits = catalog.search_tracks(title, song.artist, 100)
        assert any(
            t.duration_s and abs(t.duration_s - song.duration) <= LENGTH_TOLERANCE_S
            for t in hits
        ), f"{catalog.source}: no {title} near {song.duration:.0f}s"


@pytest.fixture(scope="module")
def lb() -> ListenBrainzClient:
    return ListenBrainzClient()


def test_listenbrainz_metadata_agrees_with_musicbrainz(
    mb: MusicBrainzClient, lb: ListenBrainzClient
) -> None:
    """MB recordings resolve in LB with the same title and length."""
    songs = mb.search_tracks("Kickstart My Heart", "Mötley Crüe", 100)
    meta = lb.recording_metadata([t.id for t in songs])
    assert meta, "no MusicBrainz recording known to ListenBrainz"
    for song in songs:
        if song.id in meta and song.duration_s and meta[song.id].length_s:
            assert meta[song.id].title == song.title
            assert abs((meta[song.id].length_s or 0) - song.duration_s) <= 1


def test_listenbrainz_lookup_maps_deezer_song_to_musicbrainz(
    dz: DeezerClient, mb: MusicBrainzClient, lb: ListenBrainzClient
) -> None:
    """The fallback path: Deezer names -> LB lookup -> a real MB recording."""
    token = os.environ.get("LISTENBRAINZ_TOKEN")
    if not token:
        pytest.skip("LISTENBRAINZ_TOKEN not set in .env.test")
    assert lb.validate_token(token).valid
    [song] = dz.search_tracks("Kickstart My Heart", "Mötley Crüe", 1)
    found = lb.lookup_recording(song.artist, song.title, token)
    assert found is not None
    recording = mb.get_track(found.mbid)
    assert recording.title.lower() == song.title.lower()
