from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from app.events.bus import EventBus
from app.events.debounce import Debouncer
from app.events.filters import DedupeFilter, StormLimiter, is_effective
from app.events.models import RawEvent
from app.events.normalize import normalize
from app.ha.registry import RegistryCache
from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EventPipeline:
    """白名单 → 有效性 → 去重 → 防抖 → 风暴限流 → 标准化 → 总线。"""

    def __init__(
        self, config: ConfigService, registry: RegistryCache, bus: EventBus
    ) -> None:
        self._config = config
        self._registry = registry
        self._bus = bus
        self._dedupe = DedupeFilter()
        self._storm = StormLimiter()
        self._debouncer = Debouncer(self._window_for, self._flush)
        self.counters: Counter[str] = Counter()

    async def start(self) -> None:
        self._apply_storm_limits()

    async def stop(self) -> None:
        await self._debouncer.stop()

    async def handle(self, raw: RawEvent) -> None:
        if not self._config.whitelist().entity_allowed(raw.entity_id):
            self.counters["dropped_whitelist"] += 1
            return
        if not is_effective(raw):
            self.counters["dropped_ineffective"] += 1
            return
        if self._dedupe.duplicate(raw.event_id):
            self.counters["dropped_duplicate"] += 1
            return
        self.counters["accepted"] += 1
        await self._debouncer.add(raw)

    def _window_for(self, entity_id: str) -> float:
        domain = entity_id.split(".", 1)[0]
        windows = self._config.global_settings().get("debounce_seconds") or {}
        try:
            return float(windows.get(domain, windows.get("default", 3)))
        except (TypeError, ValueError):
            return 3.0

    def _apply_storm_limits(self) -> None:
        limits = self._config.global_settings().get("storm_limit") or {}
        self._storm.configure(
            per_entity_per_minute=int(limits.get("per_entity_per_minute", 6)),
            per_domain_per_minute=int(limits.get("per_domain_per_minute", 30)),
            storm_interval_seconds=float(limits.get("storm_interval_seconds", 300)),
        )

    async def _flush(self, entity_id: str, raw: RawEvent, dropped: int) -> None:
        now = _utcnow()
        window_id = uuid4().hex[:12]
        self._apply_storm_limits()
        decision = self._storm.admit(entity_id, raw.domain)
        if not decision.allowed:
            if decision.suppressed_count == 0:
                self.counters["dropped_storm"] += 1
                return
            event = normalize(
                raw,
                registry=self._registry,
                now=now,
                event_type="storm",
                window_id=window_id,
                dropped_count=dropped,
                suppressed_count=decision.suppressed_count,
            )
            self.counters["storm_emitted"] += 1
        else:
            event = normalize(
                raw,
                registry=self._registry,
                now=now,
                window_id=window_id,
                dropped_count=dropped,
            )
            self.counters[f"emitted_{event.event_type}"] += 1
        await self._bus.publish(event)
        logger.debug(
            "events.published",
            entity_id=entity_id,
            event_type=event.event_type,
            dropped_count=dropped,
            suppressed_count=event.suppressed_count,
        )

    def stats(self) -> dict[str, int]:
        return dict(self.counters)

    @property
    def pending_count(self) -> int:
        return self._debouncer.pending_count
