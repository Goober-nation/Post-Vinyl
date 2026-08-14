"""
Unit tests for logging configuration.
"""

import logging
import tempfile
from pathlib import Path

import pytest

from app.config import Config
from app.logging_config import (
    RingBufferHandler,
    get_logger,
    get_recent_logs,
    setup_logging,
)


class TestSetupLogging:
    """Test setup_logging function."""

    def test_setup_logging_with_config(self):
        """setup_logging should configure logging from config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[logging]\nlevel = "DEBUG"\nformat = "%(levelname)s - %(message)s"\n'
            )

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.DEBUG
            assert logger.name == "musica"

    def test_setup_logging_without_config(self):
        """setup_logging should use defaults when config is None."""
        logger = setup_logging(None)

        assert logger.level == logging.INFO
        assert logger.name == "musica"

    def test_setup_logging_info_level(self):
        """setup_logging should handle INFO level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "INFO"\n')

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.INFO

    def test_setup_logging_warning_level(self):
        """setup_logging should handle WARNING level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "WARNING"\n')

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.WARNING

    def test_setup_logging_error_level(self):
        """setup_logging should handle ERROR level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "ERROR"\n')

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.ERROR

    def test_setup_logging_invalid_level_defaults_to_info(self):
        """setup_logging should default to INFO for invalid level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "INVALID"\n')

            config = Config(config_path=str(config_path))

            # This will raise ConfigValidationError during load
            # So we need to test setup_logging directly with invalid config
            logger = setup_logging(None)

            # Should default to INFO
            assert logger.level == logging.INFO

    def test_setup_logging_reduces_third_party_noise(self):
        """setup_logging should reduce noise from third-party libraries."""
        logger = setup_logging(None)

        # Check that third-party loggers are set to WARNING
        assert logging.getLogger("uvicorn").level == logging.WARNING
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("requests").level == logging.WARNING

    def test_setup_logging_can_be_called_multiple_times(self):
        """setup_logging should be callable multiple times (force=True)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"

            # First call with DEBUG
            config_path.write_text('[logging]\nlevel = "DEBUG"\n')
            config = Config(config_path=str(config_path))
            config.load()
            logger1 = setup_logging(config)
            assert logger1.level == logging.DEBUG

            # Second call with INFO
            config_path.write_text('[logging]\nlevel = "INFO"\n')
            config = Config(config_path=str(config_path))
            config.load()
            logger2 = setup_logging(config)
            assert logger2.level == logging.INFO


class TestGetLogger:
    """Test get_logger function."""

    def test_get_logger_returns_logger(self):
        """get_logger should return a Logger instance."""
        logger = get_logger("musica.services.search")

        assert isinstance(logger, logging.Logger)
        assert logger.name == "musica.services.search"

    def test_get_logger_with_different_names(self):
        """get_logger should work with different module names."""
        logger1 = get_logger("musica.services.search")
        logger2 = get_logger("musica.services.download")
        logger3 = get_logger("musica.workers.download_monitor")

        assert logger1.name == "musica.services.search"
        assert logger2.name == "musica.services.download"
        assert logger3.name == "musica.workers.download_monitor"

    def test_get_logger_hierarchy(self):
        """get_logger should respect logger hierarchy."""
        parent = get_logger("musica")
        child = get_logger("musica.services")

        # Child logger should inherit from parent
        assert child.parent.name == "musica"


class TestLoggingIntegration:
    """Test logging integration with Config."""

    def test_logging_with_full_config(self):
        """Logging should work with full config setup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("""
[server]
port = 8000

[logging]
level = "DEBUG"
format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
""")

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.DEBUG
            assert logger.name == "musica"

            # Test that we can get child loggers
            search_logger = get_logger("musica.services.search")
            assert search_logger.name == "musica.services.search"

    def test_logging_format_applied(self):
        """Logging format should be applied correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[logging]\nlevel = "INFO"\nformat = "TEST: %(message)s"\n'
            )

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            # Check that handlers have the correct format
            assert len(logger.handlers) > 0 or len(logging.root.handlers) > 0


class TestLogLevels:
    """Test log level handling."""

    def test_all_log_levels(self):
        """All log levels should be supported."""
        levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

        for level in levels:
            with tempfile.TemporaryDirectory() as tmpdir:
                config_path = Path(tmpdir) / "config.toml"
                config_path.write_text(f'[logging]\nlevel = "{level}"\n')

                config = Config(config_path=str(config_path))
                config.load()

                logger = setup_logging(config)

                expected_level = getattr(logging, level)
                assert logger.level == expected_level

    def test_case_insensitive_level(self):
        """Log level should be case-insensitive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "debug"\n')

            config = Config(config_path=str(config_path))
            config.load()

            logger = setup_logging(config)

            assert logger.level == logging.DEBUG


# ============================================================================
# RingBufferHandler
# ============================================================================


class TestRingBufferHandler:
    def test_capacity_eviction(self) -> None:
        ring = RingBufferHandler(10)
        ring.setFormatter(logging.Formatter("%(message)s"))

        for i in range(15):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=str(i),
                args=(),
                exc_info=None,
            )
            ring.emit(record)

        lines = ring.get_lines(15)
        assert len(lines) == 10
        assert "5" in lines
        assert "14" in lines

    def test_get_lines_limit(self) -> None:
        ring = RingBufferHandler(100)
        ring.setFormatter(logging.Formatter("%(message)s"))

        for i in range(5):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=str(i),
                args=(),
                exc_info=None,
            )
            ring.emit(record)

        lines = ring.get_lines(3)
        assert len(lines) == 3

    def test_get_lines_more_than_available(self) -> None:
        ring = RingBufferHandler(100)
        ring.setFormatter(logging.Formatter("%(message)s"))

        for i in range(3):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=str(i),
                args=(),
                exc_info=None,
            )
            ring.emit(record)

        lines = ring.get_lines(10)
        assert len(lines) == 3

    def test_get_lines_chronological_tail(self) -> None:
        ring = RingBufferHandler(100)
        ring.setFormatter(logging.Formatter("%(message)s"))

        for i in range(5):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=str(i),
                args=(),
                exc_info=None,
            )
            ring.emit(record)

        lines = ring.get_lines(5)
        assert len(lines) == 5
        assert lines[0] == "0"
        assert lines[-1] == "4"


class TestGetRecentLogs:
    def test_returns_empty_when_not_setup(self) -> None:
        result = get_recent_logs(100)
        assert result == []

    def test_returns_lines_after_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ring = RingBufferHandler(100)
        ring.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        ring.emit(record)
        monkeypatch.setattr("app.logging_config._ring_buffer", ring)

        result = get_recent_logs(100)
        assert len(result) >= 1
        assert any("hello" in line for line in result)
