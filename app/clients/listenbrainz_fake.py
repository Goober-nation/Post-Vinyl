"""In-memory ListenBrainz client for tests of code built on it."""

from __future__ import annotations

from app.clients.listenbrainz import (
    ListenBrainzAuthError,
    ListenBrainzClientBase,
    PopularRecording,
    RadioTrack,
    Recommendations,
    RecordingMetadata,
    SimilarArtist,
    TokenCheck,
)


class FakeListenBrainzClient(ListenBrainzClientBase):
    def ping(self) -> None:
        pass

    """``recordings`` by MBID; ``tokens`` maps token -> user name; lookups
    match exact (artist, title) pairs, case-insensitive."""

    def __init__(self) -> None:
        self.recordings: dict[str, RecordingMetadata] = {}
        self.tokens: dict[str, str] = {}
        self.recs: dict[str, Recommendations] = {}  # by user name
        self.radio_tracks: list[RadioTrack] = []
        self.popular: dict[str, list[PopularRecording]] = {}
        self.similar: dict[str, list[SimilarArtist]] = {}

    def recording_metadata(self, mbids: list[str]) -> dict[str, RecordingMetadata]:
        return {m: self.recordings[m] for m in mbids if m in self.recordings}

    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        if token not in self.tokens:
            raise ListenBrainzAuthError("GET /metadata/lookup/: HTTP 401")
        key = (artist.lower(), title.lower())
        return next(
            (
                r
                for r in self.recordings.values()
                if (r.artist.lower(), r.title.lower()) == key
            ),
            None,
        )

    def validate_token(self, token: str) -> TokenCheck:
        user = self.tokens.get(token)
        return TokenCheck(valid=user is not None, user_name=user)

    def recommendations(self, user_name: str) -> Recommendations | None:
        return self.recs.get(user_name)

    def radio(self, artist: str, level: str, token: str) -> list[RadioTrack]:
        if token not in self.tokens:
            raise ListenBrainzAuthError("GET /explore/lb-radio: HTTP 401")
        return self.radio_tracks

    def top_recordings(self, artist_mbid: str) -> list[PopularRecording]:
        return self.popular.get(artist_mbid, [])

    def similar_artists(self, artist_mbid: str) -> list[SimilarArtist]:
        return self.similar.get(artist_mbid, [])
