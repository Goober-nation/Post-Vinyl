"""
Musica Exception Hierarchy

All custom exceptions inherit from MusicaError base class.
Each exception includes:
- message: Human-readable error message
- code: Machine-readable error code (for API responses)
- details: Optional dict with additional context
"""


class MusicaError(Exception):
    """
    Base exception for all Musica errors.
    
    Attributes:
        message: Human-readable error message
        code: Machine-readable error code
        details: Optional dict with additional context
    """
    
    def __init__(self, message: str, code: str = "UNKNOWN_ERROR", details: dict = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
    
    def __str__(self):
        return f"[{self.code}] {self.message}"
    
    def to_dict(self) -> dict:
        """Convert exception to dict for API responses."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details
            }
        }


# ============================================================================
# Search Errors
# ============================================================================

class SearchError(MusicaError):
    """Base exception for search-related errors."""
    pass


class SearchNotFoundError(SearchError):
    """Raised when a search ID is not found."""
    
    def __init__(self, search_id: str):
        super().__init__(
            message=f"Search '{search_id}' not found",
            code="SEARCH_NOT_FOUND",
            details={"search_id": search_id}
        )


class SearchInitiationError(SearchError):
    """Raised when search initiation fails."""
    
    def __init__(self, query: str, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to initiate search for '{query}': {reason}",
            code="SEARCH_INITIATION_FAILED",
            details={"query": query, "reason": reason}
        )


class SearchTimeoutError(SearchError):
    """Raised when search times out."""

    def __init__(self, search_id: str, timeout_seconds: int):
        super().__init__(
            message=f"Search '{search_id}' timed out after {timeout_seconds}s",
            code="SEARCH_TIMEOUT",
            details={"search_id": search_id, "timeout_seconds": timeout_seconds}
        )


class SearchRateLimitedError(SearchError):
    """Raised when SearchRateLimiter's wait timeout elapses before a slot
    frees up.

    Exists because a single Soulseek search fans out network-wide and briefly
    opens on the order of thousands of peer connections (diagnosed
    2026-08-13). One search drains on its own; searches fired back to back
    do not, because the spikes superimpose. The rate limiter bounds how
    often SlskdSearch is allowed to start a new one — this is what it raises
    when a caller has been waiting for a free slot longer than it's willing
    to wait.
    """

    def __init__(self, max_searches: int, window_seconds: float):
        super().__init__(
            message=(
                f"Too many searches: limit is {max_searches} per "
                f"{window_seconds:.0f}s and no slot freed up in time"
            ),
            code="SEARCH_RATE_LIMITED",
            details={"max_searches": max_searches, "window_seconds": window_seconds},
        )


# ============================================================================
# Download Errors
# ============================================================================

class DownloadError(MusicaError):
    """Base exception for download-related errors."""
    pass


class TransferNotFoundError(DownloadError):
    """Raised when a transfer ID is not found."""
    
    def __init__(self, transfer_id: str):
        super().__init__(
            message=f"Transfer '{transfer_id}' not found",
            code="TRANSFER_NOT_FOUND",
            details={"transfer_id": transfer_id}
        )


class NoViablePeerError(DownloadError):
    """Raised when no viable peer is available for download."""
    
    def __init__(self, filename: str, reason: str = "No peers with free upload slots"):
        super().__init__(
            message=f"No viable peer found for '{filename}': {reason}",
            code="NO_VIABLE_PEER",
            details={"filename": filename, "reason": reason}
        )


class InvalidDestinationError(DownloadError):
    """Raised when a requested download destination escapes the configured
    download directories (path traversal)."""

    def __init__(self, destination: str):
        super().__init__(
            message=f"Destination '{destination}' is not under a configured download directory",
            code="INVALID_DESTINATION",
            details={"destination": destination},
        )


class QueueError(DownloadError):
    """Raised when download queue operation fails."""
    
    def __init__(self, username: str, files: list, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to queue downloads from {username}: {reason}",
            code="QUEUE_FAILED",
            details={"username": username, "files": files, "reason": reason}
        )


class MaxRetriesExceededError(DownloadError):
    """Raised when max retry attempts exceeded."""
    
    def __init__(self, transfer_id: str, max_retries: int):
        super().__init__(
            message=f"Max retries ({max_retries}) exceeded for transfer '{transfer_id}'",
            code="MAX_RETRIES_EXCEEDED",
            details={"transfer_id": transfer_id, "max_retries": max_retries}
        )


# ============================================================================
# Service Connection Errors
# ============================================================================

class ServiceConnectionError(MusicaError):
    """Base exception for external service connection errors."""
    pass


class NavidromeConnectionError(ServiceConnectionError):
    """Raised when cannot connect to Navidrome."""
    
    def __init__(self, url: str, reason: str = "Connection failed"):
        super().__init__(
            message=f"Cannot connect to Navidrome at {url}: {reason}",
            code="NAVIDROME_CONNECTION_FAILED",
            details={"url": url, "reason": reason}
        )


class SlskdConnectionError(ServiceConnectionError):
    """Raised when cannot connect to slskd."""
    
    def __init__(self, url: str, reason: str = "Connection failed"):
        super().__init__(
            message=f"Cannot connect to slskd at {url}: {reason}",
            code="SLSKD_CONNECTION_FAILED",
            details={"url": url, "reason": reason}
        )


class ListenBrainzConnectionError(ServiceConnectionError):
    """Raised when cannot connect to ListenBrainz."""
    
    def __init__(self, url: str, reason: str = "Connection failed"):
        super().__init__(
            message=f"Cannot connect to ListenBrainz at {url}: {reason}",
            code="LISTENBRAINZ_CONNECTION_FAILED",
            details={"url": url, "reason": reason}
        )


class MusicBrainzConnectionError(ServiceConnectionError):
    """Raised when cannot connect to MusicBrainz."""

    def __init__(self, url: str, reason: str = "Connection failed"):
        super().__init__(
            message=f"Cannot connect to MusicBrainz at {url}: {reason}",
            code="MUSICBRAINZ_CONNECTION_FAILED",
            details={"url": url, "reason": reason}
        )


# ============================================================================
# MusicBrainz-Specific Errors
# ============================================================================

class MusicBrainzError(MusicaError):
    """Base exception for MusicBrainz-related errors."""
    pass


class MusicBrainzNotFoundError(MusicBrainzError):
    """Raised when a MusicBrainz entity (recording/artist/release-group) is
    not found, or a caller supplies a malformed/empty MBID."""

    def __init__(self, entity: str, mbid: str):
        super().__init__(
            message=f"MusicBrainz {entity} '{mbid}' not found or invalid",
            code="MUSICBRAINZ_NOT_FOUND",
            details={"entity": entity, "mbid": mbid}
        )


class MusicBrainzRateLimitError(MusicBrainzError):
    """Raised when MusicBrainz throttles us (HTTP 503).

    Distinct from a connection failure on purpose: being rate-limited means
    the client is misbehaving and the fix is to slow down, not to retry
    harder. MusicBrainz publishes a 1 req/sec average limit for anonymous
    clients and answers 503 when it is exceeded.
    """

    def __init__(self, retry_after: float | None = None):
        super().__init__(
            message=(
                "MusicBrainz rate limit exceeded"
                + (f"; retry after {retry_after}s" if retry_after else "")
            ),
            code="MUSICBRAINZ_RATE_LIMITED",
            details={"retry_after": retry_after}
        )
        self.retry_after = retry_after


# ============================================================================
# ListenBrainz-Specific Errors
# ============================================================================

class ListenBrainzError(MusicaError):
    """Base exception for ListenBrainz-related errors."""
    pass


class ListenBrainzDisabledError(ListenBrainzError):
    """Raised when ListenBrainz integration is disabled."""
    
    def __init__(self):
        super().__init__(
            message="ListenBrainz integration is disabled. Set [listenbrainz] enabled = true in config.",
            code="LISTENBRAINZ_DISABLED",
            details={}
        )


class ListenBrainzFeedbackError(ListenBrainzError):
    """Raised when feedback submission fails."""
    
    def __init__(self, mbid: str, score: int, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to submit feedback for MBID '{mbid}' (score={score}): {reason}",
            code="LISTENBRAINZ_FEEDBACK_FAILED",
            details={"mbid": mbid, "score": score, "reason": reason}
        )


# ============================================================================
# Library Errors
# ============================================================================

class LibraryError(MusicaError):
    """Base exception for library-related errors."""
    pass


class PlaylistNotFoundError(LibraryError):
    """Raised when a playlist ID is not found."""
    
    def __init__(self, playlist_id: str):
        super().__init__(
            message=f"Playlist '{playlist_id}' not found",
            code="PLAYLIST_NOT_FOUND",
            details={"playlist_id": playlist_id}
        )


class PlaylistError(LibraryError):
    """Raised when playlist operation fails."""
    
    def __init__(self, playlist_id: str, operation: str, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to {operation} playlist '{playlist_id}': {reason}",
            code="PLAYLIST_OPERATION_FAILED",
            details={"playlist_id": playlist_id, "operation": operation, "reason": reason}
        )


# ============================================================================
# Recommendation Errors
# ============================================================================

class RecommendationError(MusicaError):
    """Base exception for recommendation-related errors."""
    pass


class RecommendationFetchError(RecommendationError):
    """Raised when fetching recommendations fails."""
    
    def __init__(self, source: str, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to fetch recommendations from {source}: {reason}",
            code="RECOMMENDATION_FETCH_FAILED",
            details={"source": source, "reason": reason}
        )


class ClassificationError(RecommendationError):
    """Raised when classification fails."""
    
    def __init__(self, reason: str = "Unknown error"):
        super().__init__(
            message=f"Failed to classify recommendations: {reason}",
            code="CLASSIFICATION_FAILED",
            details={"reason": reason}
        )


# ============================================================================
# Configuration Errors
# ============================================================================

class ConfigError(MusicaError):
    """Base exception for configuration-related errors."""
    pass


class ConfigValidationError(ConfigError):
    """Raised when configuration validation fails."""
    
    def __init__(self, key: str, value, reason: str = "Invalid value"):
        super().__init__(
            message=f"Configuration validation failed for '{key}': {reason} (got: {value})",
            code="CONFIG_VALIDATION_FAILED",
            details={"key": key, "value": str(value), "reason": reason}
        )


class ConfigNotFoundError(ConfigError):
    """Raised when required configuration is missing."""
    
    def __init__(self, key: str):
        super().__init__(
            message=f"Required configuration '{key}' not found",
            code="CONFIG_NOT_FOUND",
            details={"key": key}
        )
