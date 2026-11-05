"""The only writer to Navidrome playlists.

Each tick, a pull with a newly indexed song gets its playlist rewritten: its
indexed songs, in pull order, under the pull's user. PV owns the list, so a
playlist removed in Navidrome is simply created again.
"""

from __future__ import annotations

from app.clients.navidrome import NavidromeClientBase, NavidromeNotFound, Session
from app.services.encryption import SecretBox
from app.services.navidrome_login import NavidromeLogins
from app.stores.database import Database
from app.stores.pulls import Pull, PullsStore
from app.stores.users import UsersStore


class PlaylistService:
    def __init__(
        self, db: Database, navidrome: NavidromeClientBase, box: SecretBox
    ) -> None:
        self._db = db
        self._nd = navidrome
        self._logins = NavidromeLogins(db, navidrome, box)
        self._pulls = PullsStore()
        self._users = UsersStore()

    def sync(self) -> None:
        with self._db.transaction() as conn:
            ids = self._pulls.pulls_to_sync(conn)
        for pull_id in ids:
            self._sync_pull(pull_id)

    def _login(self, pull: Pull) -> Session | None:
        """The pull's owner, or None until they log in again."""
        with self._db.transaction() as conn:
            user = self._users.get(conn, pull.user_id)
        assert user is not None  # pulls go with their user
        return self._logins.session_for(user)

    def _sync_pull(self, pull_id: int) -> None:
        with self._db.transaction() as conn:
            pull = self._pulls.get(conn, pull_id)
            songs = self._pulls.indexed_songs(conn, pull_id)
        assert pull is not None
        session = self._login(pull)
        if session is None:
            return
        try:
            playlist_id = self._nd.save_playlist(
                session, pull.name, songs, pull.navidrome_playlist_id
            )
        except NavidromeNotFound:
            playlist_id = self._nd.save_playlist(session, pull.name, songs, None)
        with self._db.transaction(write=True) as conn:
            self._pulls.set_playlist(conn, pull, playlist_id)
            self._pulls.mark_in_playlist(conn, pull_id)
