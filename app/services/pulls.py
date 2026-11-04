"""Pulls: batches of songs a user picks from ListenBrainz (recs or radio).

A pull's songs wait in ``pull_items``. ``feed`` turns the next one into a real
request only when no request is waiting to reach a download, so a user's own
requests always go first and dispatch doesn't change. A song already in the
library skips the download. One running pull per user.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.clients.catalog import CatalogNotFound
from app.clients.listenbrainz import ListenBrainzClientBase, RadioTrack
from app.core.pulls import is_excluded, radio_name, recs_name
from app.core.states import IllegalTransition
from app.services.catalog import CatalogServiceBase
from app.services.dispatch import DownloadDispatchBase, NotStartable
from app.services.encryption import SecretBox
from app.services.requests import RequestServiceBase, StateChanged
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.files import FilesStore
from app.stores.pulls import (
    LbList,
    LbTrack,
    NewItem,
    Pull,
    PullCounts,
    PullItem,
    PullsStore,
)
from app.stores.users import User, UsersStore

log = logging.getLogger(__name__)

SONG_SOURCE = "musicbrainz"  # recording MBIDs resolve here


class NoListenBrainzAccount(Exception):
    """The user has no ListenBrainz token stored."""


class PullRunning(Exception):
    """The user already has a running pull."""


class PullNotFound(LookupError):
    """No such pull or list for this user."""


class NothingToPull(ValueError):
    """The chosen range or tracks hold no song that may be pulled."""


@dataclass(frozen=True)
class RecCheck:
    new: bool
    list: LbList | None  # None: ListenBrainz has no list for the user yet


@dataclass(frozen=True)
class PullView:
    pull: Pull
    counts: PullCounts


class PullServiceBase(ABC):
    @abstractmethod
    def check_recs(self, user_id: int) -> RecCheck:
        """Store the user's current list if it's a new generation."""

    @abstractmethod
    def rec_lists(self, user_id: int) -> list[LbList]: ...

    @abstractmethod
    def rec_tracks(
        self, user_id: int, last_updated: int, first_rank: int, last_rank: int
    ) -> list[LbTrack]:
        """The songs of that range that may be pulled."""

    @abstractmethod
    def radio(self, user_id: int, artist: str, level: str) -> list[RadioTrack]:
        """A fresh radio, excluded versions removed; nothing is stored."""

    @abstractmethod
    def pull_recs(
        self,
        user_id: int,
        last_updated: int,
        first_rank: int,
        last_rank: int,
        name: str | None,
    ) -> Pull: ...

    @abstractmethod
    def pull_radio(
        self,
        user_id: int,
        artist: str,
        level: str,
        tracks: list[RadioTrack],
        name: str | None,
    ) -> Pull: ...

    @abstractmethod
    def pulls(self, user_id: int) -> list[PullView]: ...

    @abstractmethod
    def cancel(self, user_id: int, pull_id: int) -> Pull:
        """Queued songs leave; downloads already started finish."""

    @abstractmethod
    def stop_user(self, user_id: int) -> None:
        """A deleted user: their pulls cancel and requests not yet downloading
        are dropped; downloads already started finish."""

    @abstractmethod
    def feed(self) -> None:
        """One worker tick: end settled pulls, start the next song."""


class PullService(PullServiceBase):
    def __init__(
        self,
        db: Database,
        listenbrainz: ListenBrainzClientBase,
        catalog: CatalogServiceBase,
        requests: RequestServiceBase,
        dispatch: DownloadDispatchBase,
        box: SecretBox,
        sources: SourcePreferences,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._lb = listenbrainz
        self._catalog = catalog
        self._requests = requests
        self._dispatch = dispatch
        self._box = box
        self._sources = sources
        self._clock = clock
        self._pulls = PullsStore()
        self._users = UsersStore()
        self._files = FilesStore()

    def _account(self, user_id: int) -> User:
        self._sources.require(user_id, "catalog", "listenbrainz")
        with self._db.transaction() as conn:
            user = self._users.get(conn, user_id)
        if user is None or not user.lb_token_enc or not user.lb_username:
            raise NoListenBrainzAccount("add your ListenBrainz token in Settings")
        return user

    # ------------------------------------------------------------ choosing

    def check_recs(self, user_id: int) -> RecCheck:
        user = self._account(user_id)
        assert user.lb_username is not None
        recs = self._lb.recommendations(user.lb_username)
        if recs is None:
            return RecCheck(False, None)
        with self._db.transaction() as conn:
            known = self._pulls.has_lb_list(conn, user_id, recs.last_updated)
        if not known:
            meta = self._lb.recording_metadata([r.mbid for r in recs.recordings])
            tracks = []
            for rank, rec in enumerate(recs.recordings, 1):
                m = meta.get(rec.mbid)
                tracks.append(LbTrack(
                    rank, rec.mbid, rec.score, rec.latest_listened_at,
                    m.title if m else None,
                    m.artist if m else None,
                    json.dumps(m.artist_mbids) if m else None,
                    m.release.name if m and m.release else None,
                    m.release.mbid if m and m.release else None,
                    m.length_s if m else None,
                ))  # fmt: skip
            with self._db.transaction(write=True) as conn:
                self._pulls.add_lb_list(
                    conn, user_id, recs.last_updated, int(self._clock()), tracks
                )
        lists = self.rec_lists(user_id)
        current = next(x for x in lists if x.last_updated == recs.last_updated)
        return RecCheck(not known, current)

    def rec_lists(self, user_id: int) -> list[LbList]:
        with self._db.transaction() as conn:
            return self._pulls.lb_lists(conn, user_id)

    def rec_tracks(
        self, user_id: int, last_updated: int, first_rank: int, last_rank: int
    ) -> list[LbTrack]:
        with self._db.transaction() as conn:
            if not self._pulls.has_lb_list(conn, user_id, last_updated):
                raise PullNotFound(f"list {last_updated}")
            tracks = self._pulls.lb_tracks(
                conn, user_id, last_updated, first_rank, last_rank
            )
        return [t for t in tracks if t.title and not is_excluded(t.title)]

    def radio(self, user_id: int, artist: str, level: str) -> list[RadioTrack]:
        user = self._account(user_id)
        assert user.lb_token_enc is not None
        token = self._box.decrypt(user.lb_token_enc)
        tracks = self._lb.radio(artist, level, token)
        return [t for t in tracks if not is_excluded(t.title)]

    # ------------------------------------------------------------ pulling

    def _start(
        self,
        kind: str,
        user_id: int,
        name: str,
        items: list[NewItem],
        **details: object,
    ) -> Pull:
        if not items:
            raise NothingToPull("no songs to pull")
        with self._db.transaction(write=True) as conn:
            if any(p.user_id == user_id for p in self._pulls.running(conn)):
                raise PullRunning("a pull is already running")
            pull = self._pulls.create(
                conn, kind, user_id, name, int(self._clock()), **details
            )
            self._pulls.add_items(conn, pull.id, items)
        return pull

    def pull_recs(
        self,
        user_id: int,
        last_updated: int,
        first_rank: int,
        last_rank: int,
        name: str | None,
    ) -> Pull:
        tracks = self.rec_tracks(user_id, last_updated, first_rank, last_rank)
        items = [NewItem(t.recording_mbid, t.title, t.artist) for t in tracks]
        return self._start(
            "lb_recs", user_id, name or recs_name(int(self._clock())), items,
            lb_last_updated=last_updated, first_rank=first_rank, last_rank=last_rank,
        )  # fmt: skip

    def pull_radio(
        self,
        user_id: int,
        artist: str,
        level: str,
        tracks: list[RadioTrack],
        name: str | None,
    ) -> Pull:
        items = [NewItem(t.mbid, t.title, t.artist) for t in tracks
                 if not is_excluded(t.title)]  # fmt: skip
        return self._start(
            "lb_radio", user_id, name or radio_name(artist, int(self._clock())), items,
            artist=artist, level=level, size=len(items),
        )  # fmt: skip

    def pulls(self, user_id: int) -> list[PullView]:
        with self._db.transaction() as conn:
            return [PullView(p, self._pulls.counts(conn, p.id))
                    for p in self._pulls.for_user(conn, user_id)]  # fmt: skip

    def cancel(self, user_id: int, pull_id: int) -> Pull:
        with self._db.transaction(write=True) as conn:
            pull = self._pulls.get(conn, pull_id)
            if pull is None or pull.user_id != user_id:
                raise PullNotFound(f"pull {pull_id}")
            if pull.state != "running":
                raise StateChanged(f"pull {pull_id} is {pull.state}")
            self._pulls.set_state(conn, pull, "cancelled")
            self._pulls.cancel_queued(conn, pull_id)
            waiting = self._pulls.undownloaded_requests(conn, pull_id)
            cancelled = self._pulls.get(conn, pull_id)
        for request_id in waiting:
            try:
                self._requests.drop(user_id, request_id)
                self._dispatch.cancel(user_id, request_id)
            except (IllegalTransition, StateChanged):
                pass  # it moved on meanwhile: a started download finishes
        assert cancelled is not None
        return cancelled

    def stop_user(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            pulls = [
                p.id
                for p in self._pulls.for_user(conn, user_id)
                if p.state == "running"
            ]
        for pull_id in pulls:
            self.cancel(user_id, pull_id)
        with self._db.transaction() as conn:
            waiting = self._pulls.user_undownloaded_requests(conn, user_id)
        for request_id in waiting:
            try:
                self._requests.drop(user_id, request_id)
                self._dispatch.cancel(user_id, request_id)
            except (IllegalTransition, StateChanged):
                pass  # it moved on meanwhile: a started download finishes

    # ------------------------------------------------------------ the worker

    def feed(self) -> None:
        with self._db.transaction(write=True) as conn:
            running = self._pulls.running(conn)
            for pull in running:
                if self._pulls.is_settled(conn, pull.id):
                    self._pulls.set_state(conn, pull, "done")
            busy = self._pulls.user_requests_waiting(conn)
        if busy:
            return
        for pull in running:
            while item := self._next(pull.id):
                if self._start_item(pull, item):
                    return

    def _next(self, pull_id: int) -> PullItem | None:
        with self._db.transaction() as conn:
            return self._pulls.next_queued(conn, pull_id)

    def _start_item(self, pull: Pull, item: PullItem) -> bool:
        """Whether a request was started; a song needing none lets the next go."""
        try:
            song = self._catalog.resolve(pull.user_id, SONG_SOURCE, item.recording_mbid)
        except CatalogNotFound:
            log.info("pull %s: %s isn't in the catalog", pull.id, item.recording_mbid)
            with self._db.transaction(write=True) as conn:
                self._pulls.set_item(conn, item, "failed")
            return False
        with self._db.transaction(write=True) as conn:
            file = self._files.for_song(conn, song.id)
            if file and file.file_state == "indexed":
                self._pulls.set_item(conn, item, "reused", song_id=song.id)
                return False
        req = self._requests.create(pull.user_id, song.id, pull.kind, pull.id)
        with self._db.transaction(write=True) as conn:
            self._pulls.set_item(
                conn, item, "requested", song_id=song.id, request_id=req.id
            )
        try:
            self._dispatch.dispatch(pull.user_id, req.id)
        except NotStartable:
            pass  # a worker started it meanwhile
        return True
