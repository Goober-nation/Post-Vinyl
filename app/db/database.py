"""
Database module for Musica.

Provides SQLite database connection and schema management.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import Config
from app.logging_config import get_logger

logger = get_logger(__name__)


class Database:
    """
    SQLite database manager.

    Provides connection pooling and schema management.
    """

    def __init__(self, config: Config):
        """
        Initialize Database.

        Args:
            config: Config object with database path
        """
        self.config = config
        self.db_path = Path(config.paths.data_dir) / "musica.db"
        self.migrations_path = Path(__file__).parent / "migrations"
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        """
        Get or create database connection.

        Returns:
            SQLite connection
        """
        if self._connection is None:
            logger.info(f"Connecting to database: {self.db_path}")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode
            )
            self._connection.row_factory = sqlite3.Row
            logger.info("Database connection established")

        return self._connection

    def close(self):
        """Close database connection."""
        if self._connection is not None:
            logger.info("Closing database connection")
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self):
        """
        Context manager for database transactions.

        Usage:
            with db.transaction() as conn:
                conn.execute("INSERT ...")
        """
        conn = self.connect()
        with self._lock:
            try:
                conn.execute("BEGIN")
                yield conn
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                logger.error(f"Transaction failed: {e}")
                raise

    def initialize_schema(self):
        """Create database schema if it doesn't exist."""
        logger.info("Initializing database schema")

        conn = self.connect()

        # Run migrations
        self._run_migrations(conn)

        logger.info("Database schema initialized successfully")

    def _run_migrations(self, conn: sqlite3.Connection):
        """Run all pending migrations."""
        # Get list of migration files
        migration_files = sorted(self.migrations_path.glob("*.sql"))

        if not migration_files:
            logger.warning("No migration files found")
            return

        # Create migrations tracking table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applied_migrations (
                filename TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
        """)

        # Get already applied migrations
        applied = set()
        cursor = conn.execute("SELECT filename FROM applied_migrations")
        for row in cursor.fetchall():
            applied.add(row[0])

        # Apply pending migrations
        for migration_file in migration_files:
            if migration_file.name in applied:
                logger.debug(f"Migration already applied: {migration_file.name}")
                continue

            logger.info(f"Applying migration: {migration_file.name}")

            # Read and execute migration
            with open(migration_file, "r") as f:
                sql = f.read()

            try:
                conn.executescript(sql)

                # Record migration as applied
                import time

                conn.execute(
                    "INSERT INTO applied_migrations (filename, applied_at) VALUES (?, ?)",
                    (migration_file.name, int(time.time())),
                )

                logger.info(f"Migration applied successfully: {migration_file.name}")

            except Exception as e:
                logger.error(f"Migration failed: {migration_file.name} - {e}")
                raise

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """
        Execute a query and return cursor.

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            SQLite cursor
        """
        conn = self.connect()
        with self._lock:
            return conn.execute(query, params)

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        """
        Execute query and fetch one result.

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            Dict representing row, or None
        """
        conn = self.connect()
        with self._lock:
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
        return dict(row) if row else None

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        """
        Execute query and fetch all results.

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            List of dicts representing rows
        """
        conn = self.connect()
        with self._lock:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
