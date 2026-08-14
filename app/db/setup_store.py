"""
SetupStore — SQLite persistence for the first-run setup wizard.

Backs the `setup_state` table (migration 010): a tiny generic key/value
store for onboarding flags (has the wizard run, is the tutorial dismissed).
Separate from RecsStore's worker-state table, which is worker bookkeeping,
not app-level onboarding state.
"""

import time

WIZARD_COMPLETED = "wizard_completed"
TUTORIAL_DISMISSED = "tutorial_dismissed"


class SetupStore:
    """SQLite-backed store for setup_state rows."""

    def __init__(self, database):
        self._db = database

    def get(self, key: str) -> str | None:
        row = self._db.fetch_one(
            "SELECT value FROM setup_state WHERE key = ?", (key,)
        )
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO setup_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, str(int(time.time()))),
        )

    def is_flag_set(self, key: str) -> bool:
        return self.get(key) == "1"

    def set_flag(self, key: str) -> None:
        self.set(key, "1")
