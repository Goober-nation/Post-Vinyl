"""
SlskdDownload — Concrete implementation of DownloadService using slskd REST API.

Handles downloading files from Soulseek peers via slskd.
"""

import urllib.parse
from datetime import datetime

import requests

from app.config import Config
from app.exceptions import (
    QueueError,
    SlskdConnectionError,
    TransferNotFoundError,
)
from app.logging_config import get_logger
from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    Transfer,
)

logger = get_logger(__name__)


class SlskdDownload(DownloadService):
    """
    slskd-based download implementation.

    Uses the slskd REST API to download files from Soulseek peers.
    """

    def __init__(self, config: Config, search_service=None):
        """
        Initialize SlskdDownload.

        Args:
            config: Config object with slskd settings
            search_service: Optional SearchService — used by
                fetch_search_responses() to serve a query-cache-hit search_id
                (`cache-*`, see app.services.search.SlskdSearch._make_local_job)
                through the same in-process cache SearchService already guards
                those IDs with, instead of asking slskd about an ID it never
                issued (issue #59). A real slskd search_id always goes straight
                to slskd.
        """
        self.config = config
        self.base_url = config.slskd.url
        self.api_key = config.slskd.api_key
        self.session = requests.Session()
        self._search_service = search_service

        # In-memory transfer tracking only.
        self._transfers: dict[str, Transfer] = {}

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
        Fetch peer responses for a search (used by DownloadMonitor's retry).

        Args:
            search_id: The slskd search ID

        Returns:
            List of peer response dicts

        Raises:
            SlskdConnectionError: If the request fails

        A `cache-*` search_id (a query-cache hit — see
        app.services.search.SlskdSearch._make_local_job) is not something
        slskd has ever heard of; asking it directly always fails (#59, root
        cause of #56). Those are served through SearchService instead
        (`_fetch_cached_responses`), which already tracks them in-process.
        """
        if search_id.startswith("cache-"):
            return self._fetch_cached_responses(search_id)

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

    def _fetch_cached_responses(self, search_id: str) -> list[dict]:
        """Serve a query-cache-hit search_id's responses via SearchService,
        converting its per-file SearchResult rows back into the raw,
        grouped-by-peer shape slskd returns for a real search."""
        if self._search_service is None:
            return []
        try:
            results = self._search_service.get_results(search_id)
        except Exception as e:  # noqa: BLE001 — SearchNotFoundError and friends
            logger.warning(f"Could not re-fetch cached responses for {search_id}: {e}")
            return []

        grouped: dict[str, dict] = {}
        for r in results:
            peer = grouped.setdefault(
                r.username,
                {"username": r.username, "hasFreeUploadSlot": False, "files": []},
            )
            if r.has_free_slot:
                peer["hasFreeUploadSlot"] = True
            peer["files"].append({"filename": r.filename, "size": r.size})
        return list(grouped.values())

