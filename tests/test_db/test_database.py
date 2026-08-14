"""
Unit tests for database module.
"""

import pytest
import tempfile
from pathlib import Path

from app.db.database import Database
from app.config import Config


class MockConfig:
    """Mock config for testing."""
    
    class PathsConfig:
        data_dir = ""
    
    def __init__(self, data_dir: str):
        self.paths = self.PathsConfig()
        self.paths.data_dir = data_dir


class TestDatabaseInit:
    """Test database initialization."""
    
    def test_init_creates_db_file(self):
        """Database should create db file on connect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            conn = db.connect()
            
            assert conn is not None
            assert db.db_path.exists()
            
            db.close()
    
    def test_init_creates_parent_dirs(self):
        """Database should create parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "nested" / "deep"
            config = MockConfig(str(nested_path))
            db = Database(config)
            
            conn = db.connect()
            
            assert nested_path.exists()
            
            db.close()


class TestDatabaseSchema:
    """Test schema initialization."""
    
    def test_initialize_schema_creates_tables(self):
        """initialize_schema should create all tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            db.initialize_schema()
            
            # Check tables exist
            tables = db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
            table_names = [t["name"] for t in tables]
            
            assert "searches" in table_names
            assert "downloads" in table_names
            assert "peers" in table_names
            assert "download_retry_attempts" in table_names
            assert "recommendations" in table_names
            assert "sync_state" in table_names
            assert "config" in table_names
            assert "users" in table_names
            assert "sessions" in table_names
            
            db.close()
    
    def test_initialize_schema_idempotent(self):
        """initialize_schema should be idempotent (can run multiple times)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            # Run twice
            db.initialize_schema()
            db.initialize_schema()
            
            # Should not raise
            tables = db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
            assert len(tables) >= 9
            
            db.close()


class TestDatabaseOperations:
    """Test basic database operations."""
    
    def test_execute_insert(self):
        """execute should insert data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            db.initialize_schema()
            
            db.execute(
                "INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)",
                ("testuser", 5, 0)
            )
            
            result = db.fetch_one("SELECT * FROM peers WHERE username = ?", ("testuser",))
            
            assert result is not None
            assert result["username"] == "testuser"
            assert result["failure_count"] == 5
            
            db.close()
    
    def test_fetch_one_returns_dict(self):
        """fetch_one should return dict or None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            db.initialize_schema()
            
            # No data
            result = db.fetch_one("SELECT * FROM peers WHERE username = ?", ("nonexistent",))
            assert result is None
            
            # With data
            db.execute(
                "INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)",
                ("testuser", 0, 0)
            )
            result = db.fetch_one("SELECT * FROM peers WHERE username = ?", ("testuser",))
            
            assert result is not None
            assert isinstance(result, dict)
            assert result["username"] == "testuser"
            
            db.close()
    
    def test_fetch_all_returns_list(self):
        """fetch_all should return list of dicts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            db.initialize_schema()
            
            # Empty
            results = db.fetch_all("SELECT * FROM peers")
            assert results == []
            
            # With data
            db.execute("INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)", ("user1", 0, 0))
            db.execute("INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)", ("user2", 1, 0))
            
            results = db.fetch_all("SELECT * FROM peers ORDER BY username")
            
            assert len(results) == 2
            assert all(isinstance(r, dict) for r in results)
            assert results[0]["username"] == "user1"
            assert results[1]["username"] == "user2"
            
            db.close()


class TestDatabaseTransaction:
    """Test transaction handling."""
    
    def test_transaction_commit(self):
        """transaction should commit on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            db.initialize_schema()
            
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)",
                    ("testuser", 0, 0)
                )
            
            result = db.fetch_one("SELECT * FROM peers WHERE username = ?", ("testuser",))
            assert result is not None
            
            db.close()
    
    def test_transaction_rollback_on_error(self):
        """transaction should rollback on error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            db.initialize_schema()
            
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO peers (username, failure_count, is_blocked) VALUES (?, ?, ?)",
                        ("testuser", 0, 0)
                    )
                    raise ValueError("Simulated error")
            except ValueError:
                pass
            
            # Should not be committed
            result = db.fetch_one("SELECT * FROM peers WHERE username = ?", ("testuser",))
            assert result is None
            
            db.close()


class TestDatabaseConnection:
    """Test connection management."""
    
    def test_connect_returns_same_connection(self):
        """connect should return same connection on multiple calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            conn1 = db.connect()
            conn2 = db.connect()
            
            assert conn1 is conn2
            
            db.close()
    
    def test_close_closes_connection(self):
        """close should close connection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            conn = db.connect()
            db.close()
            
            # Should create new connection
            conn2 = db.connect()
            assert conn2 is not conn
            
            db.close()


class TestDatabaseMigrations:
    """Test migration system."""
    
    def test_migrations_applied_on_init(self):
        """initialize_schema should apply all migrations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            db.initialize_schema()
            
            # Check applied_migrations table exists
            result = db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='applied_migrations'")
            assert result is not None
            
            # Check migration was recorded
            applied = db.fetch_all("SELECT * FROM applied_migrations")
            assert len(applied) > 0
            assert any("001_initial_schema.sql" in m["filename"] for m in applied)
            
            db.close()
    
    def test_migrations_idempotent(self):
        """Running initialize_schema multiple times should not re-apply migrations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = MockConfig(tmpdir)
            db = Database(config)
            
            # First run
            db.initialize_schema()
            applied1 = db.fetch_all("SELECT * FROM applied_migrations")
            
            # Second run
            db.initialize_schema()
            applied2 = db.fetch_all("SELECT * FROM applied_migrations")
            
            # Should be same
            assert len(applied1) == len(applied2)
            
            db.close()
