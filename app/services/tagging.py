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
  exactly one, and a cover from the catalog that comes first for the user:
  Deezer's album when artist and album match, or the Cover Art Archive's
  release group then release; then the other (user, 2026-11-05). Downloads,
  renames and retags all find their recording through ``_match``.
- **The peer's tags**: everything is replaced, except ReplayGain and lyrics.
- One ``tagging`` row per attempt. A failure (``failTag``) never blocks the
  file: it stays ``verified`` and the files service moves it untagged.

Formats: FLAC, MP3, M4A/MP4, Ogg Vorbis/Opus (cover art not written for Ogg).
No new cover leaves the file's old one in place.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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

from app.clients.catalog import Album, CatalogError, CatalogSource
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
from app.core.matcher import DURATION_TOLERANCE_S, norm, same_song
from app.core.normalize import artist_candidates
from app.core.release_choice import choose_release
from app.services.catalog import order_tracks
from app.services.encryption import SecretBox
from app.services.sources import SourceOff, SourcePreferences
from app.stores.database import Database
from app.stores.files import FilesStore
from app.stores.requests import RequestsStore
from app.stores.songs import SongsStore
from app.stores.users import UsersStore

log = logging.getLogger(__name__)

DEEZER_COVER_PX = 1000  # Deezer serves any square size
CAA_COVER_PX = 500  # as the release front
MAX_RENAME_RECORDINGS = 5  # each costs a rate-limited MusicBrainz call


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

# r128_: Opus loudness (RFC 7845), its ReplayGain (live #114)
_KEEP_VORBIS = ("replaygain_", "r128_", "lyrics", "unsyncedlyrics")
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
            if k.lower().startswith(_KEEP_VORBIS)
            or k.lower() == "metadata_block_picture"}  # fmt: skip
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    for k, v in kept.items():
        audio.tags[k] = v
    for k, v in tags.items():
        audio.tags[k] = _as_list(v)
    if isinstance(audio, FLAC) and cover:  # no new cover: the old one stays
        audio.clear_pictures()
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
            or (f.FrameID == "APIC" and not cover)  # no new cover: the old one stays
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
            if k == "©lyr" or (k == "covr" and not cover)
            or k.lower().startswith("----:com.apple.itunes:replaygain_")}  # fmt: skip
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


def strip_tags(path: Path) -> None:
    """Every tag off, cover included (a rename nothing matched, user 2026-10-30)."""
    audio = mutagen.File(path)
    if audio is None:
        raise TagFailed("format")
    if isinstance(audio, FLAC):
        audio.clear_pictures()
    audio.delete()
    if isinstance(audio, FLAC):
        audio.save()


# ------------------------------------------------------------ the service


def fits(found_s: float | None, file_s: float | None) -> bool:
    """A lookup's length against the file's; unknown on either side passes."""
    return not found_s or not file_s or abs(found_s - file_s) <= DURATION_TOLERANCE_S


@dataclass(frozen=True)
class Match:
    mbid: str
    via: str  # "known" | "listenbrainz" | "musicbrainz"
    info: RecordingInfo
    chosen: RecordingRelease


class AlbumCovers(Protocol):
    """What the official cover lookup needs from Deezer."""

    def search_albums(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Album]: ...

    def cover(self, cover_id: str, size: int) -> bytes | None: ...


@dataclass(frozen=True)
class Cover:
    data: bytes
    source: str  # "deezer" | "release group" | "release"


@dataclass(frozen=True)
class Suggestion:
    """The tag set new names point at (#112); the user accepts or strips."""

    recording_id: str
    release_id: str
    via: str  # "listenbrainz" | "musicbrainz"
    tags: dict[str, Any]
    cover: Cover | None


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
        search: CatalogSource | None = None,  # MusicBrainz search for renames (#112)
        sources: SourcePreferences | None = None,  # None: every source on (tests)
        deezer: AlbumCovers | None = None,  # official covers
    ) -> None:
        self._search = search
        self._deezer = deezer
        self._sources = sources
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
            req = RequestsStore().latest_for_song(conn, rec.song_id) if rec else None
        if rec is None or song is None or rec.file_state != "verified":
            return False
        now = int(self._clock())
        mbid: str | None = None
        release_id: str | None = None
        requester = req.user_id if req else None
        why: list[str] = []  # what stopped a lookup, for the failTag row
        try:
            if not self._on(requester, "musicbrainz"):
                raise TagFailed("musicbrainz_off")
            known = ids.get("musicbrainz")
            match = self._match(
                requester, song.query_title or song.title,
                song.main_artist or song.artist_name, "", song.duration_s,
                self._token(requester), known=known, strict=True, why=why,
            )  # fmt: skip
            if match is None:
                reason = next(iter(why), "no_recording")
                raise TagFailed("no_release" if known else reason)
            mbid, info, chosen = match.mbid, match.info, match.chosen
            release_id = chosen.id
            tags, cover = self._build(info, chosen, f"file {rec.id}", requester)
            write_tags(self._downloads / rec.file_location, tags,
                       cover.data if cover else None)  # fmt: skip
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

    def _build(
        self, info: RecordingInfo, chosen: RecordingRelease, what: str,
        user_id: int | None,
    ) -> tuple[dict[str, Any], Cover | None]:  # fmt: skip
        """The tag set from a recording and one of its releases, plus its cover."""
        try:
            release = self._mb.release_info(chosen.id)
            tags = build_tags(info, release)
            cover = self._cover(release, user_id)
        except (TagFailed, CatalogError) as e:  # no disc/track details
            log.info("%s: release %s details unusable (%s); album only",
                     what, chosen.id, e)  # fmt: skip
            return build_album_tags(info, chosen), None
        return tags, cover

    def _cover(self, release: ReleaseInfo, user_id: int | None) -> Cover | None:
        """From the catalog that comes first for the user (Deezer, or MusicBrainz's
        Cover Art Archive), then the other (user, 2026-11-05). A source failing
        only moves on: a cover is never why a song stays untagged."""
        deezer: list[tuple[str, Callable[[], bytes | None]]] = [
            ("deezer", lambda: self._deezer_cover(release, user_id))
        ]
        archive: list[tuple[str, Callable[[], bytes | None]]] = [
            ("release group", lambda: self._group_cover(release)),
            ("release", lambda: self._release_cover(release)),
        ]
        first = self._deezer_first(user_id)
        for source, fetch in deezer + archive if first else archive + deezer:
            try:
                data = fetch()
            except CatalogError as e:
                log.info("cover from %s failed: %s", source, e)
                continue
            if data:
                return Cover(data, source)
        return None

    def _deezer_first(self, user_id: int | None) -> bool:
        if self._sources is None:
            return True
        if user_id is None:
            order = list(self._sources.configured("catalog"))
        else:
            order = self._sources.for_user(user_id, "catalog")
        if "deezer" not in order:
            return False
        return "musicbrainz" not in order or (
            order.index("deezer") < order.index("musicbrainz")
        )

    def _deezer_cover(self, release: ReleaseInfo, user_id: int | None) -> bytes | None:
        if self._deezer is None or not self._on(user_id, "deezer"):
            return None
        albums = self._deezer.search_albums(release.title, release.artist, limit=10)
        for album in albums:
            if album.cover_id and same_song(
                release.title, release.artist, album.title, album.artist
            ):
                return self._deezer.cover(album.cover_id, DEEZER_COVER_PX)
        return None

    def _group_cover(self, release: ReleaseInfo) -> bytes | None:
        if not release.group_id:
            return None
        return self._caa.thumb(f"release-group/{release.group_id}", CAA_COVER_PX)

    def _release_cover(self, release: ReleaseInfo) -> bytes | None:
        return self._caa.front(release.id) if release.has_front_cover else None

    # ------------------------------------------------------------ renames (#112)

    def _on(self, user_id: int | None, source: str) -> bool:
        """Whether ``source`` is on for the user the work is for (user, 2026-10-30)."""
        if self._sources is None:
            return True
        return user_id is not None and self._sources.is_on(user_id, "catalog", source)

    def suggest(
        self, user_id: int, title: str, artist: str, album: str,
        length_s: float | None, token: str | None,
    ) -> Suggestion | None:  # fmt: skip
        """The tag set new names point at, or None. ListenBrainz first (with
        ``token``, same checks as tagging), MusicBrainz search when there's no
        token or ListenBrainz finds nothing, mismatches or fails (user,
        2026-10-30). A typed album picks among the recording's releases. Each
        service only while it's on for ``user_id``; without MusicBrainz no tag
        set can be built, so that refuses."""
        if not self._on(user_id, "musicbrainz"):
            raise SourceOff("musicbrainz is turned off")
        match = self._match(user_id, title, artist, album, length_s, token)
        if match is None:
            return None
        tags, cover = self._build(
            match.info, match.chosen, f"rename suggestion {match.mbid}", user_id
        )
        return Suggestion(match.mbid, match.chosen.id, match.via, tags, cover)

    def _match(
        self, user_id: int | None, title: str, artist: str, album: str,
        length_s: float | None, token: str | None, known: str | None = None,
        strict: bool = False, why: list[str] | None = None,
    ) -> Match | None:  # fmt: skip
        """The one way every tagging finds its recording and release: the known
        id, then each artist name's candidates, first with an official release.
        ``strict``: no one reviews a download, so its search must name the same
        song; a rename is reviewed before it's applied."""
        tried: set[str] = set()
        for mbid, via in self._candidates(
            user_id,
            title,
            artist,
            length_s,
            token,
            known,
            strict,
            [] if why is None else why,
        ):
            if mbid in tried:
                continue
            tried.add(mbid)
            info = self._mb.recording_info(mbid)
            same_album = [
                r for r in info.releases if album and norm(r.title) == norm(album)
            ]
            chosen = choose_release(same_album) or choose_release(info.releases)
            if chosen is not None:
                return Match(mbid, via, info, chosen)
            log.info("recording %s has no official release", mbid)
        return None

    def _candidates(
        self, user_id: int | None, title: str, artist: str,
        length_s: float | None, token: str | None, known: str | None, strict: bool,
        why: list[str],
    ) -> Iterator[tuple[str, str]]:  # fmt: skip
        if known:
            yield known, "known"
        for name in artist_candidates(artist):
            yield from self._recordings(
                user_id, title, name, length_s, token, strict, why
            )

    def _token(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        with self._db.transaction() as conn:
            user = UsersStore().get(conn, user_id)
        return (
            self._box.decrypt(user.lb_token_enc) if user and user.lb_token_enc else None
        )

    def _recordings(
        self, user_id: int | None, title: str, artist: str,
        length_s: float | None, token: str | None, strict: bool, why: list[str],
    ) -> Iterator[tuple[str, str]]:  # fmt: skip
        """One artist name's candidates, best first: ListenBrainz's, then the
        MusicBrainz search's. Lazy, so a good first one costs no search."""
        if not self._on(user_id, "listenbrainz"):
            why.append("listenbrainz_off")
        elif not token:
            why.append("no_listenbrainz_token")
        else:
            try:
                hit = self._lb.lookup_recording(artist, title, token)
            except ListenBrainzError as e:
                log.info("rename lookup at ListenBrainz failed: %s", e)
                hit = None
            if (
                hit
                and norm(artist) in norm(hit.artist)
                and fits(hit.length_s, length_s)
            ):
                yield hit.mbid, "listenbrainz"
            elif hit:
                why.append("lookup_mismatch")
        if self._search is None:
            return
        try:
            found = self._search.search_tracks(title, artist, limit=100)
        except CatalogError as e:
            log.info("rename search at MusicBrainz failed: %s", e)
            return
        fitting = [
            t.id
            for t in order_tracks(title, found)
            if fits(t.duration_s, length_s)
            and (not strict or same_song(title, artist, t.title, t.artist))
        ]
        for mbid in fitting[:MAX_RENAME_RECORDINGS]:
            yield mbid, "musicbrainz"

    def record_retag(
        self, file_id: int, recording_id: str, release_id: str, tags: dict[str, Any]
    ) -> None:
        """A library file tagged by hand: its tagging succeeded after all."""
        with self._db.transaction(write=True) as conn:
            self._files.add_tagging(
                conn, file_id, "tagged", int(self._clock()),
                mb_recording_id=recording_id, mb_release_id=release_id,
                tags_written=tags,
            )  # fmt: skip

    def tags_for(
        self, user_id: int, recording_id: str, release_id: str
    ) -> tuple[dict[str, Any], bytes | None]:
        """A suggestion rebuilt from its ids, so the client never sends tags."""
        if not self._on(user_id, "musicbrainz"):
            raise SourceOff("musicbrainz is turned off")
        info = self._mb.recording_info(recording_id)
        chosen = next((r for r in info.releases if r.id == release_id), None)
        if chosen is None:
            raise TagFailed("release_not_of_recording")
        tags, cover = self._build(info, chosen, f"rename {recording_id}", user_id)
        return tags, cover.data if cover else None
