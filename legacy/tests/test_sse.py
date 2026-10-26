"""
Unit tests for app.sse.EventHub.

All tests run synchronously (no pytest-asyncio) and create an asyncio
event loop as needed via asyncio.run() or asyncio.new_event_loop().
"""

from __future__ import annotations

import asyncio
import json
import threading

from app.sse import Event, EventHub, format_sse

# ============================================================================
# EventHub — basic publish / subscribe
# ============================================================================


def test_publish_before_subscriber_is_noop() -> None:
    """publish() with zero subscribers does nothing and does not crash."""
    hub = EventHub()
    hub.publish("search.started", {"search_id": "s1"})
    assert hub.subscriber_count() == 0


def test_subscribe_none_receives_all_categories() -> None:
    """A subscriber with types=None receives every category."""

    async def _run() -> None:
        hub = EventHub()
        sub = hub.subscribe(None)
        hub.publish("search.started", {"search_id": "s1"})
        hub.publish("transfer.started", {"id": "t1"})
        hub.publish("system.config_reloaded", {"changed_keys": ["k"]})

        events: list[Event] = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        assert len(events) == 3
        assert {e.event_type for e in events} == {
            "search.started",
            "transfer.started",
            "system.config_reloaded",
        }

    asyncio.run(_run())


def test_subscribe_with_types_filters_by_category() -> None:
    """A subscriber with types={'search'} receives search.* but NOT transfer.*/system.*."""

    async def _run() -> None:
        hub = EventHub()
        sub = hub.subscribe({"search"})
        hub.publish("search.started", {"search_id": "s1"})
        hub.publish("transfer.started", {"id": "t1"})
        hub.publish("search.completed", {"search_id": "s1"})
        hub.publish("system.error", {"code": "E"})

        events: list[Event] = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        assert len(events) == 2
        for e in events:
            assert e.event_type.startswith("search.")

    asyncio.run(_run())


# ============================================================================
# Thread safety
# ============================================================================


def test_publish_from_other_thread_delivers_event() -> None:
    """A publish() from a non-eventloop thread reaches a subscriber."""

    async def _run() -> None:
        hub = EventHub()
        sub = hub.subscribe({"search"})

        def _publish() -> None:
            hub.publish("search.started", {"search_id": "s1"})
            hub.publish("search.completed", {"search_id": "s1"})

        t = threading.Thread(target=_publish)
        t.start()
        t.join()

        # Allow the call_soon_threadsafe callbacks to execute
        await asyncio.sleep(0)

        events: list[Event] = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        assert len(events) == 2
        assert events[0].event_type == "search.started"
        assert events[1].event_type == "search.completed"

    asyncio.run(_run())


# ============================================================================
# Unsubscribe
# ============================================================================


def test_unsubscribe_stops_delivery() -> None:
    """After unsubscribe, the subscriber receives no more events."""

    async def _run() -> None:
        hub = EventHub()
        sub = hub.subscribe(None)
        assert hub.subscriber_count() == 1

        hub.publish("search.started", {})
        hub.unsubscribe(sub)
        assert hub.subscriber_count() == 0

        hub.publish("search.completed", {})

        # Only the first event should be in the queue
        events: list[Event] = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        assert len(events) == 1

    asyncio.run(_run())


def test_unsubscribe_nonexistent_is_noop() -> None:
    """Unsubscribing a subscriber that was already removed does not crash."""

    async def _run() -> None:
        hub = EventHub()
        sub = hub.subscribe(None)
        hub.unsubscribe(sub)
        assert hub.subscriber_count() == 0
        hub.unsubscribe(sub)  # should not raise
        assert hub.subscriber_count() == 0

    asyncio.run(_run())


# ============================================================================
# Queue overflow
# ============================================================================


def test_queue_overflow_drops_events_without_crash() -> None:
    """When a subscriber's queue is full, further events are dropped (no crash)."""

    async def _run() -> None:
        hub = EventHub(max_queue_size=2)
        sub = hub.subscribe(None)

        hub.publish("search.started", {"search_id": "s1"})
        hub.publish("search.progress", {"search_id": "s1"})
        hub.publish("search.completed", {"search_id": "s1"})

        events: list[Event] = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        # First 2 should be delivered; 3rd dropped
        assert len(events) == 2
        assert hub.subscriber_count() == 1

    asyncio.run(_run())


# ============================================================================
# Event / format_sse
# ============================================================================


def test_format_sse_wire_format() -> None:
    """format_sse() produces exact SSE wire format."""
    event = Event(
        event_type="search.started",
        data={"search_id": "abc123", "query": "test", "count": 42},
    )
    output = format_sse(event)

    lines = output.split("\n")
    assert lines[0] == "event: search.started"
    assert lines[1].startswith("data: ")
    assert lines[2] == ""

    data_str = lines[1][len("data: ") :]
    parsed = json.loads(data_str)
    assert parsed == {"search_id": "abc123", "query": "test", "count": 42}


def test_format_sse_serializes_datetime_via_default_str() -> None:
    """format_sse() uses default=str so datetimes don't crash JSON."""
    from datetime import datetime, timezone

    event = Event(
        event_type="test.event",
        data={"ts": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    )
    output = format_sse(event)
    # Should not raise TypeError
    assert "event: test.event" in output
    assert "2026" in output
