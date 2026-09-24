"""
PlaylistStore — SQLite persistence mapping a stable playlist "role" (trash,
comfort_zone, fresh_picks, deep_cuts) to Navidrome's playlist ID.

Backs the playlist_registry table (migration 011). The only intended
caller outside tests is app.services.playlist_registry.resolve_playlist_id —
see that module for why this mapping exists.
"""

import time


class PlaylistStore:
    """SQLite-backed store mapping playlist role -> Navidrome playlist ID."""

    def __init__(self, database):
        self._db = database

    def get(self, role: str) -> str | None:
        row = self._db.fetch_one(
            "SELECT playlist_id FROM playlist_registry WHERE role = ?", (role,)
        )
        return row["playlist_id"] if row else None

    def set(self, role: str, playlist_id: str) -> None:
        self._db.execute(
            "INSERT INTO playlist_registry (role, playlist_id, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(role) DO UPDATE SET playlist_id = excluded.playlist_id, "
            "updated_at = excluded.updated_at",
            (role, playlist_id, str(int(time.time()))),
        )

    def clear(self, role: str) -> None:
        self._db.execute("DELETE FROM playlist_registry WHERE role = ?", (role,))
