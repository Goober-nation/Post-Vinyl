"""Tagging: write only tags known to be true (#104).

No beets, no Picard. Decided with the user, 2026-10-29:

- **Recording**: the song's MusicBrainz id from ``song_identity``; without
  one, ListenBrainz's name → MBID lookup with the requesting user's token,
  accepted only when the main artist matches and the length is within the
  matcher's tolerance.
- **Release**: ``core/release_choice.py`` (earliest official plain album, then
  EP, then single).
- **Tags**: title, artist, album artist, album, date, original date, track and
  disc number with totals, MusicBrainz ids (recording, release track, release,
  release group, artists, album artists), the ISRC when MusicBrainz has
  exactly one, and the front cover from the Cover Art Archive.
- **The peer's tags**: everything is replaced, except ReplayGain and lyrics.
- One ``tagging`` row per attempt. A failure (``failTag``) never blocks the
  file: it stays ``verified`` and the files service moves it untagged.

Formats: FLAC, MP3, M4A/MP4, Ogg Vorbis/Opus (cover art not written for Ogg).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDOR,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    TXXX,
    UFID,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from app.clients.catalog import CatalogError
from app.clients.coverart import CoverArtClientBase
from app.clients.listenbrainz import ListenBrainzClientBase, ListenBrainzError
from app.clients.musicbrainz import (
    MusicBrainzTagSourceBase,
    RecordingInfo,
    RecordingRelease,
    ReleaseInfo,
)
from app.config import FilesSettings
from app.core import states
from app.core.matcher import DURATION_TOLERANCE_S, norm
from app.core.release_choice import choose_release
from app.services.encryption import SecretBox
from app.stores.database import Database
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import Song, SongsStore
from app.stores.users import UsersStore

log = logging.getLogger(__name__)


class TagFailed(Exception):
    """Why a song couldn't be tagged; the message is the stored error code."""


# ------------------------------------------------------------ building the tags


def build_tags(rec: RecordingInfo, release: ReleaseInfo) -> dict[str, Any]:
    """The tags to write, as plain values (also stored in ``tags_written``)."""
    track = next((t for t in release.tracks if t.recording_id == rec.id), None)
    if track is None:
        raise TagFailed("not_on_release")
    artist = "".join(name + join for name, join in rec.credits)
    tags: dict[str, Any] = {
        "title": rec.title,
        "artist": artist,
        "albumartist": release.artist,
        "album": release.title,
        "date": release.date,
        "originaldate": release.group_first_date,
        "tracknumber": track.position,
        "tracktotal": release.tracks_per_disc.get(track.disc, 0),
        "discnumber": track.disc,
        "disctotal": len(release.tracks_per_disc),
        "musicbrainz_trackid": rec.id,  # recording MBID, as Picard names it
        "musicbrainz_releasetrackid": track.id,
        "musicbrainz_albumid": release.id,
        "musicbrainz_releasegroupid": release.group_id,
        "musicbrainz_artistid": list(rec.artist_ids),
        "musicbrainz_albumartistid": list(release.artist_ids),
    }
    if len(rec.isrcs) == 1:
        tags["isrc"] = rec.isrcs[0]
    return {k: v for k, v in tags.items() if v not in ("", None, [])}


def build_album_tags(rec: RecordingInfo, chosen: RecordingRelease) -> dict[str, Any]:
    """When the release's disc and track details can't be had: everything known
    from the recording's release list, no track or disc numbers, so the file
    still lands in its album folder (user, 2026-10-29)."""
    tags: dict[str, Any] = {
        "title": rec.title,
        "artist": "".join(name + join for name, join in rec.credits),
        "album": chosen.title,
        "date": chosen.date,
        "musicbrainz_trackid": rec.id,
        "musicbrainz_albumid": chosen.id,
        "musicbrainz_releasegroupid": chosen.group_id,
        "musicbrainz_artistid": list(rec.artist_ids),
    }
    if len(rec.isrcs) == 1:
        tags["isrc"] = rec.isrcs[0]
    return {k: v for k, v in tags.items() if v not in ("", None, [])}


# ------------------------------------------------------------ writing, per format

_KEEP_VORBIS = ("replaygain_", "lyrics", "unsyncedlyrics")
_MB_ID3 = {
    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
}
_MP4_TEXT = {"title": "©nam", "artist": "©ART", "albumartist": "aART", "album": "©alb",
             "date": "©day"}  # fmt: skip
_MP4_FREEFORM = {
    "originaldate": "ORIGINALDATE",
    "isrc": "ISRC",
    "musicbrainz_trackid": "MusicBrainz Track Id",
    **_MB_ID3,
}


def _as_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _write_vorbis(audio: Any, tags: dict[str, Any], cover: bytes | None) -> None:
    kept = {k: v for k, v in (audio.tags or {}).items()
            if k.lower().startswith(_KEEP_VORBIS)}  # fmt: skip
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    for k, v in kept.items():
        audio.tags[k] = v
    for k, v in tags.items():
        audio.tags[k] = _as_list(v)
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
            audio.add_picture(pic)


def _write_id3(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    keep = [f for f in id3.values()
            if f.FrameID in ("USLT", "RVA2")
            or (f.FrameID == "TXXX" and f.desc.lower().startswith("replaygain_"))]  # fmt: skip
    id3.clear()
    for f in keep:
        id3.add(f)

    def num(n: str, total: str) -> str:
        return f"{tags[n]}/{tags[total]}" if total in tags else str(tags[n])

    simple = {"title": TIT2, "artist": TPE1, "albumartist": TPE2, "album": TALB,
              "date": TDRC, "originaldate": TDOR, "isrc": TSRC}  # fmt: skip
    for key, frame in simple.items():
        if key in tags:
            id3.add(frame(encoding=3, text=_as_list(tags[key])))
    if "tracknumber" in tags:
        id3.add(TRCK(encoding=3, text=num("tracknumber", "tracktotal")))
    if "discnumber" in tags:
        id3.add(TPOS(encoding=3, text=num("discnumber", "disctotal")))
    if "musicbrainz_trackid" in tags:
        id3.add(UFID(owner="http://musicbrainz.org",
                     data=tags["musicbrainz_trackid"].encode()))  # fmt: skip
    for key, desc in _MB_ID3.items():
        if key in tags:
            id3.add(TXXX(encoding=3, desc=desc, text=_as_list(tags[key])))
    if cover:
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    id3.save(path)


def _write_mp4(audio: MP4, tags: dict[str, Any], cover: bytes | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    kept = {k: v for k, v in audio.tags.items()
            if k == "©lyr" or k.lower().startswith("----:com.apple.itunes:replaygain_")}  # fmt: skip
    audio.tags.clear()
    audio.tags.update(kept)
    for key, atom in _MP4_TEXT.items():
        if key in tags:
            audio.tags[atom] = _as_list(tags[key])
    if "tracknumber" in tags:
        audio.tags["trkn"] = [(tags["tracknumber"], tags.get("tracktotal", 0))]
    if "discnumber" in tags:
        audio.tags["disk"] = [(tags["discnumber"], tags.get("disctotal", 0))]
    for key, name in _MP4_FREEFORM.items():
        if key in tags:
            audio.tags[f"----:com.apple.iTunes:{name}"] = [
                MP4FreeForm(v.encode()) for v in _as_list(tags[key])
            ]
    if cover:
        audio.tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]


def write_tags(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    """Replace the file's tags with ``tags``; ReplayGain and lyrics stay."""
    audio = mutagen.File(path)
    if isinstance(audio, FLAC | OggVorbis | OggOpus):
        _write_vorbis(audio, tags, cover)
        audio.save()
    elif isinstance(audio, MP4):
        _write_mp4(audio, tags, cover)
        audio.save()
    elif audio is not None and path.suffix.lower() == ".mp3":
        _write_id3(path, tags, cover)
    else:
        raise TagFailed("format")


# ------------------------------------------------------------ the service


class TaggingServiceBase(ABC):
    @abstractmethod
    def tag(self, file_id: int) -> bool:
        """Tag a ``verified`` file; False on failTag (the file stays verified)."""


class TaggingService(TaggingServiceBase):
    def __init__(
        self,
        db: Database,
        musicbrainz: MusicBrainzTagSourceBase,
        listenbrainz: ListenBrainzClientBase,
        coverart: CoverArtClientBase,
        box: SecretBox,
        settings: FilesSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._mb = musicbrainz
        self._lb = listenbrainz
        self._caa = coverart
        self._box = box
        self._downloads = Path(settings.music_dir) / settings.slskd_downloads_dir
        self._clock = clock
        self._files = FilesStore()

    def tag(self, file_id: int) -> bool:
        with self._db.transaction() as conn:
            rec = self._files.get(conn, file_id)
            song = SongsStore().get(conn, rec.song_id) if rec else None
            ids = SongsStore().identities(conn, rec.song_id) if rec else {}
        if rec is None or song is None or rec.file_state != "verified":
            return False
        now = int(self._clock())
        mbid: str | None = None
        release_id: str | None = None
        try:
            mbid = ids.get("musicbrainz") or self._lookup(song)
            info = self._mb.recording_info(mbid)
            chosen = choose_release(info.releases)
            if chosen is None:
                raise TagFailed("no_release")
            release_id = chosen.id
            cover: bytes | None = None
            try:
                release = self._mb.release_info(chosen.id)
                tags = build_tags(info, release)
                if release.has_front_cover:
                    cover = self._caa.front(release.id)
            except (TagFailed, CatalogError) as e:  # no disc/track details
                log.info("file %s: release %s details unusable (%s); album only",
                         rec.id, chosen.id, e)  # fmt: skip
                tags = build_album_tags(info, chosen)
            write_tags(self._downloads / rec.file_location, tags, cover)
        except (TagFailed, CatalogError, ListenBrainzError, mutagen.MutagenError) as e:
            error = str(e) if isinstance(e, TagFailed) else f"{type(e).__name__}: {e}"
            with self._db.transaction(write=True) as conn:
                self._files.add_tagging(conn, rec.id, "failTag", now, error=error,
                                        mb_recording_id=mbid, mb_release_id=release_id)  # fmt: skip
            log.info("file %s not tagged: %s", rec.id, error)
            return False
        written = {**tags, "cover": cover is not None}
        with self._db.transaction(write=True) as conn:
            self._files.add_tagging(conn, rec.id, "tagged", now, mb_recording_id=mbid,
                                    mb_release_id=chosen.id, tags_written=written)  # fmt: skip
            current = self._files.get(conn, rec.id)
            if current is None or current.file_state != "verified":
                return False  # replaced meanwhile
            self._files.set_state(conn, rec.id, states.file_tagged("verified"), now)
        log.info("file %s tagged as %s – %s (%s)", rec.id, tags.get("artist"),
                 tags.get("title"), tags.get("album"))  # fmt: skip
        return True

    def _lookup(self, song: Song) -> str:
        """No MBID: ListenBrainz with the requester's token, checked against the song."""
        with self._db.transaction() as conn:
            req = RequestsStore().latest_for_song(conn, song.id)
            user = UsersStore().get(conn, req.user_id) if req else None
        if user is None or not user.lb_token_enc:
            raise TagFailed("no_listenbrainz_token")
        artist = song.main_artist or song.artist_name
        title = song.query_title or song.title
        hit = self._lb.lookup_recording(
            artist, title, self._box.decrypt(user.lb_token_enc)
        )
        if hit is None:
            raise TagFailed("no_recording")
        if norm(artist) not in norm(hit.artist):  # "A" in "A feat. B"
            raise TagFailed("lookup_mismatch")
        if (
            song.duration_s
            and hit.length_s
            and (abs(hit.length_s - song.duration_s) > DURATION_TOLERANCE_S)
        ):
            raise TagFailed("lookup_mismatch")
        return hit.mbid
