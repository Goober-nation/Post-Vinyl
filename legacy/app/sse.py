"""
Thread-safe SSE event hub: services/workers publish, SSE clients subscribe.

Publishers call publish() from any thread; subscribers receive events via
asyncio queues. The hub uses call_soon_threadsafe to bridge threads safely.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass

from app.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Event:
    """A single SSE event with a dotted event_type and a data dict."""

    event_type: str
    data: dict


@dataclass
class EventSubscriber:
    """An active SSE subscriber: owns an asyncio queue and a type filter."""

    queue: asyncio.Queue[Event]
    types: set[str] | None  # None = receive all categories
    loop: asyncio.AbstractEventLoop


class EventHub:
    """
    Thread-safe publish/subscribe hub for SSE events.

    Services/workers call publish() from any thread.
    SSE endpoints call subscribe()/unsubscribe() from the event loop.
    """

    def __init__(self, max_queue_size: int = 100) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._lock = threading.Lock()
        self._max_queue_size = max_queue_size
        self.shutdown_event: asyncio.Event = asyncio.Event()

    def signal_shutdown(self) -> None:
        """Signal all active/future SSE streams to exit promptly.

        Must be called from the event loop the streams are running on
        (the FastAPI lifespan shutdown phase runs there).
        """
        self.shutdown_event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, types: set[str] | None = None) -> EventSubscriber:
        """
        Create a new subscriber.

        Must be called from an async context (captures the running event loop).
        *types* limits delivery to matching event categories (first segment).
        None means deliver all events.
        """
        sub = EventSubscriber(
            queue=asyncio.Queue(maxsize=self._max_queue_size),
            types=types,
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            self._subscribers.append(sub)
        logger.debug(f"SSE subscriber added (total: {len(self._subscribers)})")
        return sub

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """Remove a subscriber. Safe to call when already removed (no-op)."""
        with self._lock:
            self._subscribers = [
                s for s in self._subscribers if id(s) != id(subscriber)
            ]
        logger.debug(f"SSE subscriber removed (total: {len(self._subscribers)})")

    def publish(self, event_type: str, data: dict | None = None) -> None:
        """
        Publish an event to all matching subscribers.  SAFE FROM ANY THREAD.

        The category (first segment of *event_type*) is compared against each
        subscriber's type filter.  Events are enqueued synchronously when the
        publisher is already on the subscriber's event loop, otherwise via
        call_soon_threadsafe() so the publisher does not need to be on the
        event loop.
        """
        category = event_type.split(".", 1)[0]
        data = data or {}
        event = Event(event_type=event_type, data=data)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        with self._lock:
            subscribers_snapshot = list(self._subscribers)

        for sub in subscribers_snapshot:
            if sub.types is None or category in sub.types:
                if running_loop is sub.loop:
                    self._enqueue(sub.queue, event)
                else:
                    sub.loop.call_soon_threadsafe(self._enqueue, sub.queue, event)

    def subscriber_count(self) -> int:
        """Return the current number of active subscribers."""
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enqueue(self, queue: asyncio.Queue[Event], event: Event) -> None:
        """Enqueue an event; drop with a warning if the queue is full."""
        if queue.full():
            logger.warning(f"SSE event dropped (queue full): {event.event_type}")
        else:
            queue.put_nowait(event)


def format_sse(event: Event) -> str:
    """Convert an Event to the SSE wire format."""
    return f"event: {event.event_type}\ndata: {json.dumps(event.data, default=str)}\n\n"
