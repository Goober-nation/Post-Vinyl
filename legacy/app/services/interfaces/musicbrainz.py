"""
MusicBrainzService — Abstract base class for canonical-metadata lookups.

This is the contract Phase 6.8 (P-MB-1..4) is built on, written up front so
the pieces that need it sooner — beets import constraint first — don't have
to be retrofitted later.

**Why this exists at all.** Everything musica learns about a file today comes
from a Soulseek peer: the filename, and whatever tags happen to be embedded.
That is untrusted, frequently wrong, and measurably so — the 2026-08-12 live
run found beets importing Björk's "Jóga" as `Various Artists / LateNightTales`
and Radiohead's "Everything In Its Right Place" as the live version off
`I Might Be Wrong`, because those are the identities the downloaded files
claimed. Tags, folder names, and Navidrome's display are all derived from
that one wrong fact, which is why a single bad match shows up as three
separate-looking bugs.

MusicBrainz is the authority that replaces the guess. musica knows what the
user *asked* for; this interface turns that intent into canonical metadata,
so the pipeline stops taking a stranger's word for what a file is.

**Release types are the point, not an extra.** `MBRelease.secondary_types`
distinguishes a studio album from a live record, a compilation, or a DJ mix —
the exact distinction every one of the observed mis-taggings got wrong. See
`MBRelease.is_canonical_studio`.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

#: Secondary release types that mean "this is not the canonical studio
#: release of this recording". Ordered loosely by how badly a user asking for
#: an album track would be surprised to receive one.
NON_CANONICAL_SECONDARY_TYPES: frozenset[str] = frozenset(
    {
        "Live",
        "Compilation",
        "DJ-mix",
        "Mixtape/Street",
        "Remix",
        "Demo",
        "Interview",
        "Audiobook",
        "Audio drama",
        "Spokenword",
    }
)

#: Primary types a user asking for "the song/album" is happy to receive. This
#: deliberately keeps Singles and EPs — a single can be the canonical release
#: of a track — while excluding Broadcast/Other (podcasts, audiobooks, misc).
OFFICIAL_PRIMARY_TYPES: frozenset[str] = frozenset({"Album", "Single", "EP"})


@dataclass
class MBArtist:
    """An artist, as MusicBrainz knows them.

    `sort_name` and `disambiguation` exist for P-MB-3's discography browser,
    where "Nirvana (US grunge band)" has to be distinguishable from the three
    other Nirvanas.
    """

    mbid: str
    name: str
    sort_name: str | None = None
    disambiguation: str | None = None
    #: MusicBrainz search score, 0-100. None for lookups (not searches).
    score: int | None = None


@dataclass
class MBReleaseGroup:
    """A release group — the album an artist intended, releases bundled under.

    MusicBrainz groups many physical releases (original, reissue, remaster,
    region-specific) under one release group. When the user asks for an
    album, the *group* is what they mean; the *release* is which pressing.
    P-MB-2's album search returns these, and `lookup_release_group_tracks`
    turns one into its canonical track list.
    """

    mbid: str
    title: str
    #: Primary/album artist alone, no featuring clause (matches `MBRecording.artist`).
    artist: str
    artist_mbid: str | None = None
    #: "Album", "Single", "EP", "Broadcast", "Other", or None.
    primary_type: str | None = None
    #: Year of the group's first release, when known.
    year: int | None = None
    #: "Live", "Compilation", "DJ-mix", ... — same set as `MBRelease`.
    secondary_types: list[str] = field(default_factory=list)
    #: Number of releases/pressings in this group. This is a catalog-size
    #: signal, not a listener/popularity count.
    release_count: int | None = None
    #: MusicBrainz text relevance score, 0-100. None for non-search results.
    score: int | None = None

    @property
    def is_official(self) -> bool:
        """True when this release group is an official album/single/EP.

        The release-group-level "official" test (no status field exists on a
        release group, so only primary type and secondary types are available
        here): a mixtape, live album, compilation, DJ-mix, demo or remix is
        filtered out, but a Single or EP is kept. Unknown primary type is
        treated as non-official, mirroring `MBRelease.is_canonical_studio`.
        """
        if self.primary_type not in OFFICIAL_PRIMARY_TYPES:
            return False
        return not (set(self.secondary_types) & NON_CANONICAL_SECONDARY_TYPES)


@dataclass
class MBRelease:
    """A release (album, single, EP...) a recording appears on."""

    mbid: str
    title: str
    #: "Album", "Single", "EP", "Broadcast", "Other", or None.
    primary_type: str | None = None
    #: "Live", "Compilation", "DJ-mix", ... — may hold several at once.
    secondary_types: list[str] = field(default_factory=list)
    date: str | None = None
    country: str | None = None
    artist_credit: str | None = None
    #: Position of the recording within this release, when known.
    track_number: int | None = None
    track_count: int | None = None
    #: MusicBrainz release status ("Official", "Bootleg", "Promotion",
    #: "Pseudo-Release", ...). Only set where the API returns it (search/
    #: browse/lookup all do); the release-group *lookup* path threads the
    #: group in without a per-release status.
    status: str | None = None
    #: The parent release group's MBID. Present on search/browse results
    #: (where the release-group is embedded); None on the release-group
    #: *lookup* path, which threads only type/status through. This is what
    #: keys cover art on the Cover Art Archive.
    release_group_mbid: str | None = None

    @property
    def is_official(self) -> bool:
        """True when this release is an official album/single/EP.

        Stricter than `is_official` on a release group: it also requires
        `status == "Official"`, which is the field that separates a bootleg
        or promotional pressing from the real thing. Only a release carries
        status, so this is the one place the bootleg check can live.
        """
        if self.status != "Official":
            return False
        if self.primary_type not in OFFICIAL_PRIMARY_TYPES:
            return False
        return not (set(self.secondary_types) & NON_CANONICAL_SECONDARY_TYPES)

    @property
    def is_canonical_studio(self) -> bool:
        """True when this is a studio album rather than a live/comp/mix.

        This is the single most important predicate in the module. Asking for
        "Everything In Its Right Place" and receiving the `I Might Be Wrong`
        live version is not a tagging bug downstream — it is this question
        never having been asked.

        A release with no `primary_type` at all is treated as non-canonical:
        unknown provenance is exactly the case where trusting the peer got us
        here in the first place.
        """
        if self.primary_type != "Album":
            return False
        return not (set(self.secondary_types) & NON_CANONICAL_SECONDARY_TYPES)

    @property
    def year(self) -> int | None:
        if not self.date:
            return None
        head = self.date.split("-", 1)[0]
        return int(head) if head.isdigit() else None


@dataclass
class MBRecording:
    """A recording — the thing a downloaded audio file actually *is*.

    Deliberately a recording and not a track: the same recording appears on
    many releases, and picking the right release among them is what
    `best_release` does.
    """

    mbid: str
    title: str
    #: Display artist, including featured artists ("Alesso feat. Tove Lo").
    artist_credit: str
    #: Primary/album artist alone, no featuring clause ("Alesso"). This is
    #: what a folder should be named after — the live run found featured
    #: artists promoted into albumartist, splitting one artist across three
    #: directories.
    artist: str
    artist_mbid: str | None = None
    length_ms: int | None = None
    disambiguation: str | None = None
    score: int | None = None
    releases: list[MBRelease] = field(default_factory=list)

    @property
    def best_release(self) -> MBRelease | None:
        """The release this recording should be filed under.

        Canonical studio albums first, then earliest — the original album
        rather than a later reissue or box set. Falls back to the earliest
        release of any type rather than returning None, because a recording
        that only ever appeared on a live album still has to go somewhere.
        """
        if not self.releases:
            return None
        studio = [r for r in self.releases if r.is_canonical_studio]
        pool = studio or self.releases
        return min(pool, key=lambda r: (r.year is None, r.year or 0, r.title))

    @property
    def is_live(self) -> bool:
        """True when *every* known release of this recording is non-canonical.

        Not the same as "the best release is live": a studio recording that
        also appears on a live compilation is still a studio recording.
        """
        return bool(self.releases) and not any(
            r.is_canonical_studio for r in self.releases
        )

    @property
    def is_official(self) -> bool:
        """True when this recording appears on at least one official release.

        A "has any official release" test, not "is only on official releases"
        — the right shape for deciding whether to *show* a recording, where a
        false positive just means one extra row, but a false negative hides a
        canonical track. `search_recording(official_only=True)` filters on this
        predicate after a buffered fetch; the recording search response carries
        each recording's full release list, so the test is reliable there.
        """
        return any(r.is_official for r in self.releases)


class MusicBrainzService(ABC):
    """Canonical metadata lookups against MusicBrainz.

    Implementations:
    - `MusicBrainzClient` (app/services/musicbrainz_client.py) — the real
      HTTP client, rate-limited to MusicBrainz's published 1 req/sec.

    Every method returns plain dataclasses, never raw JSON, so callers do not
    grow a dependency on MusicBrainz's response shape. Implementations raise
    `MusicBrainzConnectionError` for transport failures and
    `MusicBrainzRateLimitError` when throttled; a search that legitimately
    finds nothing returns an empty list, which is not an error.
    """

    @abstractmethod
    def search_recording(
        self,
        title: str,
        artist: str | None = None,
        limit: int = 10,
        official_only: bool = False,
    ) -> list[MBRecording]:
        """Find recordings matching a title (and optionally an artist).

        `official_only` filters the results to recordings with at least one
        official release (an `is_official` release: status Official, an
        album/single/EP primary type, and no live/compilation/demo/remix
        secondary type). The primary entry point for constraining a beets
        import: musica knows what was asked for, and this turns it into a
        recording MBID.
        """

    @abstractmethod
    def search_release(
        self, title: str, artist: str | None = None, limit: int = 10
    ) -> list[MBRelease]:
        """Find releases by title — P-MB-2's album search."""

    @abstractmethod
    def search_release_group(
        self,
        title: str,
        artist: str | None = None,
        limit: int = 10,
        official_only: bool = False,
    ) -> list[MBReleaseGroup]:
        """Find release groups by title — P-MB-2's album search.

        `official_only` filters the returned groups to `is_official` ones
        (albums/singles/EPs, no mixtape/live/compilation/demo/remix).
        """

    @abstractmethod
    def search_artist(self, name: str, limit: int = 10) -> list[MBArtist]:
        """Find artists by name — P-MB-3's discography entry point."""

    @abstractmethod
    def browse_artist_releases(
        self, artist_mbid: str, limit: int = 100
    ) -> list[MBRelease]:
        """An artist's releases — P-MB-3's discography listing."""

    @abstractmethod
    def browse_artist_release_groups(
        self, artist_mbid: str, limit: int = 100, official_only: bool = False
    ) -> list[MBReleaseGroup]:
        """An artist's release groups — P-MB-3's discography listing.

        `official_only` filters to `is_official` groups, which is what keeps
        an artist's discography free of mixtapes, bootlegs and live albums.
        """

    @abstractmethod
    def lookup_recording(self, mbid: str) -> MBRecording | None:
        """Fetch one recording by MBID, with its releases. None if unknown."""

    @abstractmethod
    def lookup_release_group_tracks(self, release_group_mbid: str) -> list[MBRecording]:
        """The track list of a release group's canonical release, in order.

        Empty when the group is unknown or has no usable release. This is the
        bridge between "the album the user picked" and "the recordings to
        fetch from Soulseek".
        """

    @abstractmethod
    def lookup_release_tracks(
        self, release_mbid: str
    ) -> tuple[str, list[MBRecording]] | None:
        """The canonical title and ordered track list of one release.

        Unlike `lookup_release_group_tracks` (which takes a release-group
        MBID and re-selects the canonical release), this takes an exact
        *release* MBID — the `mb_albumid` beets rows carry — and returns
        the release's title plus its recordings in release order (media
        position, then track position), so the position of each recording
        is `index + 1`. None when the release is unknown.

        This is the album-consolidation bridge: given a release, musica can
        renumber tracks from the authoritative tracklist and re-tag files
        with the canonical album title.
        """

    @abstractmethod
    def resolve_canonical(
        self, title: str, artist: str, min_score: int = 90
    ) -> MBRecording | None:
        """The intent-to-canonical resolver the import path needs.

        Returns the single best studio recording matching this artist/title,
        or None when MusicBrainz has no confident answer. Returning None is a
        legitimate, expected outcome — the caller must fall back to importing
        the file as-is and flagging it, never to guessing. A wrong canonical
        answer is worse than no answer, because it would be applied silently.
        """
