"""SQLite connection and migrations (#84).

Migrations are ``migrations/NNN_name.sql``, applied in order; the applied
number is kept in ``PRAGMA user_version``, so no bookkeeping table is needed.
Each migration runs in one transaction with the version bump, so a failing
one leaves the database as it was.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_\w+\.sql$")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        """A new connection (one per thread): foreign keys on, rows by name."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A connection whose work is committed on success, rolled back on error."""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> int:
        """Apply every migration newer than the database; returns the version."""
        found = sorted(
            (int(m.group(1)), p)
            for p in migrations_dir.iterdir()
            if (m := _MIGRATION_NAME.match(p.name))
        )
        numbers = [n for n, _ in found]
        if numbers != list(range(1, len(numbers) + 1)):
            raise RuntimeError(
                f"migrations must be numbered 001.. without gaps: {numbers}"
            )
        conn = self.connect()
        try:
            version: int = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(found):
                raise RuntimeError(
                    f"database is at version {version}, newer than this app ({len(found)})"
                )
            for number, path in found[version:]:
                script = path.read_text(encoding="utf-8")
                try:
                    conn.executescript(
                        f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                    )
                except sqlite3.Error:
                    conn.rollback()
                    raise
                version = number
            return version
        finally:
            conn.close()
