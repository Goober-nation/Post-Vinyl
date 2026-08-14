"""
SlskdDownload — Concrete implementation of DownloadService using slskd REST API.

Handles downloading files from Soulseek peers via slskd.
"""

import os
import urllib.parse
from datetime import datetime

import requests

from app.config import Config
from app.exceptions import (
    MaxRetriesExceededError,
    NoViablePeerError,
    QueueError,
    SlskdConnectionError,
    TransferNotFoundError,
)
from app.logging_config import get_logger
from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    RetryResult,
    Transfer,
)

logger = get_logger(__name__)


class SlskdDownload(DownloadService):
    """
    slskd-based download implementation.

    Uses the slskd REST API to download files from Soulseek peers.
    """

    def __init__(self, config: Config, store=None):
        """
        Initialize SlskdDownload.

        Args:
            config: Config object with slskd settings
            store: Optional DownloadStore (SQLite) — used to resolve a
                transfer's search_id so retry can re-fetch that search's
                peers from slskd after a restart. Without it (e.g. tests),
                retry only sees whatever was set via store_search_responses().
        """
        self.config = config
        self.base_url = config.slskd.url
        self.api_key = config.slskd.api_key
        self.session = requests.Session()

        # In-memory storage. The search-responses cache below is a
        # within-process cache only: on a miss, retry() re-fetches the
        # search's peers from slskd, which is where they actually live.
        self._transfers: dict[str, Transfer] = {}
        self._search_responses: dict[str, list[dict]] = {}  # tracker_key -> responses
        self._tried_peers: dict[str, list[str]] = {}  # tracker_key -> [usernames]
        self._retry_counts: dict[str, int] = {}  # tracker_key -> count
        self._blocked_peers: set[str] = set()
        self._bad_peers: dict[str, int] = {}  # username -> failure_count

        # Optional SQLite-backed store (resolves filename -> search_id)
        self._store = store

        # Audio extensions
        self.allowed_extensions = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}

    def _get_headers(self) -> dict:
        """Get HTTP headers for slskd API."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def queue(
        self,
        username: str,
        files: list[dict],
        search_id: str | None = None,
        destination: str | None = None,
    ) -> QueueResult:
        """
        Queue files for download from a peer.

        Args:
            username: Peer username
            files: List of file dicts with 'filename' and 'size' keys
            search_id: Optional search ID
            destination: Optional destination directory

        Returns:
            QueueResult with enqueued count and failures
        """
        logger.info(f"Queueing {len(files)} files from {username}")

        url = f"{self.base_url}/api/v0/transfers/downloads/batches"
        payload = {
            "username": username,
            "files": [
                {"filename": f["filename"], "size": f.get("size", 0)} for f in files
            ],
        }

        if search_id:
            payload["searchId"] = search_id
        if destination:
            payload["options"] = {"destination": destination}

        try:
            # slskd's enqueue endpoint blocks synchronously while it negotiates
            # a direct-or-indirect P2P connection to the peer before responding
            # — this can legitimately take 20s+ under indirect fallback. A short
            # timeout here doesn't make the queue fail faster, it just makes us
            # give up and report a false "connection failed" before slskd's own
            # real answer (success or a proper peer-unreachable failure) comes
            # back. Confirmed via logs: slskd was still negotiating at the 20s
            # mark while a 15s client timeout had already errored the request.
            resp = self.session.post(
                url, json=payload, headers=self._get_headers(), timeout=45
            )

            if resp.status_code == 201:
                # All files enqueued
                enqueued = len(files)
                failures = []
                logger.info(f"All {enqueued} files enqueued from {username}")
            elif resp.status_code in (200, 207):
                # Partial success
                data = resp.json()
                failures = data.get("failures", [])
                enqueued = len(files) - len(failures)
                logger.info(
                    f"{enqueued} enqueued, {len(failures)} failed from {username}"
                )
            else:
                # Complete failure
                logger.error(f"Queue failed: HTTP {resp.status_code}")
                raise QueueError(
                    username, [f["filename"] for f in files], f"HTTP {resp.status_code}"
                )

            # Create transfer objects for enqueued files
            for file in files:
                filename = file["filename"]
                # Check if this file failed
                if any(f["filename"] == filename for f in failures):
                    continue

                # Generate transfer ID
                transfer_id = f"{username}:{filename}:{datetime.now().timestamp()}"

                transfer = Transfer(
                    transfer_id=transfer_id,
                    username=username,
                    filename=filename,
                    size=file.get("size", 0),
                    state="queued",
                    progress=0.0,
                    speed=None,
                    started_at=datetime.now(),
                    completed_at=None,
                    is_rec_download=destination and "discovery" in destination.lower(),
                )

                self._transfers[transfer_id] = transfer

                # Store search responses for retry
                if search_id:
                    tracker_key = f"{username}:{filename}"
                    # Left empty on purpose: retry() fetches this search's
                    # peers from slskd on demand rather than caching a copy
                    # of data slskd already holds.
                    self._search_responses.setdefault(tracker_key, [])

            return QueueResult(
                enqueued_count=enqueued, failures=failures, search_id=search_id
            )

        except requests.exceptions.RequestException as e:
            logger.error(f"Queue connection error: {e}")
            raise SlskdConnectionError(self.base_url, str(e))

    def get_status(self) -> list[Transfer]:
        """
        Get status of all active downloads.

        Returns:
            List of Transfer objects
        """
        logger.debug("Fetching transfer status")

        url = f"{self.base_url}/api/v0/transfers/downloads"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=10)

            if resp.status_code != 200:
                logger.error(f"Failed to get transfers: HTTP {resp.status_code}")
                return list(self._transfers.values())

            data = resp.json()

            transfers = self._parse_status_groups(data)

            # Update in-memory storage
            for transfer in transfers:
                self._transfers[transfer.transfer_id] = transfer

            return transfers

        except requests.exceptions.RequestException as e:
            logger.error(f"Get status connection error: {e}")
            raise SlskdConnectionError(self.base_url, str(e))

    def _parse_status_groups(self, data) -> list[Transfer]:
        """Parse slskd's grouped transfer payload into Transfer objects.

        Both the downloads and uploads endpoints return the same shape:
        a list of per-user groups containing directories of files.
        """
        transfers = []
        if isinstance(data, list):
            # New format: list of user groups
            for user_group in data:
                username = user_group.get("username", "")
                for directory in user_group.get("directories", []):
                    for file in directory.get("files", []):
                        transfer = self._parse_transfer(file, username)
                        if transfer:
                            transfers.append(transfer)
        elif isinstance(data, dict):
            # Old format: dict by username
            for username, user_transfers in data.items():
                if isinstance(user_transfers, list):
                    for file in user_transfers:
                        transfer = self._parse_transfer(file, username)
                        if transfer:
                            transfers.append(transfer)
        return transfers

    def get_upload_status(self) -> list[Transfer]:
        """Get slskd's upload history as Transfer objects (best-effort).

        Same grouped payload shape as downloads (live-verified the endpoint
        exists on 0.26.0); uploads are not merged into `_transfers` — that
        dict is download-scoped and drives download-only operations.
        """
        logger.debug("Fetching upload status")

        url = f"{self.base_url}/api/v0/transfers/uploads"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=10)
        except requests.exceptions.RequestException as e:
            logger.error(f"Get upload status connection error: {e}")
            raise SlskdConnectionError(self.base_url, str(e))

        if resp.status_code != 200:
            logger.error(f"Failed to get uploads: HTTP {resp.status_code}")
            return []

        return self._parse_status_groups(resp.json())

    def delete_upload_transfer(self, transfer_id: str, username: str) -> bool:
        """Permanently remove a terminal upload record from slskd history.

        Same `remove=true` semantics as delete_transfer() but against the
        uploads endpoint (live-verified route exists on 0.26.0). Best-effort,
        never raises.
        """
        if not transfer_id or ":" in transfer_id:
            logger.warning(f"Delete upload skipped: unusable id {transfer_id!r}")
            return False

        url = (
            f"{self.base_url}/api/v0/transfers/uploads/"
            f"{urllib.parse.quote(username)}/{urllib.parse.quote(str(transfer_id))}"
            f"?remove=true"
        )

        try:
            resp = self.session.delete(url, headers=self._get_headers(), timeout=10)
        except requests.exceptions.RequestException as e:
            logger.error(f"Upload delete connection error: {e}")
            return False

        if resp.status_code in (200, 204):
            logger.info(f"Upload transfer deleted: {transfer_id}")
            return True
        logger.warning(f"Upload delete failed: HTTP {resp.status_code}")
        return False

    def fetch_search_responses(self, search_id: str) -> list[dict]:
        """
        Fetch stored peer responses for a search (for retry).

        Args:
            search_id: The slskd search ID

        Returns:
            List of peer response dicts

        Raises:
            SlskdConnectionError: If the request fails
        """
        url = f"{self.base_url}/api/v0/searches/{search_id}/responses"
        try:
            resp = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                raise SlskdConnectionError(self.base_url, f"HTTP {resp.status_code}")
            data = resp.json()
            if not isinstance(data, list):
                raise SlskdConnectionError(
                    self.base_url, "Unexpected response format (not a list)"
                )
            return data
        except requests.exceptions.RequestException as e:
            raise SlskdConnectionError(self.base_url, str(e))

    def _parse_transfer(self, file: dict, username: str) -> Transfer | None:
        """Parse a transfer from slskd response."""
        filename = file.get("filename", "")
        transfer_id = file.get("id", f"{username}:{filename}")
        state = file.get("state", "unknown").lower()

        # Map slskd states to our states.
        # slskd TransferStates: Requested, Queued, Initializing, InProgress,
        # Completed (+ Succeeded/Cancelled/TimedOut/Errored/Rejected/Aborted).
        fail_reason = None
        if "completed" in state and "succeeded" in state:
            mapped_state = "completed"
        elif "cancelled" in state:
            mapped_state = "cancelled"
        elif any(
            kw in state for kw in ["error", "failed", "timedout", "rejected", "aborted"]
        ):
            mapped_state = "failed"
            for kw in ["timedout", "aborted", "rejected", "errored", "failed"]:
                if kw in state:
                    fail_reason = kw
                    break
            else:
                fail_reason = "error"
        elif "inprogress" in state or "downloading" in state or "initializing" in state:
            mapped_state = "downloading"
        elif "queued" in state or "requested" in state:
            mapped_state = "queued"
        else:
            mapped_state = "queued"

        # Calculate progress
        bytes_transferred = file.get("bytesTransferred", 0)
        size = file.get("size", 0)
        progress = (bytes_transferred / size * 100) if size > 0 else 0.0

        return Transfer(
            transfer_id=transfer_id,
            username=username,
            filename=filename,
            size=size,
            state=mapped_state,
            progress=progress,
            speed=file.get("averageSpeed"),
            started_at=datetime.now(),  # Note: slskd doesn't provide start time
            completed_at=datetime.now() if mapped_state == "completed" else None,
            is_rec_download=False,  # We'd need to track this separately
            fail_reason=fail_reason,
        )

    def retry(self, transfer_id: str) -> RetryResult:
        """
        Retry a failed download from stored search results.

        Args:
            transfer_id: ID of failed transfer

        Returns:
            RetryResult with success status and new transfer ID
        """
        logger.info(f"Retrying transfer: {transfer_id}")

        if transfer_id not in self._transfers:
            raise TransferNotFoundError(transfer_id)

        old_transfer = self._transfers[transfer_id]
        username = old_transfer.username
        filename = old_transfer.filename
        tracker_key = f"{username}:{filename}"

        # Check retry count
        current_count = self._retry_counts.get(tracker_key, 0)
        if current_count >= self.config.download.max_retries_per_track:
            raise MaxRetriesExceededError(
                transfer_id, self.config.download.max_retries_per_track
            )

        # Get stored search responses
        stored_responses = self._search_responses.get(tracker_key, [])
        if not stored_responses:
            # After a restart the in-memory cache is empty — re-fetch this
            # track's search from slskd, which retains it.
            stored_responses = self._fetch_responses_for_track(username, filename)
        if not stored_responses:
            raise NoViablePeerError(filename, "No stored search responses available")

        # Get tried peers
        tried = set(self._tried_peers.get(tracker_key, []))
        tried.add(username)  # Skip original peer

        # Find alternative peer
        for response in stored_responses:
            peer = response.get("username", "")

            # Skip if already tried
            if peer in tried:
                continue

            # Skip if blocked
            if peer.lower() in self._blocked_peers:
                continue

            # Skip if bad peer
            if self._bad_peers.get(peer, 0) >= self.config.download.bad_peer_threshold:
                continue

            # Skip if no free slot
            if not response.get("hasFreeUploadSlot"):
                continue

            # Find audio file
            peer_files = response.get("files", [])
            candidate_file = None
            for f in peer_files:
                fname = f.get("filename", "")
                ext = os.path.splitext(fname)[1].lower()
                if ext in self.allowed_extensions:
                    candidate_file = f
                    break

            if not candidate_file:
                continue

            # Try to queue from this peer
            logger.info(f"Retry {tracker_key}: trying peer {peer}")

            try:
                result = self.queue(peer, [candidate_file])

                if result.enqueued_count > 0:
                    # Success - delete old transfer
                    self.cancel(transfer_id)

                    # Update tracking
                    self._tried_peers.setdefault(tracker_key, []).append(peer)
                    self._retry_counts[tracker_key] = current_count + 1

                    # Get new transfer ID
                    new_transfer_id = list(self._transfers.keys())[-1]

                    logger.info(f"Retry successful: queued from {peer}")
                    return RetryResult(
                        success=True,
                        message=f"Retrying from {peer}",
                        new_transfer_id=new_transfer_id,
                    )
                else:
                    # Queue failed, try next peer
                    tried.add(peer)
                    continue

            except Exception as e:
                logger.warning(f"Queue from {peer} failed: {e}")
                tried.add(peer)
                continue

        # No viable peer found
        self._retry_counts[tracker_key] = current_count + 1
        raise NoViablePeerError(filename, "No alternative peer with free upload slot")

    @staticmethod
    def _extract_download_id(transfer_id: str) -> str:
        """Parse the slskd download GUID out of a transfer_id.

        Format: username:filename:timestamp — the GUID is the timestamp
        segment. Falls back to the whole transfer_id if it doesn't match.
        """
        parts = transfer_id.split(":")
        if len(parts) >= 3:
            return parts[2]
        return transfer_id

    def cancel(self, transfer_id: str) -> bool:
        """
        Cancel an active download.

        Args:
            transfer_id: ID of transfer to cancel

        Returns:
            True if cancelled successfully
        """
        logger.info(f"Cancelling transfer: {transfer_id}")

        if transfer_id not in self._transfers:
            raise TransferNotFoundError(transfer_id)

        transfer = self._transfers[transfer_id]
        username = transfer.username
        download_id = self._extract_download_id(transfer_id)

        url = f"{self.base_url}/api/v0/transfers/downloads/{urllib.parse.quote(username)}/{urllib.parse.quote(str(download_id))}"

        try:
            resp = self.session.delete(url, headers=self._get_headers(), timeout=10)

            if resp.status_code in (200, 204):
                self._transfers[transfer_id].state = "cancelled"
                logger.info(f"Transfer cancelled: {transfer_id}")
                return True
            else:
                logger.warning(f"Cancel failed: HTTP {resp.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Cancel connection error: {e}")
            return False

    def delete_transfer(self, transfer_id: str) -> bool:
        """
        Permanently remove a finished transfer record from slskd via
        DELETE .../transfers/downloads/{username}/{id}?remove=true.

        Unlike cancel(), ``remove=true`` tells slskd to forget the record
        entirely rather than just mark it cancelled, so it won't reappear
        on the next get_status() poll.

        Args:
            transfer_id: ID of transfer to delete

        Returns:
            True if slskd confirmed removal, False otherwise (best-effort;
            never raises)
        """
        logger.info(f"Deleting transfer: {transfer_id}")

        transfer = self._transfers.get(transfer_id)
        if transfer is None:
            logger.warning(f"Delete failed: unknown transfer {transfer_id}")
            return False

        username = transfer.username
        download_id = self._extract_download_id(transfer_id)
        url = (
            f"{self.base_url}/api/v0/transfers/downloads/"
            f"{urllib.parse.quote(username)}/{urllib.parse.quote(str(download_id))}"
            f"?remove=true"
        )

        try:
            resp = self.session.delete(url, headers=self._get_headers(), timeout=10)

            if resp.status_code in (200, 204):
                self._transfers.pop(transfer_id, None)
                logger.info(f"Transfer deleted: {transfer_id}")
                return True
            else:
                logger.warning(f"Delete failed: HTTP {resp.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Delete connection error: {e}")
            return False

    def get_transfer(self, transfer_id: str) -> Transfer:
        """
        Get details of a specific transfer.

        Args:
            transfer_id: ID of transfer

        Returns:
            Transfer object
        """
        if transfer_id not in self._transfers:
            raise TransferNotFoundError(transfer_id)

        return self._transfers[transfer_id]

    def store_search_responses(self, tracker_key: str, responses: list[dict]):
        """
        Store search responses for retry (called by SlskdSearch).

        Args:
            tracker_key: username:filename
            responses: List of peer responses
        """
        self._search_responses[tracker_key] = responses

    def _fetch_responses_for_track(self, username: str, filename: str) -> list[dict]:
        """Re-fetch the peers from the search this track came from.

        Looks the track's search_id up in the downloads table, then asks
        slskd for that search's responses. This is what makes alternative-peer
        retry survive a restart without musica keeping its own copy of the
        results — and it is *not* a fresh search: no new search is initiated,
        the same completed search is re-read, so retry still picks from the
        same candidate pool. Returns [] when there's no store, no search_id,
        or slskd can't be reached.
        """
        if self._store is None:
            return []
        search_id = self._store.get_pending_search_id(username, filename)
        if not search_id:
            return []
        try:
            return self.fetch_search_responses(search_id)
        except SlskdConnectionError as e:
            logger.warning(f"Could not re-fetch responses for {search_id}: {e}")
            return []

    def mark_peer_bad(self, username: str):
        """
        Mark a peer as bad (increment failure count).

        Args:
            username: Peer username
        """
        self._bad_peers[username] = self._bad_peers.get(username, 0) + 1

        if self._bad_peers[username] >= self.config.download.bad_peer_threshold:
            self._blocked_peers.add(username.lower())
            logger.warning(
                f"Peer {username} blocked (failures: {self._bad_peers[username]})"
            )

    def unblock_peer(self, username: str):
        """
        Unblock a peer.

        Args:
            username: Peer username
        """
        self._blocked_peers.discard(username.lower())
        self._bad_peers.pop(username, None)
        logger.info(f"Peer {username} unblocked")
