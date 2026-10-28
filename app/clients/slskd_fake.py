"""In-memory slskd client for tests of code built on the slskd client."""

from __future__ import annotations

from app.clients.slskd import (
    AppState,
    PeerResponse,
    SearchState,
    SlskdClientBase,
    SlskdError,
)


class FakeSlskdClient(SlskdClientBase):
    """Searches answer with whatever ``results[query]`` holds and complete at once."""

    def __init__(self, results: dict[str, list[PeerResponse]] | None = None) -> None:
        self.state = AppState(logged_in=True, has_parent=True)
        self.results = results or {}
        self.searches: dict[str, SearchState] = {}
        self.cancelled: list[str] = []

    def app_state(self) -> AppState:
        return self.state

    def start_search(self, query: str, response_limit: int, timeout_s: int) -> str:
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
            raise SlskdError(f"GET /searches/{search_id}: HTTP 404")
        return self.searches[search_id]

    def get_search(self, search_id: str) -> SearchState:
        return self._search(search_id)

    def get_responses(self, search_id: str) -> list[PeerResponse]:
        search = self._search(search_id)
        return self.results.get(search.query, [])[: search.response_count]

    def cancel_search(self, search_id: str) -> None:
        self._search(search_id)
        self.cancelled.append(search_id)
