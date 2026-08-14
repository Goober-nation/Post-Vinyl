"""
Unit tests for ListenBrainzRecs implementation.
"""

from unittest.mock import Mock, patch

import pytest

from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError,
)
from app.services.interfaces.recommendation import Recommendation
from app.services.library import Song
from app.services.recommendation import ListenBrainzRecs, is_spoken_word


class MockConfig:
    """Mock config for testing."""

    class ListenBrainzConfig:
        enabled = True
        url = "https://api.listenbrainz.org"
        token = "test-token"
        username = "testuser"

    class RecsConfig:
        pass

    def __init__(self):
        self.listenbrainz = self.ListenBrainzConfig()
        self.recs = self.RecsConfig()


class TestListenBrainzRecsInit:
    """Test ListenBrainzRecs initialization."""

    def test_init_with_config(self):
        """ListenBrainzRecs should initialize with config."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        assert recs.base_url == "https://api.listenbrainz.org"
        assert recs.token == "test-token"
        assert recs.username == "testuser"

    def test_get_headers_with_token(self):
        """_get_headers should include authorization token."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        headers = recs._get_headers()

        assert headers["Authorization"] == "Token test-token"
        assert headers["Content-Type"] == "application/json"


class TestListenBrainzFetchRecommendations:
    """Test fetch_recommendations() method."""

    def test_fetch_disabled_raises_error(self):
        """fetch_recommendations() should raise ListenBrainzDisabledError when disabled."""
        config = MockConfig()
        config.listenbrainz.enabled = False
        recs = ListenBrainzRecs(config)

        with pytest.raises(ListenBrainzDisabledError):
            recs.fetch_recommendations({"comfort_zone": 5})

    @patch("app.services.recommendation.ListenBrainzRecs._fetch_comfort_zone")
    @patch("app.services.recommendation.ListenBrainzRecs._fetch_fresh_picks")
    @patch("app.services.recommendation.ListenBrainzRecs._fetch_deep_cuts")
    def test_fetch_all_sources(self, mock_deep, mock_fresh, mock_comfort):
        """fetch_recommendations() should fetch from all sources."""
        mock_comfort.return_value = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1")
        ]
        mock_fresh.return_value = [
            Recommendation("fresh_picks", "Artist 2", "Track 2", "mbid-2")
        ]
        mock_deep.return_value = [
            Recommendation("deep_cuts", "Artist 3", "Track 3", "mbid-3")
        ]

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        results = recs.fetch_recommendations(
            {"comfort_zone": 5, "fresh_picks": 5, "deep_cuts": 5}
        )

        assert len(results) == 3
        assert any(r.source == "comfort_zone" for r in results)
        assert any(r.source == "fresh_picks" for r in results)
        assert any(r.source == "deep_cuts" for r in results)

    @patch("app.services.recommendation.ListenBrainzRecs._fetch_comfort_zone")
    def test_fetch_single_source(self, mock_comfort):
        """fetch_recommendations() should handle single source."""
        mock_comfort.return_value = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1")
        ]

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        results = recs.fetch_recommendations({"comfort_zone": 5})

        assert len(results) == 1
        assert results[0].source == "comfort_zone"

    @patch("app.services.recommendation.ListenBrainzRecs._fetch_comfort_zone")
    def test_fetch_zero_count(self, mock_comfort):
        """fetch_recommendations() should skip sources with count 0."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        results = recs.fetch_recommendations({"comfort_zone": 0})

        assert len(results) == 0
        mock_comfort.assert_not_called()


class TestListenBrainzClassify:
    """Test classify() method."""

    def test_classify_in_library_by_mbid(self):
        """classify() should match by MBID."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        recommendations = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-123")
        ]

        library = [
            Song(
                "song-1",
                "Track 1",
                "Artist 1",
                "Album",
                "/path",
                180,
                4320000,
                192,
                1,
                2020,
                "Rock",
                5,
                True,
                "mbid-123",
            )
        ]

        classification = recs.classify(recommendations, library)

        assert len(classification.in_library) == 1
        assert len(classification.to_download) == 0

    def test_classify_in_library_by_artist_track(self):
        """classify() should match by normalized artist+track."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        recommendations = [Recommendation("comfort_zone", "Artist 1", "Track 1", None)]

        library = [
            Song(
                "song-1",
                "Track 1",
                "Artist 1",
                "Album",
                "/path",
                180,
                4320000,
                192,
                1,
                2020,
                "Rock",
                5,
                True,
                None,
            )
        ]

        classification = recs.classify(recommendations, library)

        assert len(classification.in_library) == 1
        assert len(classification.to_download) == 0

    def test_classify_to_download(self):
        """classify() should mark non-matching as to_download."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        recommendations = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-123")
        ]

        library = []  # Empty library

        classification = recs.classify(recommendations, library)

        assert len(classification.in_library) == 0
        assert len(classification.to_download) == 1

    def test_classify_deduplication(self):
        """classify() should deduplicate recommendations."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        recommendations = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-123"),
            Recommendation(
                "fresh_picks", "Artist 1", "Track 1", "mbid-123"
            ),  # Duplicate MBID
        ]

        library = []

        classification = recs.classify(recommendations, library)

        # Should only have one (deduplicated)
        assert len(classification.to_download) == 1


class TestListenBrainzHelpers:
    """Test helper methods."""

    def test_normalize(self):
        """_normalize should lowercase and remove non-alphanumeric."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        assert recs._normalize("Hello World!") == "helloworld"
        assert recs._normalize("Artist Name") == "artistname"
        assert recs._normalize("") == ""

    def test_artist_words(self):
        """_artist_words should extract meaningful words."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        words = recs._artist_words("The Beatles")
        assert "beatles" in words
        assert "the" not in words  # Stop word

    def test_artist_words_with_features(self):
        """_artist_words should truncate the feat clause (P6.5-6)."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        words = recs._artist_words("Alesso feat. Katy Perry")
        assert words == ["alesso"]
        assert "katy" not in words
        assert "perry" not in words
        assert "feat" not in words

    def test_filepath_contains_artist(self):
        """_filepath_contains_artist should check for artist words."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        assert recs._filepath_contains_artist("Artist - Track.mp3", ["artist"]) is True
        assert recs._filepath_contains_artist("Other - Track.mp3", ["artist"]) is False
        assert recs._filepath_contains_artist("Track.mp3", []) is True  # No filter

    def test_artist_words_folds_accents(self):
        """2026-08-12 fix: Python's `\\w` is Unicode-aware, so this method's
        own regex never shattered 'Björk' the way query_builder's ASCII-only
        tokenizer did — but it also never stripped the accent, which is its
        own bug (see the filepath test below)."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)
        assert recs._artist_words("Björk") == ["bjork"]
        assert recs._artist_words("Sigur Rós") == ["sigur", "ros"]

    def test_filepath_contains_artist_matches_either_way_on_accents(self):
        """The real bug: an unfolded 'björk' only matched a peer filename
        that ALSO kept the accent. Most Soulseek filenames are plain ASCII,
        so this silently rejected the common case. Folding both the artist
        words and the filepath fixes it regardless of which way a given
        peer spelled it."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)
        words = recs._artist_words("Björk")

        assert recs._filepath_contains_artist("01 Bjork - Joga.flac", words) is True
        assert recs._filepath_contains_artist("01 Björk - Jóga.flac", words) is True

    def test_filename_has_remix_qualifier(self):
        """_filename_has_remix_qualifier should detect remixes."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        assert recs._filename_has_remix_qualifier("Track (Remix).mp3") is True
        assert recs._filename_has_remix_qualifier("Track (Live).mp3") is True
        assert recs._filename_has_remix_qualifier("Track.mp3") is False


class TestListenBrainzQueueDownloads:
    """Test queue_downloads() method."""

    def test_queue_downloads_stub(self):
        """queue_downloads() should return stub response."""
        config = MockConfig()
        recs = ListenBrainzRecs(config)

        recommendations = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1")
        ]

        result = recs.queue_downloads(recommendations)

        assert result["queued"] == 0
        assert result["failed"] == 1
        assert len(result["failures"]) == 1


class TestListenBrainzFetchSources:
    """Test individual fetch methods."""

    @patch("app.services.recommendation.requests.Session.get")
    def test_fetch_comfort_zone_success(self, mock_get):
        """_fetch_comfort_zone should parse response correctly."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "payload": {
                "mbids": [{"recording_mbid": "mbid-1"}, {"recording_mbid": "mbid-2"}]
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        # Mock metadata fetch
        with patch.object(recs, "_fetch_recording_metadata") as mock_meta:
            mock_meta.return_value = {"artist_name": "Artist", "track_name": "Track"}

            results = recs._fetch_comfort_zone(5)

            assert len(results) == 2
            assert all(r.source == "comfort_zone" for r in results)

    @patch("app.services.recommendation.requests.Session.get")
    def test_fetch_comfort_zone_connection_error(self, mock_get):
        """_fetch_comfort_zone should raise ListenBrainzConnectionError on connection error."""
        import requests

        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        with pytest.raises(ListenBrainzConnectionError):
            recs._fetch_comfort_zone(5)

    @patch("app.services.recommendation.requests.Session.get")
    def test_fetch_fresh_picks_success(self, mock_get):
        """_fetch_fresh_picks should parse response correctly."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "payload": {
                "releases": [
                    {
                        "artist_credit_name": "Artist 1",
                        "release_name": "Track 1",
                        "recording_mbid": "mbid-1",
                    }
                ]
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        results = recs._fetch_fresh_picks(5)

        assert len(results) == 1
        assert results[0].source == "fresh_picks"
        assert results[0].artist == "Artist 1"
        assert results[0].track == "Track 1"
        assert results[0].album == "Track 1"


class TestListenBrainzDeepCutsParsing:
    """Test Deep Cuts real-shape parsing."""

    def test_unwrap_playlist_wrapper(self):
        """Deep Cuts playlists are wrapped in a 'playlist' key."""
        import json
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "lb" / "user_playlists.json"
        )
        data = json.loads(fixture_path.read_text())
        playlists = data.get("playlists", [])

        for entry in playlists:
            inner = entry.get("playlist", {})
            assert inner, f"Expected 'playlist' wrapper in entry: {list(entry.keys())}"
            ident = inner.get("identifier", "")
            assert "listenbrainz.org/playlist/" in ident, (
                f"Expected playlist URL, got: {ident}"
            )

    def test_extract_uuid_from_playlist_url(self):
        """UUID should be extracted as the last / segment from listenbrainz playlist URL."""
        url = "https://listenbrainz.org/playlist/620b23a2-83bc-4fd5-94a2-af9e0bcb32bd"
        uuid = url.rsplit("/", 1)[-1]
        assert uuid == "620b23a2-83bc-4fd5-94a2-af9e0bcb32bd"

        url2 = "https://listenbrainz.org/playlist/no-uuid/"
        uuid2 = url2.rsplit("/", 1)[-1]
        assert uuid2 == ""

    def test_track_identifier_is_list(self):
        """Track identifiers from /1/playlist/{uuid} are a list of one URL."""
        identifiers = [
            "https://musicbrainz.org/recording/f47ac10b-58cc-4372-a567-0e02b2c3d479"
        ]
        first = identifiers[0] if isinstance(identifiers, list) else identifiers
        assert "musicbrainz.org" in first
        mbid = first.rsplit("/", 1)[-1]
        assert mbid == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    def test_track_identifier_empty_list(self):
        """Empty identifier list should yield None mbid."""
        identifiers: list = []
        first = identifiers[0] if identifiers else None
        assert first is None

    def test_track_identifier_string(self):
        """identifier that is already a string should still work."""
        identifiers = "https://musicbrainz.org/recording/abc-123"
        mbid = (
            identifiers.rsplit("/", 1)[-1] if "musicbrainz.org" in identifiers else None
        )
        assert mbid == "abc-123"


class TestListenBrainzRealFixtures:
    """Tests that load real fixture JSON and verify parsing."""

    @patch("app.services.recommendation.requests.Session.get")
    def test_fresh_picks_real_fixture(self, mock_get, tmp_path):
        """Fresh Picks parsing of real fixture sets album field."""
        import json
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "lb" / "fresh_releases.json"
        )
        fixture_data = json.loads(fixture_path.read_text())

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = fixture_data
        mock_get.return_value = mock_response

        config = MockConfig()
        recs = ListenBrainzRecs(config)
        results = recs._fetch_fresh_picks(3)

        assert len(results) == 3
        for r in results:
            assert r.source == "fresh_picks"
            assert r.artist
            assert r.track
            assert r.album == r.track

    @patch("app.services.recommendation.requests.Session.get")
    def test_comfort_zone_real_fixture(self, mock_get):
        """Comfort Zone parsing of real fixture extracts recording_mbids."""
        import json
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "lb" / "cf_recommendation.json"
        )
        fixture_data = json.loads(fixture_path.read_text())

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = fixture_data
        mock_get.return_value = mock_response

        config = MockConfig()
        recs = ListenBrainzRecs(config)

        with patch.object(recs, "_fetch_recording_metadata") as mock_meta:
            mock_meta.return_value = {
                "artist_name": "Test Artist",
                "track_name": "Test Track",
            }
            results = recs._fetch_comfort_zone(3)

        assert len(results) == 3
        for r in results:
            assert r.source == "comfort_zone"
            assert r.mbid
            assert r.artist == "Test Artist"
            assert r.track == "Test Track"

    @patch("app.services.recommendation.requests.Session.get")
    def test_deep_cuts_real_fixture_unwrap(self, mock_get):
        """Deep Cuts parsing of real fixture correctly unwraps playlist wrapper."""
        import json
        from pathlib import Path

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "lb" / "user_playlists.json"
        )
        fixture_data = json.loads(fixture_path.read_text())

        # Build mock that returns fixture for playlists endpoint
        call_count = [0]

        def side_effect(*args, **kwargs):
            resp = Mock()
            call_count[0] += 1
            if "recommendations" in args[0]:
                resp.status_code = 200
                resp.json.return_value = fixture_data
            else:
                # Playlist tracks endpoint
                resp.status_code = 200
                resp.json.return_value = {
                    "playlist": {
                        "track": [
                            {
                                "creator": "Artist 1",
                                "title": "Track 1",
                                "identifier": [
                                    "https://musicbrainz.org/recording/mbid-deep-1"
                                ],
                            }
                        ]
                    }
                }
            return resp

        mock_get.side_effect = side_effect

        config = MockConfig()
        recs = ListenBrainzRecs(config)
        results = recs._fetch_deep_cuts(1)

        # Should have processed the first playlist's tracks
        assert len(results) >= 1
        for r in results:
            assert r.source == "deep_cuts"
            assert r.artist
            assert r.track


class TestSpokenWordExclusion:
    """Fresh Picks is LB's *global* new-releases feed, so it serves podcasts
    and audiobooks alongside music. A live pull on 2026-08-11 spent all 5 of
    the user's Fresh Picks slots on releases that could never resolve on
    Soulseek, one of them "The Adam Buxton Podcast #279"."""

    def test_broadcast_primary_type_is_spoken_word(self):
        # How MusicBrainz actually types podcasts — verified against the
        # live LB fresh-releases feed.
        assert is_spoken_word(
            name="The Adam Buxton Podcast #279", primary_type="Broadcast"
        )

    def test_spoken_word_secondary_types_are_excluded(self):
        for secondary in ("Audiobook", "Audio drama", "Spokenword", "Interview"):
            assert is_spoken_word(name="Whatever", secondary_type=secondary), secondary

    def test_name_fallback_catches_untyped_podcasts(self):
        assert is_spoken_word(name="Some Show Podcast #12")
        assert is_spoken_word(name="Moby Dick (audiobook)")

    def test_ordinary_music_is_kept(self):
        assert not is_spoken_word(
            name="Flower Boy", artist="Tyler, The Creator", primary_type="Album"
        )
        assert not is_spoken_word(name="Alright", primary_type="Single")

    def test_chapter_titled_album_is_not_mistaken_for_an_audiobook(self):
        """Real 2026-08-11 fresh release — a name regex loose enough to
        include 'chapter' or 'episode' drops legitimate music."""
        assert not is_spoken_word(
            name="One Assassination Under God, Chapter 2",
            artist="Marilyn Manson",
            primary_type="Album",
        )

    @patch("requests.Session.get")
    def test_podcasts_do_not_consume_fresh_picks_slots(self, mock_get):
        """Filtering must happen before the count slice — otherwise a
        podcast doesn't just waste a slot, it burns one of the user's N."""
        releases = [
            {
                "artist_credit_name": "Adam Buxton",
                "release_name": "The Adam Buxton Podcast #279",
                "release_group_primary_type": "Broadcast",
            },
            {
                "artist_credit_name": "Real Band",
                "release_name": "Real Album",
                "release_group_primary_type": "Album",
            },
        ]
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"payload": {"releases": releases}}
        mock_get.return_value = resp

        results = ListenBrainzRecs(MockConfig())._fetch_fresh_picks(1)

        assert [r.track for r in results] == ["Real Album"]


class TestDeepCutsCountIsTracksNotPlaylists:
    """`playlists[:count]` sliced *playlists*, so a request for 5 Deep Cuts
    pulled the first 5 playlists and queued every track in them — a live
    pull on 2026-08-11 asked for 5 and got 100, queueing ~97 downloads."""

    @patch("requests.Session.get")
    def test_count_caps_tracks_across_playlists(self, mock_get):
        playlists = {
            "playlists": [
                {
                    "playlist": {
                        "identifier": f"https://listenbrainz.org/playlist/uuid-{i}"
                    }
                }
                for i in range(3)
            ]
        }

        def side_effect(url, *_args, **_kwargs):
            resp = Mock()
            resp.status_code = 200
            if "recommendations" in url:
                resp.json.return_value = playlists
            else:
                resp.json.return_value = {
                    "playlist": {
                        "track": [
                            {"creator": f"Artist {n}", "title": f"Track {n}"}
                            for n in range(50)
                        ]
                    }
                }
            return resp

        mock_get.side_effect = side_effect

        results = ListenBrainzRecs(MockConfig())._fetch_deep_cuts(5)

        assert len(results) == 5

    @patch("requests.Session.get")
    def test_walks_on_to_the_next_playlist_when_short(self, mock_get):
        """The cap must not become a floor: a 2-track first playlist should
        still let the second one top the pull up."""
        playlists = {
            "playlists": [
                {"playlist": {"identifier": "https://listenbrainz.org/playlist/uuid-a"}},
                {"playlist": {"identifier": "https://listenbrainz.org/playlist/uuid-b"}},
            ]
        }

        def side_effect(url, *_args, **_kwargs):
            resp = Mock()
            resp.status_code = 200
            if "recommendations" in url:
                resp.json.return_value = playlists
            elif "uuid-a" in url:
                resp.json.return_value = {
                    "playlist": {
                        "track": [
                            {"creator": "A", "title": "a1"},
                            {"creator": "A", "title": "a2"},
                        ]
                    }
                }
            else:
                resp.json.return_value = {
                    "playlist": {
                        "track": [{"creator": "B", "title": f"b{n}"} for n in range(10)]
                    }
                }
            return resp

        mock_get.side_effect = side_effect

        results = ListenBrainzRecs(MockConfig())._fetch_deep_cuts(5)

        assert len(results) == 5
        assert [r.track for r in results] == ["a1", "a2", "b0", "b1", "b2"]


class TestFreshPicksOrdering:
    """LB serves this feed sorted alphabetically by artist, so `[:count]`
    returned the same obscure head on every pull rather than the freshest
    releases."""

    @patch("requests.Session.get")
    def test_newest_releases_win_over_alphabetical_order(self, mock_get):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {
            "payload": {
                "releases": [
                    {
                        "artist_credit_name": "144p",
                        "release_name": "Alphabetically First",
                        "release_date": "2026-08-08",
                        "release_group_primary_type": "Album",
                    },
                    {
                        "artist_credit_name": "Zeta",
                        "release_name": "Actually Newest",
                        "release_date": "2026-08-14",
                        "release_group_primary_type": "Album",
                    },
                ]
            }
        }
        mock_get.return_value = resp

        results = ListenBrainzRecs(MockConfig())._fetch_fresh_picks(1)

        assert [r.track for r in results] == ["Actually Newest"]

    @patch("requests.Session.get")
    def test_missing_release_date_does_not_crash(self, mock_get):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {
            "payload": {
                "releases": [
                    {"artist_credit_name": "A", "release_name": "No Date"},
                    {
                        "artist_credit_name": "B",
                        "release_name": "Dated",
                        "release_date": "2026-08-14",
                    },
                ]
            }
        }
        mock_get.return_value = resp

        results = ListenBrainzRecs(MockConfig())._fetch_fresh_picks(2)

        assert [r.track for r in results] == ["Dated", "No Date"]
