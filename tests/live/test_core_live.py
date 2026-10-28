"""The core matcher and query strategy on real Soulseek results (#90, #91).

Names and lengths come from the track on the album's earliest official
MusicBrainz edition, as the catalog service will pick them; each song runs the L0 → TA → T
ladder on the test slskd until a query finds a helpful peer. Runs only with
``PV_LIVE=1`` and the test stack up (``.env.test`` sourced):

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_core_live.py -v -s

With ``-s`` it prints rejection counts and a few verdicts per song for a hand
check; peer names and paths are never written to a file (public repo).
"""

from __future__ import annotations

import os
import random
import time
from collections import Counter

import pytest

from app.clients.catalog import Track
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.slskd import PeerResponse, SlskdClient
from app.core import normalize
from app.core.matcher import Song, label_file
from app.core.strategy import track_queries
from tests.live.test_clients_integration import SLSKD_URL

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)

# (album, artist, track) to look up; the names used are MusicBrainz's. Chosen
# for what they exercise: plain, accents, non-Latin, a version in the title.
SONGS = [
    ("OK Computer", "Radiohead", "Karma Police"),
    ("Takk", "Sigur Rós", "Hoppípolla"),
    ("Группа крови", "Кино", "Группа крови"),
    # many recordings of one song (295–356 s): matched against the chosen one
    (
        "Владимир Высоцкий в записях Михаила Шемякина",
        "Владимир Высоцкий",
        "Кони привередливые",
    ),
    ("7", "Lil Nas X", "Old Town Road (Remix)"),
]


@pytest.fixture(scope="module")
def slskd() -> SlskdClient:
    client = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    deadline = time.monotonic() + 120
    while not (state := client.app_state()).logged_in or not state.has_parent:
        assert time.monotonic() < deadline, f"test slskd not ready: {state}"
        time.sleep(3)
    return client


def _album_track(mb: MusicBrainzClient, album: str, artist: str, title: str) -> Track:
    """The track on the album's earliest official (else non-bootleg) edition."""
    albums = [
        a
        for a in mb.search_albums(album, artist, 100)
        if a.primary_type in ("Album", "EP")
        and not a.secondary_types
        and a.title.casefold().startswith(album.casefold())
    ]
    assert albums, f"no MusicBrainz album {artist} – {album}"
    first = min(albums, key=lambda a: a.first_release_date or "9999")
    editions = mb.album_editions(first.id)
    # official first; some albums only have editions without a status
    editions = [e for e in editions if e.status == "Official"] or [
        e for e in editions if e.status != "Bootleg"
    ]
    assert editions, f"no usable edition of {first.title}"
    edition = min(editions, key=lambda e: e.date or "9999")
    tracks = [
        t
        for t in mb.edition_tracks(edition.id)
        if t.title.casefold() == title.casefold() and t.duration_s
    ]
    assert tracks, f"{title} not on {first.title} ({edition.id})"
    return tracks[0]


def _search(slskd: SlskdClient, query: str) -> list[PeerResponse]:
    search_id = slskd.start_search(query, 300, 3)
    deadline = time.monotonic() + 60
    while not slskd.get_search(search_id).is_complete:
        if time.monotonic() > deadline:
            slskd.cancel_search(search_id)
            time.sleep(2)
            break
        time.sleep(1)
    return slskd.get_responses(search_id)


@pytest.mark.parametrize(("album", "artist", "title"), SONGS)
def test_ladder_finds_helpful_peer(
    slskd: SlskdClient, album: str, artist: str, title: str
) -> None:
    track = _album_track(MusicBrainzClient(), album, artist, title)
    credits = [normalize.Credit(track.artist)]
    names = normalize.query_names(track.title, credits, None, track.artist)
    song = Song(names.track, names.artist, album, track.duration_s)

    for shape, query in track_queries(names.track, names.artist):
        responses = _search(slskd, query)
        reasons: Counter[str] = Counter()
        correct: list[str] = []
        rejected: list[tuple[str, str]] = []
        helpful = 0
        for peer in responses:
            hit = False
            for f in peer.files:
                verdict = label_file(f.filename, f.length, song, locked=f.locked)
                reasons[verdict.split(":")[0]] += 1
                if verdict == "correct":
                    hit = True
                    correct.append(f.filename)
                elif verdict not in ("not_audio", "no_artist", "no_title"):
                    rejected.append((verdict, f.filename))
            helpful += hit
        print(
            f"\n{artist} – {title} [{shape}] {query!r}: {len(responses)} peers, "
            f"{helpful} helpful; {dict(reasons)}; MB length {track.duration_s}s"
        )
        rng = random.Random(0)
        for name in rng.sample(correct, min(5, len(correct))):
            print(f"  correct   {name}")
        for verdict, name in rng.sample(rejected, min(5, len(rejected))):
            print(f"  {verdict:<9} {name}")
        if helpful:
            return
    pytest.fail(f"no helpful peer for {artist} – {title}")
