from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.events.models import RawEvent

FlushHandler = Callable[[str, RawEvent, int], Awaitable[None]]


@dataclass(slots=True)
class _Pending:
    raw: RawEvent
    dropped: int = 0


class Debouncer:
    """按实体滑动窗口合并：窗口内只保留最新一条，到期投递并带上合并数。"""

    def __init__(self, window_for: Callable[[str], float], on_flush: FlushHandler) -> None:
        self._window_for = window_for
        self._on_flush = on_flush
        self._pending: dict[str, _Pending] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, raw: RawEvent) -> None:
        entity_id = raw.entity_id
        pending = self._pending.get(entity_id)
        if pending is not None:
            pending.raw = raw
            pending.dropped += 1
            return
        self._pending[entity_id] = _Pending(raw=raw)
        self._timers[entity_id] = asyncio.create_task(
            self._flush_after(entity_id, self._window_for(entity_id)),
            name=f"debounce:{entity_id}",
        )

    async def _flush_after(self, entity_id: str, window: float) -> None:
        await asyncio.sleep(window)
        self._timers.pop(entity_id, None)
        pending = self._pending.pop(entity_id, None)
        if pending is None:
            return
        await self._on_flush(entity_id, pending.raw, pending.dropped)

    async def stop(self) -> None:
        tasks = list(self._timers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timers.clear()
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)
