"""
Musica Logging Configuration

Provides structured logging with configurable levels and formats.
Uses Python's built-in logging module.
"""

import logging
import sys
from collections import deque

from app.config import Config


class RingBufferHandler(logging.Handler):
    """Thread-safe in-memory ring buffer for recent log lines.

    Appends formatted records to a bounded deque; oldest lines
    are evicted when capacity is reached.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buffer: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        """Format and append a log record to the buffer."""
        lock = self.lock
        assert lock is not None
        with lock:
            self._buffer.append(self.format(record))

    def get_lines(self, limit: int) -> list[str]:
        """Return up to *limit* most-recent formatted lines.

        Chronological order (oldest first within the window), like the tail
        of a log file.
        """
        lock = self.lock
        assert lock is not None
        with lock:
            return list(reversed(self._buffer))[-limit:][::-1]


# Set by setup_logging; read by get_recent_logs.
_ring_buffer: RingBufferHandler | None = None


def get_recent_logs(limit: int) -> list[str]:
    """Return most-recent log lines from the ring buffer.

    Chronological order (oldest first within the window).
    Returns [] if setup_logging has not been called.
    """
    if _ring_buffer is None:
        return []
    return _ring_buffer.get_lines(limit)


def setup_logging(config: Config | None = None) -> logging.Logger:
    """
    Configure logging for the application.

    Args:
        config: Config object with logging settings. If None, uses defaults.

    Returns:
        Root logger for the application

    Usage:
        config = Config()
        config.load()
        logger = setup_logging(config)
        logger.info("Application started")
    """
    if config is None:
        level = "INFO"
        format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    else:
        level = config.logging.level
        format_str = config.logging.format

    # Convert level string to logging constant
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    log_level = level_map.get(level.upper(), logging.INFO)

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=format_str,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,  # Override any existing configuration
    )

    # Reduce noise from third-party libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # Attach ring buffer for /api/logs endpoint
    global _ring_buffer
    ring = RingBufferHandler(500)
    ring.setFormatter(logging.Formatter(format_str))
    logging.getLogger().addHandler(ring)
    _ring_buffer = ring

    # Get root logger for Musica
    logger = logging.getLogger("musica")
    logger.setLevel(log_level)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a specific module.

    Args:
        name: Module name (e.g., "musica.services.search")

    Returns:
        Logger instance

    Usage:
        logger = get_logger("musica.services.search")
        logger.info("Search initiated")
    """
    return logging.getLogger(name)
