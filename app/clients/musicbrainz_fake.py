"""In-memory MusicBrainz tag source for tests of code built on it."""

from __future__ import annotations

from app.clients.catalog import CatalogNotFound
from app.clients.musicbrainz import MusicBrainzTagSourceBase, RecordingInfo, ReleaseInfo


class FakeMusicBrainzTags(MusicBrainzTagSourceBase):
    """Fill ``recordings`` and ``releases`` by MBID; unknown ids are not found."""

    def __init__(self) -> None:
        self.recordings: dict[str, RecordingInfo] = {}
        self.releases: dict[str, ReleaseInfo] = {}

    def recording_info(self, recording_id: str) -> RecordingInfo:
        if recording_id not in self.recordings:
            raise CatalogNotFound(recording_id)
        return self.recordings[recording_id]

    def release_info(self, release_id: str) -> ReleaseInfo:
        if release_id not in self.releases:
            raise CatalogNotFound(release_id)
        return self.releases[release_id]
