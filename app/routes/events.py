"""
SSE event-stream endpoint.

Endpoint:
    GET /api/events?types=search,transfer,rec,system  → SSE stream (200)

This endpoint is deliberately ``async def`` (the project uses sync handlers
elsewhere) because it is a long-lived streaming endpoint that awaits on
asyncio queues — not a short-lived blocking service call.

Heartbeats (``: ping`` comments) are sent every HEARTBEAT_INTERVAL seconds
to keep the connection alive.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.logging_config import get_logger
from app.sse import EventHub, EventSubscriber, format_sse

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["events"])

# Categories the frontend may filter on.  Must stay in sync with §6.1.
ALLOWED_TYPES = {"search", "transfer", "rec", "system", "mb"}

# Seconds between heartbeat pings (no real data).  Tests may monkeypatch this.
HEARTBEAT_INTERVAL: float = 15.0


@router.get("/events")
async def events(request: Request, types: str | None = None) -> StreamingResponse:
    """
    Server-Sent Events stream.

    Query params:
        types: Optional comma-separated event categories (search,transfer,rec,system).
               Omit to receive all events.  Unknown categories → 400.

    Becomes an async def that yields SSE-formatted lines until the client
    disconnects or the connection is closed.
    """
    hub: EventHub = request.app.state.event_hub

    sub_types: set[str] | None = None
    if types is not None:
        parsed = {t.strip() for t in types.split(",") if t.strip()}
        invalid = sorted(parsed - ALLOWED_TYPES)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown event type(s): {', '.join(invalid)}. "
                    f"Allowed: {sorted(ALLOWED_TYPES)}"
                ),
            )
        if parsed:
            sub_types = parsed  # empty string after strip → all

    subscriber = hub.subscribe(sub_types)

    return StreamingResponse(
        event_stream(hub, subscriber, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def event_stream(
    hub: EventHub, subscriber: EventSubscriber, request: Request
) -> AsyncGenerator[str, None]:
    """
    Yield SSE-formatted events for *subscriber* until it is closed.

    Emits a ": ping" heartbeat every HEARTBEAT_INTERVAL seconds when idle.
    Exits promptly (without waiting for the next heartbeat) on client
    disconnect or on server shutdown (``hub.shutdown_event``, set by the
    app's lifespan on SIGTERM).

    NOTE on why ``hub.shutdown_event`` alone isn't enough: uvicorn's own
    Server.shutdown() waits for every open connection to finish *before* it
    ever sends the ASGI lifespan "shutdown" event that normally sets
    ``hub.shutdown_event`` — see uvicorn/server.py. A live SSE connection
    never finishes on its own, so without another trigger the two sides
    would deadlock until Docker SIGKILLs the process. `app/main.py` works
    around this by overriding uvicorn's signal handler to call
    ``hub.signal_shutdown()`` directly, the instant SIGTERM/SIGINT arrives —
    so this generator's shutdown check fires promptly, during uvicorn's
    connection-drain wait, and the connection closes itself well before
    `timeout_graceful_shutdown` (kept as a safety net, not the primary
    mechanism) would ever force-cancel it.

    Unsubscribes from *hub* on close.  Extracted from the endpoint so tests
    can drive it directly (in-process HTTP transports buffer the full body
    and cannot stream an infinite SSE response).
    """
    shutdown_task = asyncio.ensure_future(hub.shutdown_event.wait())
    try:
        while True:
            if hub.shutdown_event.is_set() or await request.is_disconnected():
                break

            get_task = asyncio.ensure_future(subscriber.queue.get())
            done, pending = await asyncio.wait(
                {get_task, shutdown_task},
                timeout=HEARTBEAT_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if get_task in done:
                yield format_sse(get_task.result())
            else:
                get_task.cancel()
                if shutdown_task in done:
                    break
                yield ": ping\n\n"
    finally:
        shutdown_task.cancel()
        hub.unsubscribe(subscriber)
