"""
Unit tests for the MusicBrainz client (P-MB-1).

Mocked responses throughout — no network, so these run anywhere and cannot
be flaky. The fixtures are deliberately the **real failures from the
2026-08-12 live run**, not invented data:

- Radiohead's "Everything In Its Right Place" imported as the live version
  off `I Might Be Wrong` instead of the `Kid A` album track.
- Björk's "Jóga" imported as `Various Artists / LateNightTales`, a DJ-mix
  compilation, instead of `Homogenic`.
- "Alesso feat. Tove Lo" promoted whole into albumartist, splitting one
  artist across directories.

If this module ever goes green while those cases regress, the tests are
wrong, not the run.
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
import requests

from app.exceptions import MusicBrainzConnectionError, MusicBrainzRateLimitError
from app.services.interfaces.musicbrainz import MBRecording, MBRelease, MBReleaseGroup
from app.services.musicbrainz_client import (
    MusicBrainzClient,
    _artist_credit_display,
    _primary_artist,
    _RateLimiter,
    _TTLCache,
    escape_lucene,
)


class _Cfg:
    """Minimal stand-in for the musicbrainz config section."""

    class musicbrainz:
        enabled = True
        url = "https://musicbrainz.test"
        timeout_seconds = 5
        min_request_interval = 0.0  # no real sleeping in unit tests
        cache_ttl_seconds = 3600
        min_score = 90
        version = "test"


def make_client(responses: list[dict] | None = None, status: int = 200):
    client = MusicBrainzClient(_Cfg())
    if responses is not None:
        payloads = list(responses)

        def _get(*_a, **_kw):
            resp = Mock()
            resp.status_code = status
            resp.headers = {}
            resp.json.return_value = payloads.pop(0) if payloads else {}
            return resp

        client._session.get = Mock(side_effect=_get)
    return client


# ---------------------------------------------------------------------------
# Query escaping
# ---------------------------------------------------------------------------


class TestEscapeLucene:
    def test_escapes_characters_lucene_treats_as_syntax(self):
        assert escape_lucene("ALICE_") == "ALICE_"  # underscore is not special
        assert escape_lucene("Write This Down (feat. Nieve)") == (
            "Write This Down \\(feat. Nieve\\)"
        )
        assert escape_lucene("AC/DC") == "AC\\/DC"
        assert escape_lucene("50/50 - Remix") == "50\\/50 \\- Remix"

    def test_leaves_non_ascii_alone(self):
        """Björk and Jóga must survive unmangled — escaping is about Lucene
        syntax, not about characters being unfamiliar."""
        assert escape_lucene("Björk") == "Björk"
        assert escape_lucene("Jóga") == "Jóga"

    def test_query_is_built_with_escaped_values(self):
        client = make_client([{"recordings": []}])
        client.search_recording("Heroes (We Could Be)", "Alesso")
        params = client._session.get.call_args.kwargs["params"]
        assert "\\(" in params["query"]
        assert 'artist:"Alesso"' in params["query"]


# ---------------------------------------------------------------------------
# Artist credit parsing — the feat. problem
# ---------------------------------------------------------------------------


ALESSO_CREDIT = [
    {
        "name": "Alesso",
        "joinphrase": " feat. ",
        "artist": {"id": "alesso-mbid", "name": "Alesso"},
    },
    {
        "name": "Tove Lo",
        "joinphrase": "",
        "artist": {"id": "tove-mbid", "name": "Tove Lo"},
    },
]


class TestArtistCredits:
    def test_display_credit_keeps_the_featured_artist(self):
        assert _artist_credit_display(ALESSO_CREDIT) == "Alesso feat. Tove Lo"

    def test_primary_artist_drops_the_featured_artist(self):
        """The folder-naming fix: 'Alesso', not 'Alesso feat. Tove Lo'."""
        name, mbid = _primary_artist(ALESSO_CREDIT)
        assert name == "Alesso"
        assert mbid == "alesso-mbid"

    def test_empty_credits_do_not_explode(self):
        assert _artist_credit_display([]) == ""
        assert _primary_artist([]) == ("", None)


# ---------------------------------------------------------------------------
# Release classification — the heart of the fix
# ---------------------------------------------------------------------------


class TestReleaseClassification:
    def test_plain_studio_album_is_canonical(self):
        assert MBRelease("r", "Kid A", primary_type="Album").is_canonical_studio

    @pytest.mark.parametrize(
        "secondary", [["Live"], ["Compilation"], ["DJ-mix"], ["Live", "Compilation"]]
    )
    def test_live_and_compilation_albums_are_not_canonical(self, secondary):
        release = MBRelease("r", "x", primary_type="Album", secondary_types=secondary)
        assert not release.is_canonical_studio

    @pytest.mark.parametrize("primary", ["Single", "EP", "Broadcast", "Other", None])
    def test_non_album_primary_types_are_not_canonical(self, primary):
        assert not MBRelease("r", "x", primary_type=primary).is_canonical_studio

    def test_year_parses_from_partial_dates(self):
        assert MBRelease("r", "x", date="2000-10-02").year == 2000
        assert MBRelease("r", "x", date="2000").year == 2000
        assert MBRelease("r", "x", date=None).year is None
        assert MBRelease("r", "x", date="unknown").year is None


KID_A = MBRelease("kid-a", "Kid A", primary_type="Album", date="2000-10-02")
I_MIGHT_BE_WRONG = MBRelease(
    "imbw",
    "I Might Be Wrong",
    primary_type="Album",
    secondary_types=["Live"],
    date="2001-11-12",
)
LATENIGHTTALES = MBRelease(
    "lnt",
    "LateNightTales",
    primary_type="Album",
    secondary_types=["Compilation", "DJ-mix"],
    date="2011-01-01",
)
HOMOGENIC = MBRelease("homo", "Homogenic", primary_type="Album", date="1997-09-22")


class TestBestRelease:
    def test_studio_album_beats_the_live_album(self):
        """The Radiohead case, exactly as it failed live."""
        rec = MBRecording(
            "x",
            "Everything In Its Right Place",
            "Radiohead",
            "Radiohead",
            releases=[I_MIGHT_BE_WRONG, KID_A],
        )
        assert rec.best_release is KID_A

    def test_studio_album_beats_the_dj_mix_compilation(self):
        """The Björk case: Homogenic, not LateNightTales."""
        rec = MBRecording(
            "x", "Jóga", "Björk", "Björk", releases=[LATENIGHTTALES, HOMOGENIC]
        )
        assert rec.best_release is HOMOGENIC

    def test_earliest_studio_release_wins_over_a_reissue(self):
        reissue = MBRelease("re", "Kid A", primary_type="Album", date="2020-01-01")
        rec = MBRecording("x", "t", "a", "a", releases=[reissue, KID_A])
        assert rec.best_release is KID_A

    def test_falls_back_rather_than_returning_nothing(self):
        """A recording that only ever appeared live still has to go
        somewhere — returning None would strand the file."""
        rec = MBRecording("x", "t", "a", "a", releases=[I_MIGHT_BE_WRONG])
        assert rec.best_release is I_MIGHT_BE_WRONG

    def test_no_releases_is_none(self):
        assert MBRecording("x", "t", "a", "a").best_release is None

    def test_is_live_only_when_every_release_is_non_canonical(self):
        assert MBRecording("x", "t", "a", "a", releases=[I_MIGHT_BE_WRONG]).is_live
        assert not MBRecording(
            "x", "t", "a", "a", releases=[I_MIGHT_BE_WRONG, KID_A]
        ).is_live
        assert not MBRecording("x", "t", "a", "a").is_live


# ---------------------------------------------------------------------------
# resolve_canonical — what the import path actually calls
# ---------------------------------------------------------------------------


def recording_payload(title, artist, score, releases):
    return {
        "id": f"{title}-mbid",
        "title": title,
        "score": score,
        "artist-credit": [{"name": artist, "artist": {"id": "a", "name": artist}}],
        "releases": releases,
    }


LIVE_RELEASE_JSON = {
    "id": "imbw",
    "title": "I Might Be Wrong",
    "date": "2001",
    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
}
STUDIO_RELEASE_JSON = {
    "id": "kid-a",
    "title": "Kid A",
    "date": "2000",
    "release-group": {"primary-type": "Album", "secondary-types": []},
}


class TestResolveCanonical:
    def test_prefers_the_studio_recording_over_the_live_one(self):
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "Everything In Its Right Place",
                            "Radiohead",
                            100,
                            [LIVE_RELEASE_JSON],
                        ),
                        recording_payload(
                            "Everything In Its Right Place",
                            "Radiohead",
                            95,
                            [STUDIO_RELEASE_JSON],
                        ),
                    ]
                }
            ]
        )
        best = client.resolve_canonical("Everything In Its Right Place", "Radiohead")
        assert best is not None
        assert best.best_release.title == "Kid A"

    def test_discards_matches_below_the_confidence_floor(self):
        """A weak match applied silently is worse than no match: the file
        still gets filed, just under a confident-looking wrong name."""
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "Something Else", "Someone", 42, [STUDIO_RELEASE_JSON]
                        )
                    ]
                }
            ]
        )
        assert client.resolve_canonical("Jóga", "Björk") is None

    def test_no_results_is_none_not_an_error(self):
        client = make_client([{"recordings": []}])
        assert client.resolve_canonical("Nonexistent", "Nobody") is None

    def test_musicbrainz_being_down_degrades_to_none(self):
        """Must never fail the download that already succeeded — the file is
        on disk, MusicBrainz is just unavailable to name it."""
        client = make_client()
        client._session.get = Mock(side_effect=requests.exceptions.ConnectionError("x"))
        assert client.resolve_canonical("Jóga", "Björk") is None

    def test_disabled_client_resolves_to_none_without_calling_out(self):
        client = make_client([{"recordings": []}])
        client.enabled = False
        assert client.resolve_canonical("Jóga", "Björk") is None
        client._session.get.assert_not_called()


# ---------------------------------------------------------------------------
# Transport behaviour
# ---------------------------------------------------------------------------


class TestCanonicalFilter:
    """The query-level filter, measured against the real API on 2026-08-12.

    Without it the top results for Nirvana / "Smells Like Teen Spirit" were a
    promo sampler and two bootlegs, all scoring 100. With it: `Nevermind`.
    """

    def test_resolve_biases_toward_official_studio_albums(self):
        client = make_client([{"recordings": []}])
        client.resolve_canonical("Smells Like Teen Spirit", "Nirvana")
        query = client._session.get.call_args.kwargs["params"]["query"]
        assert "status:official" in query
        assert "primarytype:album" in query

    def test_resolve_does_not_negate_secondarytype(self):
        """The bug this filter used to have: MusicBrainz pools
        `secondarytype` across every release a recording appears on, so
        `-secondarytype:compilation` excludes the whole recording — the
        correct studio release included — the moment that recording *also*
        appears on any compilation. Verified against the live API on
        2026-08-12: Madvillain's "All Caps" disappeared entirely once that
        clause was added, despite its `Madvillainy` release having no
        secondary type at all. The negation must never come back."""
        client = make_client([{"recordings": []}])
        client.resolve_canonical("All Caps", "Madvillain")
        query = client._session.get.call_args.kwargs["params"]["query"]
        assert "-secondarytype" not in query
        assert "secondarytype:" not in query

    def test_plain_search_is_left_unfiltered(self):
        """A user hunting for a live album must still be able to find one —
        the filter belongs to the import path, not to search itself."""
        client = make_client([{"recordings": []}])
        client.search_recording("Smells Like Teen Spirit", "Nirvana")
        query = client._session.get.call_args.kwargs["params"]["query"]
        assert "status:official" not in query

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Madvillainy", False),
            ("Madvillainy Instrumentals", True),
            ("Escapism", False),
            ("Escapism: Instrumentals", True),
            ("Madvillainy Demos", True),
            ("Kid A", False),
            ("The Sounds Alive Promotion Sampler", True),
        ],
    )
    def test_variant_editions_are_recognised(self, title, expected):
        from app.services.musicbrainz_client import looks_like_variant

        assert looks_like_variant(title) is expected

    def test_variant_penalty_outranks_score(self):
        """The Madvillain case: the instrumentals edition scored 100 and the
        album scored 99, so sorting on score first picked the wrong one."""
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "All Caps",
                            "Madvillain",
                            100,
                            [
                                {
                                    "id": "i",
                                    "title": "Madvillainy Instrumentals",
                                    "date": "2004",
                                    "release-group": {
                                        "primary-type": "Album",
                                        "secondary-types": [],
                                    },
                                }
                            ],
                        ),
                        recording_payload(
                            "All Caps",
                            "Madvillain",
                            99,
                            [
                                {
                                    "id": "m",
                                    "title": "Madvillainy",
                                    "date": "2004",
                                    "release-group": {
                                        "primary-type": "Album",
                                        "secondary-types": [],
                                    },
                                }
                            ],
                        ),
                    ]
                }
            ]
        )
        best = client.resolve_canonical("All Caps", "Madvillain")
        assert best is not None
        assert best.best_release.title == "Madvillainy"

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Feather (N0ms Bootleg)", True),
            ("Feather", False),
            ("Some Song (VIP Remix)", True),
            ("Some Song (Extended Mashup)", True),
            ("Some Song (Acoustic Rework)", True),
            ("A Tribute to Someone", True),
            ("Radio Edit", False),  # deliberately not a marker — see VARIANT_MARKERS
            ("Undercover Lover", False),  # "cover" substring must not false-positive
        ],
    )
    def test_variant_markers_cover_bootleg_style_recording_titles(
        self, title, expected
    ):
        from app.services.musicbrainz_client import looks_like_variant

        assert looks_like_variant(title) is expected

    def test_near_miss_studio_recording_beats_a_confident_bootleg(self):
        """Live-verified case (2026-08-12): MusicBrainz's own text relevance
        scored the real Nujabes - Feather recording ("Nujabes featuring Cise
        Starr & Akin from CYNE") at 77 — below min_score=90 — purely for
        having a longer, more complete artist-credit than a bootleg's bare
        "Nujabes" (100). The bootleg has no canonical studio release at all,
        yet won by being the only candidate that cleared the confidence bar.
        The real recording, 13 points under the bar with a genuine studio
        release, must win instead."""
        bootleg_release = {
            "id": "peachboiz",
            "title": "Peachboiz Vol. 1",
            "date": None,
            "release-group": {
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        }
        studio_release = {
            "id": "modal-soul",
            "title": "Modal Soul",
            "date": "2005-11-11",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        }
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "Feather (N0ms Bootleg)", "Nujabes", 100, [bootleg_release]
                        ),
                        recording_payload("Feather", "Nujabes", 77, [studio_release]),
                    ]
                }
            ]
        )

        best = client.resolve_canonical("Feather", "Nujabes", min_score=90)

        assert best is not None
        assert best.best_release.title == "Modal Soul"

    def test_near_miss_fallback_has_a_floor(self):
        """The near-miss studio fallback must not swallow every possible
        score gap — a recording scoring far below min_score is still
        discarded, even with a canonical studio release, and the confident
        (if worse) candidate wins rather than an essentially-unrelated
        guess."""
        from app.services.musicbrainz_client import NEAR_MISS_STUDIO_SCORE_MARGIN

        far_below = 90 - NEAR_MISS_STUDIO_SCORE_MARGIN - 1
        bootleg_release = {
            "id": "bootleg-rel",
            "title": "Some Compilation",
            "date": None,
            "release-group": {
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        }
        studio_release = {
            "id": "studio-rel",
            "title": "The Real Album",
            "date": "2005",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        }
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "Some Song (Bootleg)", "Someone", 100, [bootleg_release]
                        ),
                        recording_payload(
                            "Some Song", "Someone", far_below, [studio_release]
                        ),
                    ]
                }
            ]
        )

        best = client.resolve_canonical("Some Song", "Someone", min_score=90)

        assert best is not None
        assert best.best_release.title == "Some Compilation"

    def test_near_miss_fallback_only_activates_when_no_confident_studio_match(self):
        """When a confident candidate already has a canonical studio
        release, the near-miss fallback must not run at all — it exists
        only to rescue an otherwise-hopeless pool, never to second-guess a
        pool that already has a good answer."""
        studio_release = {
            "id": "kid-a",
            "title": "Kid A",
            "date": "2000",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        }
        weird_release = {
            "id": "obscure",
            "title": "Some Obscure Comp",
            "date": "1999",
            "release-group": {
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        }
        client = make_client(
            [
                {
                    "recordings": [
                        recording_payload(
                            "Everything In Its Right Place",
                            "Radiohead",
                            95,
                            [studio_release],
                        ),
                        recording_payload(
                            "Everything In Its Right Place (Obscure Edit)",
                            "Radiohead",
                            92,
                            [weird_release],
                        ),
                    ]
                }
            ]
        )

        best = client.resolve_canonical("Everything In Its Right Place", "Radiohead")

        assert best is not None
        assert best.best_release.title == "Kid A"


class TestTransport:
    def test_user_agent_identifies_the_app_and_a_contact(self):
        """MusicBrainz answers 403 without this."""
        client = make_client([{"recordings": []}])
        client.search_recording("x")
        headers = client._session.get.call_args.kwargs["headers"]
        assert "musica/" in headers["User-Agent"]
        assert "https://github.com/musica" in headers["User-Agent"]

    def test_503_raises_rate_limit_error_after_retries(self):
        client = make_client([{}, {}, {}], status=503)
        with pytest.raises(MusicBrainzRateLimitError):
            client.search_recording("x")

    def test_404_is_an_empty_result_not_a_failure(self):
        client = make_client([{}], status=404)
        assert client.lookup_recording("missing-mbid") is None

    def test_timeout_retries_then_succeeds(self, monkeypatch):
        """A read timeout is transient — the retry backoff re-asks and the
        search succeeds (live 2026-08-14: a timeout aborted an MB search
        when a retry seconds later would have answered)."""
        monkeypatch.setattr(
            "app.services.musicbrainz_client.time.sleep", lambda s: None
        )
        client = make_client()
        resp = Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"recordings": []}
        client._session.get = Mock(
            side_effect=[requests.exceptions.Timeout("slow"), resp]
        )
        assert client.search_recording("x") == []
        assert client._session.get.call_count == 2

    def test_connection_error_becomes_our_own_exception(self, monkeypatch):
        """After the retries are spent, a persistent connection failure
        raises rather than masquerading as 'not found'."""
        monkeypatch.setattr(
            "app.services.musicbrainz_client.time.sleep", lambda s: None
        )
        client = make_client()
        client._session.get = Mock(side_effect=requests.exceptions.Timeout("slow"))
        with pytest.raises(MusicBrainzConnectionError):
            client.search_recording("x")
        assert client._session.get.call_count == 3

    def test_repeated_identical_searches_hit_the_cache(self):
        """Each miss costs a full second of rate limit, and every track off
        one album asks the same question."""
        client = make_client([{"recordings": []}])
        client.search_recording("Jóga", "Björk")
        client.search_recording("Jóga", "Björk")
        assert client._session.get.call_count == 1


class TestRateLimiter:
    def test_enforces_the_minimum_interval(self):
        limiter = _RateLimiter(min_interval=0.05)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        assert time.monotonic() - start >= 0.04

    def test_first_call_does_not_block(self):
        assert _RateLimiter(min_interval=5.0).wait() == 0.0


class TestTTLCache:
    def test_returns_stored_value_then_expires_it(self):
        cache = _TTLCache(ttl=0.05)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        time.sleep(0.06)
        assert cache.get("k") is None

    def test_evicts_oldest_when_full(self):
        cache = _TTLCache(ttl=100, max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        assert cache.get("a") is None
        assert cache.get("c") == 3


# ---------------------------------------------------------------------------
# Release groups — P-MB-2's album search
# ---------------------------------------------------------------------------


RADIOHEAD_CREDIT = [
    {
        "name": "Radiohead",
        "joinphrase": "",
        "artist": {"id": "radiohead-mbid", "name": "Radiohead"},
    },
]

KID_A_GROUP_JSON = {
    "id": "rg-kid-a",
    "title": "Kid A",
    "count": 7,
    "score": 99,
    "primary-type": "Album",
    "secondary-types": [],
    "first-release-date": "2000-10-02",
    "artist-credit": RADIOHEAD_CREDIT,
}


class TestSearchReleaseGroup:
    def test_parses_every_field(self):
        client = make_client([{"release-groups": [KID_A_GROUP_JSON]}])
        groups = client.search_release_group("Kid A", "Radiohead")
        assert len(groups) == 1
        g = groups[0]
        assert isinstance(g, MBReleaseGroup)
        assert g.mbid == "rg-kid-a"
        assert g.title == "Kid A"
        assert g.artist == "Radiohead"
        assert g.artist_mbid == "radiohead-mbid"
        assert g.primary_type == "Album"
        assert g.secondary_types == []
        assert g.year == 2000
        assert g.release_count == 7
        assert g.score == 99

    def test_query_is_built_with_escaped_releasegroup_clause(self):
        client = make_client([{"release-groups": []}])
        client.search_release_group("Heroes (We Could Be)", "Alesso")
        params = client._session.get.call_args.kwargs["params"]
        assert 'releasegroup:"Heroes \\(We Could Be\\)"' in params["query"]
        assert 'artist:"Alesso"' in params["query"]

    def test_empty_title_short_circuits_without_calling_out(self):
        client = make_client([{"release-groups": []}])
        assert client.search_release_group("  ") == []
        client._session.get.assert_not_called()


class TestBrowseArtistReleaseGroups:
    def test_browses_the_release_group_endpoint(self):
        client = make_client([{"release-groups": [KID_A_GROUP_JSON]}])
        groups = client.browse_artist_release_groups("radiohead-mbid")
        assert [g.mbid for g in groups] == ["rg-kid-a"]
        url = client._session.get.call_args.args[0]
        params = client._session.get.call_args.kwargs["params"]
        assert url.endswith("/release-group")
        assert params["artist"] == "radiohead-mbid"


# ---------------------------------------------------------------------------
# Official-only search filtering
# ---------------------------------------------------------------------------

MIXTAPE_GROUP_JSON = {
    "id": "rg-mixtape",
    "title": "Some Mixtape",
    "primary-type": "Album",
    "secondary-types": ["Mixtape/Street"],
    "first-release-date": "2020-01-01",
    "artist-credit": RADIOHEAD_CREDIT,
}
LIVE_GROUP_JSON = {
    "id": "rg-live",
    "title": "Some Live Album",
    "primary-type": "Album",
    "secondary-types": ["Live"],
    "first-release-date": "2021-01-01",
    "artist-credit": RADIOHEAD_CREDIT,
}
SINGLE_GROUP_JSON = {
    "id": "rg-single",
    "title": "Some Single",
    "primary-type": "Single",
    "secondary-types": [],
    "first-release-date": "2020-01-01",
    "artist-credit": RADIOHEAD_CREDIT,
}


class TestOfficialOnlyFilter:
    def test_recording_official_only_filters_client_side_not_in_query(self):
        # The official-only filter runs *after* the fetch (like release groups),
        # never as a `status:official` query clause — that clause re-scores the
        # results and buries the canonical recording (the "All Caps" regression).
        client = make_client([{"recordings": []}])
        client.search_recording("Jóga", "Björk", official_only=True)
        params = client._session.get.call_args.kwargs["params"]
        assert "status:official" not in params["query"]
        assert "primarytype:album" not in params["query"]

    def test_recording_official_only_fetches_a_buffer_before_slicing(self):
        client = make_client([{"recordings": []}])
        client.search_recording("All Caps", limit=10, official_only=True)
        params = client._session.get.call_args.kwargs["params"]
        assert params["limit"] == 30  # max(10 * 3, 25)

    def test_recording_official_only_drops_bootleg_and_live_keeps_studio(self):
        def rec(mbid, releases):
            return {
                "id": mbid,
                "title": "All Caps",
                "score": 100,
                "artist-credit": [
                    {"name": "Madvillain", "artist": {"id": "a", "name": "Madvillain"}}
                ],
                "releases": releases,
            }

        bootleg = {
            "id": "b",
            "title": "Mixtape",
            "status": "Bootleg",
            "release-group": {
                "primary-type": "Album",
                "secondary-types": ["Mixtape/Street"],
            },
        }
        studio = {
            "id": "s",
            "title": "Madvillainy",
            "status": "Official",
            "release-group": {"primary-type": "Album", "secondary-types": []},
        }
        live = {
            "id": "l",
            "title": "Live Album",
            "status": "Official",
            "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
        }
        client = make_client(
            [
                {
                    "recordings": [
                        rec("boot", [bootleg]),
                        rec("studio", [studio]),
                        rec("live", [live]),
                    ]
                }
            ]
        )
        results = client.search_recording("All Caps", official_only=True)
        assert [r.mbid for r in results] == ["studio"]

    def test_recording_default_is_unfiltered(self):
        client = make_client([{"recordings": []}])
        client.search_recording("Jóga", "Björk")
        query = client._session.get.call_args.kwargs["params"]["query"]
        assert "status:official" not in query

    def test_release_group_official_only_filters_mixtape_and_live(self):
        client = make_client(
            [
                {
                    "release-groups": [
                        MIXTAPE_GROUP_JSON,
                        LIVE_GROUP_JSON,
                        KID_A_GROUP_JSON,
                    ]
                }
            ]
        )
        groups = client.search_release_group("x", official_only=True)
        assert [g.mbid for g in groups] == ["rg-kid-a"]

    def test_release_group_official_only_keeps_single(self):
        client = make_client([{"release-groups": [SINGLE_GROUP_JSON]}])
        groups = client.search_release_group("x", official_only=True)
        assert [g.mbid for g in groups] == ["rg-single"]

    def test_release_group_official_only_fetches_a_buffer_before_slicing(self):
        # Fetch more than `limit` so a mixtape-heavy head doesn't empty the
        # result when the album is one row further down.
        client = make_client([{"release-groups": []}])
        client.search_release_group("x", limit=10, official_only=True)
        params = client._session.get.call_args.kwargs["params"]
        assert params["limit"] == 30  # max(10 * 3, 25)

    def test_browse_artist_release_groups_official_only_filters(self):
        client = make_client(
            [
                {
                    "release-groups": [
                        LIVE_GROUP_JSON,
                        KID_A_GROUP_JSON,
                    ]
                }
            ]
        )
        groups = client.browse_artist_release_groups(
            "radiohead-mbid", official_only=True
        )
        assert [g.mbid for g in groups] == ["rg-kid-a"]


class TestOfficialPredicates:
    def test_release_group_is_official(self):
        assert MBReleaseGroup("g", "Album", "Artist", primary_type="Album").is_official
        assert MBReleaseGroup(
            "g", "Single", "Artist", primary_type="Single"
        ).is_official
        assert MBReleaseGroup("g", "EP", "Artist", primary_type="EP").is_official
        assert not MBReleaseGroup(
            "g",
            "Mixtape",
            "Artist",
            primary_type="Album",
            secondary_types=["Mixtape/Street"],
        ).is_official
        assert not MBReleaseGroup(
            "g", "Podcast", "Artist", primary_type="Broadcast"
        ).is_official
        assert not MBReleaseGroup("g", "?", "Artist", primary_type=None).is_official

    def test_release_is_official_requires_status(self):
        assert MBRelease("r", "A", primary_type="Album", status="Official").is_official
        assert not MBRelease(
            "r", "A", primary_type="Album", status="Bootleg"
        ).is_official
        assert not MBRelease(
            "r", "A", primary_type="Album", secondary_types=["Live"], status="Official"
        ).is_official

    def test_recording_is_official_when_any_release_is(self):
        live = MBRelease(
            "l",
            "Live",
            primary_type="Album",
            secondary_types=["Live"],
            status="Official",
        )
        studio = MBRelease("s", "Studio", primary_type="Album", status="Official")
        assert MBRecording("r", "t", "a", "a", releases=[live, studio]).is_official
        assert not MBRecording("r", "t", "a", "a", releases=[live]).is_official
        assert not MBRecording("r", "t", "a", "a").is_official


class TestReleaseGroupMbid:
    def test_search_recording_exposes_the_group_mbid_of_the_best_release(self):
        # The release's nested `release-group` carries the MBID that keys cover
        # art on the Cover Art Archive. It must survive parsing and surface via
        # `best_release` so the route can hand it to the frontend.
        client = make_client(
            [
                {
                    "recordings": [
                        {
                            "id": "rec-1",
                            "title": "All Caps",
                            "score": 100,
                            "artist-credit": [
                                {
                                    "name": "Madvillain",
                                    "artist": {"id": "a", "name": "Madvillain"},
                                }
                            ],
                            "releases": [
                                {
                                    "id": "rel-1",
                                    "title": "Madvillainy",
                                    "date": "2004-03-23",
                                    "status": "Official",
                                    "release-group": {
                                        "id": "rg-madvillainy",
                                        "primary-type": "Album",
                                        "secondary-types": [],
                                    },
                                }
                            ],
                        }
                    ]
                }
            ]
        )
        recs = client.search_recording("All Caps", "Madvillain")
        assert len(recs) == 1
        best = recs[0].best_release
        assert best is not None
        assert best.release_group_mbid == "rg-madvillainy"

    def test_release_without_a_group_has_no_cover_mbid(self):
        # The release-group *lookup* path threads type/status through a
        # synthetic group with no id — cover art must simply be None there.
        from app.services.musicbrainz_client import _parse_release

        release = _parse_release(
            {"id": "rel-1", "title": "Kid A", "status": "Official"},
            {"primary-type": "Album", "secondary-types": []},
        )
        assert release.release_group_mbid is None


# ---------------------------------------------------------------------------
# lookup_release_group_tracks — the group-to-track-list bridge
# ---------------------------------------------------------------------------


RG_LOOKUP_JSON = {
    "id": "rg-kid-a",
    "primary-type": "Album",
    "secondary-types": [],
    "releases": [
        {
            "id": "rel-2016",
            "title": "Kid A",
            "date": "2016-05-13",
            "status": "Official",
        },
        {
            "id": "rel-2000",
            "title": "Kid A",
            "date": "2000-10-02",
            "status": "Official",
        },
    ],
}

MADVILLAINY_LOOKUP_JSON = {
    "id": "rg-madvillainy",
    "primary-type": "Album",
    "secondary-types": [],
    "releases": [
        {
            "id": "rel-preview",
            "title": "Madvillainy Preview 11/02",
            "date": "2002-11",
            "status": "Bootleg",
        },
        {
            "id": "rel-madvillainy",
            "title": "Madvillainy",
            "date": "2004-03-19",
            "status": "Official",
        },
    ],
}

RELEASE_LOOKUP_JSON = {
    "id": "rel-2000",
    "title": "Kid A",
    "media": [
        {
            "position": 1,
            "track-count": 2,
            "track": [
                {
                    "position": 1,
                    "number": "1",
                    "recording": {
                        "id": "rec-1",
                        "title": "Everything In Its Right Place",
                        "length": 271000,
                        "artist-credit": RADIOHEAD_CREDIT,
                    },
                },
                {
                    "position": 2,
                    "number": "2",
                    "recording": {
                        "id": "rec-2",
                        "title": "Kid A",
                        "length": 244000,
                        "artist-credit": RADIOHEAD_CREDIT,
                    },
                },
            ],
        }
    ],
}


class TestLookupReleaseGroupTracks:
    def test_returns_ordered_tracks_and_picks_the_earliest_release(self):
        """The reissue (2016) must not win over the original (2000)."""
        client = make_client([RG_LOOKUP_JSON, RELEASE_LOOKUP_JSON])
        tracks = client.lookup_release_group_tracks("rg-kid-a")

        assert [t.title for t in tracks] == ["Everything In Its Right Place", "Kid A"]
        assert tracks[0].mbid == "rec-1"
        assert tracks[0].artist == "Radiohead"
        assert tracks[0].artist_mbid == "radiohead-mbid"
        assert tracks[0].length_ms == 271000
        # Second lookup went to the canonical (earliest) release.
        second_url = client._session.get.call_args_list[1].args[0]
        assert second_url.endswith("/release/rel-2000")

    def test_skips_an_earlier_bootleg_preview_for_the_official_album(self):
        client = make_client([MADVILLAINY_LOOKUP_JSON, RELEASE_LOOKUP_JSON])
        client.lookup_release_group_tracks("rg-madvillainy")

        second_url = client._session.get.call_args_list[1].args[0]
        assert second_url.endswith("/release/rel-madvillainy")

    def test_non_canonical_group_still_returns_tracks(self):
        """A live/compilation group has no canonical studio release, but its
        tracks still have to come from *somewhere* — the earliest release."""
        live_group = {
            "id": "rg-live",
            "primary-type": "Album",
            "secondary-types": ["Live"],
            "releases": [
                {"id": "rel-live", "title": "I Might Be Wrong", "date": "2001-11-12"},
            ],
        }
        client = make_client([live_group, RELEASE_LOOKUP_JSON])
        tracks = client.lookup_release_group_tracks("rg-live")
        assert len(tracks) == 2

    def test_preserves_media_and_track_order_across_discs(self):
        two_disc = {
            "id": "rel-2disc",
            "media": [
                {
                    "position": 1,
                    "track": [
                        {
                            "position": 1,
                            "number": "1",
                            "recording": {
                                "id": "d1t1",
                                "title": "Disc One",
                                "length": 1,
                                "artist-credit": RADIOHEAD_CREDIT,
                            },
                        }
                    ],
                },
                {
                    "position": 2,
                    "track": [
                        {
                            "position": 1,
                            "number": "1",
                            "recording": {
                                "id": "d2t1",
                                "title": "Disc Two",
                                "length": 1,
                                "artist-credit": RADIOHEAD_CREDIT,
                            },
                        }
                    ],
                },
            ],
        }
        client = make_client([RG_LOOKUP_JSON, two_disc])
        tracks = client.lookup_release_group_tracks("rg-kid-a")
        assert [t.mbid for t in tracks] == ["d1t1", "d2t1"]

    def test_404_is_an_empty_list_not_a_failure(self):
        client = make_client([{}], status=404)
        assert client.lookup_release_group_tracks("missing") == []

    def test_malformed_group_is_an_empty_list(self):
        client = make_client([{"foo": "bar"}])
        assert client.lookup_release_group_tracks("rg-x") == []

    def test_group_with_no_releases_is_an_empty_list(self):
        client = make_client([{"id": "rg-x", "releases": []}])
        assert client.lookup_release_group_tracks("rg-x") == []
