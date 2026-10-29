"""In-memory slskd client for tests of code built on the slskd client."""

from __future__ import annotations

from dataclasses import replace

from app.clients.slskd import (
    AppState,
    PeerResponse,
    QueueResult,
    SearchState,
    SlskdClientBase,
    SlskdNotFound,
    Transfer,
)


class FakeSlskdClient(SlskdClientBase):
    """Searches answer with whatever ``results[query]`` holds and complete at once.
    Queued downloads stay "Queued, Remotely" until a test edits ``transfers``."""

    def __init__(self, results: dict[str, list[PeerResponse]] | None = None) -> None:
        self.state = AppState(logged_in=True, has_parent=True)
        self.results = results or {}
        self.searches: dict[str, SearchState] = {}
        self.cancelled: list[str] = []
        self.transfers: dict[str, Transfer] = {}
        self.offline_peers: set[str] = set()
        self.destinations: list[str | None] = []

    def app_state(self) -> AppState:
        return self.state

    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        search_id = f"fake-{len(self.searches) + 1}"
        responses = self.results.get(query, [])[:response_limit]
        self.searches[search_id] = SearchState(
            id=search_id,
            query=query,
            state="Completed, Succeeded",
            is_complete=True,
            response_count=len(responses),
            file_count=sum(len(r.files) for r in responses),
        )
        return search_id

    def _search(self, search_id: str) -> SearchState:
        if search_id not in self.searches:
            raise SlskdNotFound(f"GET /searches/{search_id}: HTTP 404")
        return self.searches[search_id]

    def get_search(self, search_id: str) -> SearchState:
        return self._search(search_id)

    def get_responses(self, search_id: str) -> list[PeerResponse]:
        search = self._search(search_id)
        return self.results.get(search.query, [])[: search.response_count]

    def cancel_search(self, search_id: str) -> None:
        self._search(search_id)
        self.cancelled.append(search_id)

    def queue_download(
        self,
        username: str,
        files: list[tuple[str, int]],
        search_id: str | None = None,
        destination: str | None = None,
    ) -> QueueResult:
        if username in self.offline_peers:
            raise SlskdNotFound(f"User {username} appears to be offline")
        self.destinations.append(destination)
        queued = []
        for filename, size in files:
            transfer_id = f"fake-transfer-{len(self.transfers) + 1}"
            self.transfers[transfer_id] = Transfer(
                id=transfer_id,
                username=username,
                filename=filename,
                state="Queued, Remotely",
                size=size,
                bytes_transferred=0,
                average_speed=0,
                exception=None,
            )
            queued.append(self.transfers[transfer_id])
        return QueueResult(transfers=queued, failures={})

    def list_downloads(self) -> list[Transfer]:
        return list(self.transfers.values())

    def get_download(self, username: str, transfer_id: str) -> Transfer:
        transfer = self.transfers.get(transfer_id)
        if transfer is None or transfer.username != username:
            raise SlskdNotFound(f"GET /transfers/downloads/{transfer_id}: HTTP 404")
        return transfer

    def cancel_download(
        self, username: str, transfer_id: str, forget: bool = False
    ) -> None:
        transfer = self.get_download(username, transfer_id)
        if forget:
            del self.transfers[transfer_id]
        else:
            self.transfers[transfer_id] = replace(
                transfer, state="Completed, Cancelled"
            )
