"""
The fixed track corpus every pipeline run uses.

Fixed on purpose: which peers are online changes minute to minute, so the
only way run-to-run numbers mean anything is to ask for the *same* thing
every time and let availability be the variable.

Three tiers, chosen for what they stress rather than what they are:

- POPULAR  — well-seeded, plain titles. If these fail, the failure is ours.
- AWKWARD  — feat. clauses, parentheses, non-ASCII, ampersands, long titles.
             These stress query building, the artist post-filter, beets'
             albumartist handling, and path sanitisation.
- RARE     — thin seeding. These stress retry, alternative-peer selection,
             and the "nothing viable" path.

**Run order is POPULAR -> AWKWARD -> RARE** (user decision 2026-08-12): start
where success is expected so a broken pipeline is obvious immediately, and
only then spend wall-clock on the hard cases. **Reporting weight runs the
other way** — a RARE failure says more about the system than a POPULAR one,
so the report ranks findings rare > awkward > popular.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    POPULAR = "popular"
    AWKWARD = "awkward"
    RARE = "rare"


#: Run order. Reporting weight is the reverse — see the module docstring.
RUN_ORDER: tuple[Tier, ...] = (Tier.POPULAR, Tier.AWKWARD, Tier.RARE)

#: Reporting weight per tier, used when ranking findings in the report.
TIER_WEIGHT: dict[Tier, float] = {Tier.POPULAR: 1.0, Tier.AWKWARD: 2.0, Tier.RARE: 3.0}


@dataclass(frozen=True)
class Track:
    """One corpus entry, plus what a correct result would look like.

    `expect_*` are the grading targets for S3 (are the search results
    actually the thing asked for?) and S8 (are the written tags right?).
    They are what the *user* meant, not what any particular peer happens to
    have named the file — the whole point is to catch the gap between those.
    """

    title: str
    artist: str
    tier: Tier
    #: Canonical album artist — no featuring clause. This is what the folder
    #: on disk must be named under the strict-canonical placement spec.
    expect_albumartist: str
    #: Album the track canonically belongs to. `None` where the track is a
    #: single / the album is genuinely ambiguous, in which case S9 grades
    #: only the artist folder and the filename.
    expect_album: str | None
    #: What makes this entry interesting — quoted verbatim in the report so
    #: a failure explains itself.
    stresses: str


CORPUS: tuple[Track, ...] = (
    # -- POPULAR ------------------------------------------------------------
    Track(
        title="Alright",
        artist="Kendrick Lamar",
        tier=Tier.POPULAR,
        expect_albumartist="Kendrick Lamar",
        expect_album="To Pimp a Butterfly",
        stresses="baseline; known to have imported cleanly before, so a "
        "failure here is unambiguously a regression",
    ),
    Track(
        title="All Caps",
        artist="Madvillain",
        tier=Tier.POPULAR,
        expect_albumartist="Madvillain",
        expect_album="Madvillainy",
        stresses="duplicate magnet — many peers, many encodings; the track "
        "that already produced three competing copies in the live tree",
    ),
    Track(
        title="EARFQUAKE",
        artist="Tyler, The Creator",
        tier=Tier.POPULAR,
        expect_albumartist="Tyler, The Creator",
        expect_album="IGOR",
        stresses="all-caps title plus a comma in the artist name; also the "
        "artist whose folder already fragmented three ways",
    ),
    Track(
        title="Smells Like Teen Spirit",
        artist="Nirvana",
        tier=Tier.POPULAR,
        expect_albumartist="Nirvana",
        expect_album="Nevermind",
        stresses="control — maximally available, plain ASCII, no punctuation",
    ),
    # -- AWKWARD ------------------------------------------------------------
    Track(
        title="Write This Down (feat. Nieve)",
        artist="SoulChef",
        tier=Tier.AWKWARD,
        expect_albumartist="SoulChef",
        expect_album="Escapism",
        stresses="parenthesised feat. clause — must not end up as its own "
        "albumartist folder",
    ),
    Track(
        title="Heroes (We Could Be)",
        artist="Alesso feat. Tove Lo",
        tier=Tier.AWKWARD,
        expect_albumartist="Alesso",
        expect_album=None,
        stresses="feat. clause in the *artist* field plus parentheses in the "
        "title; the query builder must not send both verbatim",
    ),
    Track(
        title="Time Moves Slow",
        artist="BADBADNOTGOOD feat. Samuel T. Herring",
        tier=Tier.AWKWARD,
        expect_albumartist="BADBADNOTGOOD",
        expect_album="IV",
        stresses="feat. artist that beets has already mis-promoted to "
        "albumartist in the live searches library",
    ),
    Track(
        title="Jóga",
        artist="Björk",
        tier=Tier.AWKWARD,
        expect_albumartist="Björk",
        expect_album="Homogenic",
        stresses="non-ASCII in both artist and title — query encoding, "
        "filesystem naming, and tag round-tripping all at once",
    ),
    Track(
        title="Everything In Its Right Place",
        artist="Radiohead",
        tier=Tier.AWKWARD,
        expect_albumartist="Radiohead",
        expect_album="Kid A",
        stresses="long multi-word title; exercises the query ladder's word cap",
    ),
    # -- RARE ---------------------------------------------------------------
    Track(
        title="Charlie's Inferno",
        artist="That Handsome Devil",
        tier=Tier.RARE,
        expect_albumartist="That Handsome Devil",
        expect_album="The Heart Goes to Heaven, The Head Goes to Hell",
        stresses="apostrophe in the title, comma in the album; thin seeding. "
        "Already stranded in downloads/complete on the live stack",
    ),
    Track(
        title="ALICE_",
        artist="jev.",
        tier=Tier.RARE,
        expect_albumartist="jev.",
        expect_album="when angels cry",
        stresses="trailing punctuation in both artist and title; niche "
        "release that MusicBrainz may not match at all (asis import path)",
    ),
    Track(
        title="Feather",
        artist="Nujabes",
        tier=Tier.RARE,
        expect_albumartist="Nujabes",
        expect_album="Modal Soul",
        stresses="commonly shared with '[Explicit]' and feat. clauses welded "
        "into the filename; tests filename-vs-tag disagreement",
    ),
)


#: Query-anomaly probe input. Not downloads — these are search-only.
#:
#: User report (2026-08-12): `gorillaz` returned 0 responses while `gorilas`
#: returned several. Established fact so far: unexplained. The probe issues
#: each of these through musica *and* directly against slskd so "musica
#: mangled the query" and "Soulseek returned nothing" are distinguishable.
ANOMALY_QUERIES: tuple[str, ...] = (
    "gorillaz",
    "gorilas",
    "Gorillaz",
    "GORILLAZ",
    "gorillaz feel good",
    "gorillaz demon days",
    "gorilla",
    "gorillas",
    "damon albarn",
)

#: Control artists for the broader zero-result sweep — all household names,
#: every one of which *should* return responses. Any that return zero are as
#: interesting as Gorillaz.
SWEEP_ARTISTS: tuple[str, ...] = (
    "radiohead",
    "nirvana",
    "kendrick lamar",
    "daft punk",
    "the beatles",
    "pink floyd",
    "beyonce",
    "metallica",
    "aphex twin",
    "miles davis",
    "bjork",
    "tyler the creator",
    "kanye west",
    "led zeppelin",
    "portishead",
    "massive attack",
    "the prodigy",
    "fleetwood mac",
    "outkast",
    "burial",
)


def tracks_in_run_order() -> list[Track]:
    """The corpus, popular first and rare last."""
    return [t for tier in RUN_ORDER for t in CORPUS if t.tier is tier]


def tracks_of_tier(tier: Tier) -> list[Track]:
    return [t for t in CORPUS if t.tier is tier]
