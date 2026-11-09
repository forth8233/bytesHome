from __future__ import annotations

import asyncio

import structlog

from app.events.models import ProcessedEvent

logger = structlog.get_logger(__name__)


class EventBus:
    """进程内扇出。投递永不阻塞生产者，订阅者队列满则丢弃最旧事件。"""

    def __init__(self, maxsize: int = 1000, warn_interval: float = 60.0) -> None:
        self._maxsize = maxsize
        self._warn_interval = warn_interval
        self._subscribers: dict[str, asyncio.Queue[ProcessedEvent]] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.dropped_total = 0
        self._dropped_since_warn = 0

    def subscribe(self, name: str) -> asyncio.Queue[ProcessedEvent]:
        queue: asyncio.Queue[ProcessedEvent] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[name] = queue
        return queue

    async def publish(self, event: ProcessedEvent) -> None:
        for queue in self._subscribers.values():
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_total += 1
                self._dropped_since_warn += 1
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 单线程下不应发生
                self.dropped_total += 1
                self._dropped_since_warn += 1

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._report_drops(), name="event-bus-metrics")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _report_drops(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._warn_interval)
            except asyncio.TimeoutError:
                pass
            if self._dropped_since_warn:
                logger.warning(
                    "bus.dropped",
                    dropped_since_last=self._dropped_since_warn,
                    dropped_total=self.dropped_total,
                )
                self._dropped_since_warn = 0
