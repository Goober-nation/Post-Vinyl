"""
Integration tests for SSE event-stream routes (P4-5).

In-process HTTP transports (starlette TestClient, httpx ASGITransport) buffer
the FULL response body and wait for the app to complete, so an infinite SSE
stream cannot be exercised over HTTP in tests.  Instead:

- the endpoint's event_stream() generator is driven directly (wire format,
  heartbeat, type filtering, disconnect cleanup)
- route → hub emission is verified with finite POST requests through
  httpx.AsyncClient(ASGITransport) against a pre-subscribed subscriber
- the real HTTP streaming path is covered by live-testing (curl) against uvicorn

No pytest-asyncio — all tests are synchronous wrappers around asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.routes.events import event_stream
from app.services.interfaces.search import SearchJob, SearchResult, SearchService

# ============================================================================
# Helpers
# ============================================================================


async def _next_chunk(agen, timeout: float = 2.0) -> str:
    """Read the next chunk from an async generator with a timeout."""
    return await asyncio.wait_for(agen.__anext__(), timeout)


# ============================================================================
# Fake services
# ============================================================================


class FakeSearchService(SearchService):
    """In-memory SearchService — mirrors tests/test_routes/test_search_routes.py."""

    def __init__(self) -> None:
        self.searches: dict = {}
        self.next_id = 1

    def search(self, query: str, artist: str | None = None) -> SearchJob:
        search_id = f"search-{self.next_id}"
        self.next_id += 1
        job = SearchJob(
            search_id=search_id,
            query=query,
            artist=artist,
            created_at=datetime.now(timezone.utc),
            status="searching",
        )
        self.searches[search_id] = job
        return job

    def get_results(self, search_id: str) -> list[SearchResult]:
        from app.exceptions import SearchNotFoundError

        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        return [
            SearchResult(
                username="peer1",
                filename="song.mp3",
                size=5242880,
                has_free_slot=True,
                upload_speed=102400,
                bitrate="320kbps",
                duration=240,
            )
        ]

    def cancel(self, search_id: str) -> bool:
        from app.exceptions import SearchNotFoundError

        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        self.searches[search_id].status = "cancelled"
        return True

    def get_status(self, search_id: str) -> SearchJob:
        from app.exceptions import SearchNotFoundError

        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        return self.searches[search_id]

    def list_searches(self) -> list[SearchJob]:
        return sorted(
            self.searches.values(), key=lambda job: job.created_at, reverse=True
        )

    def get_progress(self, search_id: str) -> dict:
        from app.exceptions import SearchNotFoundError

        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        return {
            "response_count": 0,
            "file_count": 0,
            "is_complete": False,
            "elapsed_seconds": 0.0,
            "threshold": 10,
            "max_wait_seconds": 10,
        }


def _write_config(tmp_path: Path) -> None:
    """Write a minimal config.toml to *tmp_path* (mirrors test_config_routes.py)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """\
[server]
port = 8000
host = "0.0.0.0"

[paths]
data_dir = "/app/data"
music_dir = "/music"
download_dir = "/music/downloads"
searches_dir = "/music/searches"
discovery_familiar_dir = "Discovery/Comfort_Zone"
discovery_new_releases_dir = "Discovery/Fresh_Picks"
discovery_exploration_dir = "Discovery/Deep_Cuts"

[navidrome]
url = "http://navidrome-server:4533"

[slskd]
url = "http://slskd:5030"

[listenbrainz]
enabled = false
url = "https://api.listenbrainz.org"

[search]
wait_seconds = 10
poll_interval = 1
response_threshold = 10
min_wait_seconds = 3

[download]
check_interval = 15
max_retries_per_track = 3
bad_peer_threshold = 1
upload_limit_mb = 50

[recs]
comfort_zone_enabled = false
fresh_picks_enabled = false
deep_cuts_enabled = false
comfort_zone_interval_days = 1
deep_cuts_interval_days = 7
comfort_zone_playlist_name = "Comfort Zone"
fresh_picks_playlist_name = "Fresh Picks"
deep_cuts_playlist_name = "Deep Cuts"
comfort_zone_count = 5
deep_cuts_count = 5

[sync]
interval_hours = 12

[logging]
level = "INFO"
format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
"""
    )
    env_path = tmp_path / ".env"
    env_path.write_text('SLSKD_API_KEY="real-key"\nNAVIDROME_PASSWORD="real-pass"\n')


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def client() -> TestClient:
    """Basic app with a FakeSearchService (no config file needed)."""
    app = create_app(search_service=FakeSearchService())
    return TestClient(app)


@pytest.fixture
def app_with_config(tmp_path: Path):
    """App with real Config backed by temp files (for config emission tests)."""
    _write_config(tmp_path)
    cfg = Config(
        config_path=str(tmp_path / "config.toml"),
        env_path=str(tmp_path / ".env"),
    )
    cfg.load()
    return create_app(config=cfg)


# ============================================================================
# Validation
# ============================================================================


class TestInvalidType:
    def test_unknown_type_returns_400(self, client) -> None:
        """Unknown event type → 400 with error format."""
        resp = client.get("/api/events?types=bogus")

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "HTTP_400"
        assert "bogus" in body["error"]["message"]


# ============================================================================
# event_stream generator — wire format, filtering, heartbeat, cleanup
# ============================================================================


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — never reports disconnect."""

    async def is_disconnected(self) -> bool:
        return False


class TestEventStream:
    def test_receives_published_event(self) -> None:
        """A published event is delivered in SSE wire format."""

        async def _run() -> None:
            from app.sse import EventHub

            hub = EventHub()
            sub = hub.subscribe({"search"})
            agen = event_stream(hub, sub, _FakeRequest())

            hub.publish("search.started", {"search_id": "s1", "query": "test"})
            chunk = await _next_chunk(agen)

            lines = chunk.split("\n")
            assert lines[0] == "event: search.started"
            assert lines[1].startswith("data: ")
            data = json.loads(lines[1][len("data: ") :])
            assert data == {"search_id": "s1", "query": "test"}
            assert lines[2] == ""

            await agen.aclose()

        asyncio.run(_run())

    def test_type_filtering_excludes_wrong_categories(self) -> None:
        """Subscriber with types={'search'} gets search.* but NOT transfer.*."""
        from app.sse import EventHub

        async def _run() -> None:
            hub = EventHub()
            sub = hub.subscribe({"search"})
            agen = event_stream(hub, sub, _FakeRequest())

            hub.publish("transfer.started", {"transfer_id": "t1"})
            await asyncio.sleep(0.2)  # would arrive first if filtering failed
            hub.publish("search.completed", {"search_id": "s1"})

            chunk = await _next_chunk(agen)
            assert chunk.startswith("event: search.completed")

            await agen.aclose()

        asyncio.run(_run())

    def test_heartbeat_ping_on_idle(self, monkeypatch) -> None:
        """When idle longer than HEARTBEAT_INTERVAL, a ': ping' is sent."""
        monkeypatch.setattr("app.routes.events.HEARTBEAT_INTERVAL", 0.05)

        from app.sse import EventHub

        async def _run() -> None:
            hub = EventHub()
            sub = hub.subscribe({"search"})
            agen = event_stream(hub, sub, _FakeRequest())

            chunk = await _next_chunk(agen, timeout=2.0)
            assert chunk == ": ping\n\n"

            await agen.aclose()

        asyncio.run(_run())

    def test_close_unsubscribes(self) -> None:
        """Cancelling the stream (client disconnect) removes the subscriber."""
        from contextlib import suppress

        from app.sse import EventHub

        async def _run() -> None:
            hub = EventHub()
            sub = hub.subscribe({"search"})
            assert hub.subscriber_count() == 1

            agen = event_stream(hub, sub, _FakeRequest())
            # Drive the generator to its suspension point, then cancel it —
            # the same way starlette tears down a disconnected SSE stream.
            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

            assert hub.subscriber_count() == 0

        asyncio.run(_run())

    def test_shutdown_signal_ends_stream_promptly(self) -> None:
        """Setting hub.shutdown_event exits the stream without waiting for
        a heartbeat — this is what lets docker stop/SIGTERM return promptly
        even with a browser tab still connected."""
        from app.sse import EventHub

        async def _run() -> None:
            hub = EventHub(max_queue_size=10)
            hub_holder = hub
            sub = hub.subscribe({"search"})
            agen = event_stream(hub, sub, _FakeRequest())

            task = asyncio.ensure_future(agen.__anext__())
            await asyncio.sleep(0)
            hub_holder.signal_shutdown()

            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(task, timeout=1.0)

            assert hub.subscriber_count() == 0

        asyncio.run(_run())


# ============================================================================
# Route-driven emission (finite requests through ASGITransport)
# ============================================================================


class TestRouteEmission:
    def test_search_route_emits_search_started(self) -> None:
        """POST /api/search publishes search.started to subscribers."""
        from app.sse import EventHub

        async def _run() -> None:
            app = create_app(search_service=FakeSearchService())
            hub: EventHub = app.state.event_hub
            sub = hub.subscribe({"search"})

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/api/search", json={"query": "test"})
                assert resp.status_code == 201

                event = await asyncio.wait_for(sub.queue.get(), 2.0)
                assert event.event_type == "search.started"
                assert event.data["query"] == "test"
                assert "search_id" in event.data

            hub.unsubscribe(sub)

        asyncio.run(_run())

    def test_cancel_route_emits_search_cancelled(self) -> None:
        """POST /api/searches/{id}/cancel publishes search.cancelled."""
        from app.sse import EventHub

        async def _run() -> None:
            app = create_app(search_service=FakeSearchService())
            hub: EventHub = app.state.event_hub
            sub = hub.subscribe({"search"})

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                created = (
                    await client.post("/api/search", json={"query": "test"})
                ).json()
                await client.post(f"/api/searches/{created['search_id']}/cancel")

                # First event is search.started from the create call
                first = await asyncio.wait_for(sub.queue.get(), 2.0)
                assert first.event_type == "search.started"
                second = await asyncio.wait_for(sub.queue.get(), 2.0)
                assert second.event_type == "search.cancelled"
                assert second.data["cancelled"] is True

            hub.unsubscribe(sub)

        asyncio.run(_run())

    def test_config_route_emits_config_reloaded(self, app_with_config) -> None:
        """POST /api/config publishes system.config_reloaded with changed_keys."""
        from app.sse import EventHub

        async def _run() -> None:
            hub: EventHub = app_with_config.state.event_hub
            sub = hub.subscribe({"system"})

            transport = httpx.ASGITransport(app=app_with_config)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/config", json={"search": {"wait_seconds": 5}}
                )
                assert resp.status_code == 200

                event = await asyncio.wait_for(sub.queue.get(), 2.0)
                assert event.event_type == "system.config_reloaded"
                assert "search.wait_seconds" in event.data["changed_keys"]

            hub.unsubscribe(sub)

        asyncio.run(_run())
