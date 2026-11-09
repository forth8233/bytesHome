from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from typing import Any, Deque


class SSEHub:
    """进程内 SSE 扇出：短期票据、环形缓冲补发、单成员连接上限。"""

    def __init__(
        self,
        *,
        ring_size: int = 500,
        ticket_ttl: float = 60.0,
        max_connections: int = 3,
        heartbeat: float = 15.0,
    ) -> None:
        self._ring: Deque[tuple[int, str, str]] = deque(maxlen=ring_size)
        self._tickets: dict[str, tuple[int, float]] = {}
        self._subscribers: dict[int, list[asyncio.Queue[tuple[int, str, str] | None]]] = {}
        self._seq = 0
        self._ticket_ttl = ticket_ttl
        self._max_connections = max_connections
        self.heartbeat = heartbeat
        self._lock = asyncio.Lock()

    def issue_ticket(self, member_id: int) -> str:
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = (member_id, time.monotonic() + self._ticket_ttl)
        self._prune_tickets()
        return ticket

    def consume_ticket(self, ticket: str) -> int | None:
        self._prune_tickets()
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        member_id, expires_at = entry
        if expires_at < time.monotonic():
            return None
        return member_id

    def _prune_tickets(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_, deadline) in self._tickets.items() if deadline < now]
        for key in expired:
            self._tickets.pop(key, None)

    async def publish(self, event_type: str, data: dict[str, Any]) -> int:
        self._seq += 1
        payload = json.dumps(data, ensure_ascii=False)
        entry = (self._seq, event_type, payload)
        async with self._lock:
            self._ring.append(entry)
            for queues in self._subscribers.values():
                for queue in queues:
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    queue.put_nowait(entry)
        return self._seq

    def since(self, last_id: int) -> list[tuple[int, str, str]]:
        if last_id <= 0:
            return []
        return [entry for entry in self._ring if entry[0] > last_id]

    def open(self, member_id: int) -> asyncio.Queue[tuple[int, str, str] | None]:
        queue: asyncio.Queue[tuple[int, str, str] | None] = asyncio.Queue(maxsize=200)
        queues = self._subscribers.setdefault(member_id, [])
        while len(queues) >= self._max_connections:
            oldest = queues.pop(0)
            try:
                oldest.put_nowait(None)
            except asyncio.QueueFull:
                pass
        queues.append(queue)
        return queue

    def close(self, member_id: int, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(member_id)
        if not queues:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(member_id, None)

    @property
    def last_id(self) -> int:
        return self._seq

    @property
    def buffer_size(self) -> int:
        return len(self._ring)
