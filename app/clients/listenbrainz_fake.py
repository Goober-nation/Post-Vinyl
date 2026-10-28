"""In-memory ListenBrainz client for tests of code built on it."""

from __future__ import annotations

from app.clients.listenbrainz import (
    ListenBrainzAuthError,
    ListenBrainzClientBase,
    RecordingMetadata,
    TokenCheck,
)


class FakeListenBrainzClient(ListenBrainzClientBase):
    """``recordings`` by MBID; ``tokens`` maps token -> user name; lookups
    match exact (artist, title) pairs, case-insensitive."""

    def __init__(self) -> None:
        self.recordings: dict[str, RecordingMetadata] = {}
        self.tokens: dict[str, str] = {}

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
