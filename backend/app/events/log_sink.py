from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ProcessedEventLog
from app.events.models import ProcessedEvent

logger = structlog.get_logger(__name__)

_STATE_MAX = 64


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:_STATE_MAX]


class EventLogSink:
    """总线订阅者：把标准化事件批量落库，绝不阻塞生产者。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: asyncio.Queue[ProcessedEvent],
        batch_size: int = 200,
        flush_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.persisted = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="event-log-sink")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush_once()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_interval)
            except asyncio.TimeoutError:
                pass
            await self._flush_once()

    async def _flush_once(self) -> None:
        batch: list[ProcessedEvent] = []
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        try:
            async with self._session_factory() as session:
                session.add_all(
                    [
                        ProcessedEventLog(
                            event_id=event.event_id[:128],
                            entity_id=event.entity_id[:128],
                            event_type=event.event_type,
                            old_state=_clip(event.old_state),
                            new_state=_clip(event.new_state),
                            risk_pre=event.risk_pre,
                            window_id=event.window_id,
                            dropped_count=event.dropped_count,
                            occurred_at=event.occurred_at,
                        )
                        for event in batch
                    ]
                )
                await session.commit()
            self.persisted += len(batch)
        except Exception:  # noqa: BLE001 - 落库失败不能拖垮事件链路
            logger.exception("events.log_sink.persist_failed", count=len(batch))
