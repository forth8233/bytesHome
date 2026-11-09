from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from app.events.models import RawEvent

IGNORED_ATTRIBUTES = frozenset({"last_changed", "last_updated", "friendly_name"})


def attribute_delta(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_attrs = old.get("attributes") or {}
    new_attrs = new.get("attributes") or {}
    delta: dict[str, Any] = {}
    for key in set(old_attrs) | set(new_attrs):
        if key in IGNORED_ATTRIBUTES:
            continue
        if old_attrs.get(key) != new_attrs.get(key):
            delta[key] = new_attrs.get(key)
    return delta


def is_effective(raw: RawEvent) -> bool:
    """state 未变且属性变化只落在忽略集合内的事件没有信息量。"""
    old, new = raw.old_state, raw.new_state
    if old is None or new is None:
        return True
    if old.get("state") != new.get("state"):
        return True
    return bool(attribute_delta(old, new))


class DedupeFilter:
    """按事件 id 去重，抵御 WS 重连重放与重复投递。"""

    def __init__(self, ttl_seconds: float = 600.0, maxsize: int = 5000) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._seen: OrderedDict[str, float] = OrderedDict()

    def duplicate(self, key: str, now: float | None = None) -> bool:
        if not key:
            return False
        now = time.monotonic() if now is None else now
        self._evict(now)
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = now
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        deadline = now - self._ttl
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if stamp >= deadline:
                break
            self._seen.popitem(last=False)


@dataclass(frozen=True, slots=True)
class StormDecision:
    allowed: bool
    suppressed_count: int = 0


class StormLimiter:
    def __init__(
        self,
        per_entity_per_minute: int = 6,
        per_domain_per_minute: int = 30,
        window_seconds: float = 60.0,
        storm_interval_seconds: float = 300.0,
    ) -> None:
        self._per_entity = per_entity_per_minute
        self._per_domain = per_domain_per_minute
        self._window = window_seconds
        self._storm_interval = storm_interval_seconds
        self._entity_hits: dict[str, deque[float]] = {}
        self._domain_hits: dict[str, deque[float]] = {}
        self._suppressed: dict[str, int] = {}
        self._last_storm: dict[str, float] = {}

    def configure(
        self,
        per_entity_per_minute: int,
        per_domain_per_minute: int,
        storm_interval_seconds: float,
    ) -> None:
        self._per_entity = per_entity_per_minute
        self._per_domain = per_domain_per_minute
        self._storm_interval = storm_interval_seconds

    def admit(self, entity_id: str, domain: str, now: float | None = None) -> StormDecision:
        now = time.monotonic() if now is None else now
        entity_hits = self._prune(self._entity_hits, entity_id, now)
        domain_hits = self._prune(self._domain_hits, domain, now)
        if len(entity_hits) < self._per_entity and len(domain_hits) < self._per_domain:
            entity_hits.append(now)
            domain_hits.append(now)
            return StormDecision(True)

        self._suppressed[entity_id] = self._suppressed.get(entity_id, 0) + 1
        last = self._last_storm.get(entity_id)
        if last is not None and now - last < self._storm_interval:
            return StormDecision(False)
        count = self._suppressed.pop(entity_id)
        self._last_storm[entity_id] = now
        return StormDecision(False, suppressed_count=count)

    def _prune(self, table: dict[str, deque[float]], key: str, now: float) -> deque[float]:
        hits = table.get(key)
        if hits is None:
            hits = deque()
            table[key] = hits
        deadline = now - self._window
        while hits and hits[0] < deadline:
            hits.popleft()
        return hits
