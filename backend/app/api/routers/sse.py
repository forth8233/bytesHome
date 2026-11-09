from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser

router = APIRouter(prefix="/api/sse", tags=["sse"])


def _hub(request: Request):
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        raise HTTPException(503, "sse is not available")
    return hub


@router.post("/ticket")
async def create_ticket(user: CurrentUser, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    return {"ticket": hub.issue_ticket(user.id), "expires_in": 60}


@router.get("/notify")
async def notify_stream(request: Request, ticket: str) -> StreamingResponse:
    hub = _hub(request)
    member_id = hub.consume_ticket(ticket)
    if member_id is None:
        raise HTTPException(401, "invalid or expired ticket")

    last_raw = request.headers.get("last-event-id") or "0"
    try:
        last_id = int(last_raw)
    except ValueError:
        last_id = 0

    async def event_stream():
        queue = hub.open(member_id)
        try:
            for entry in hub.since(last_id):
                yield _format(entry)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=hub.heartbeat)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                yield _format(item)
        finally:
            hub.close(member_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format(entry: tuple[int, str, str]) -> str:
    event_id, event_type, data = entry
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"
