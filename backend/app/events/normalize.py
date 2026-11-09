from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.events.filters import attribute_delta
from app.events.models import EventType, ProcessedEvent, RawEvent
from app.events.risk import pre_risk
from app.ha.registry import RegistryCache

_UNKNOWN_STATES = frozenset({"unavailable", "unknown"})


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _state_of(state: dict[str, Any] | None) -> str | None:
    return None if state is None else str(state.get("state"))


def classify(raw: RawEvent, delta: dict[str, Any]) -> EventType:
    old, new = raw.old_state, raw.new_state
    if old is None or new is None:
        return "state_changed"
    old_state, new_state = _state_of(old), _state_of(new)
    if old_state in _UNKNOWN_STATES and new_state not in _UNKNOWN_STATES:
        return "recovered"
    if old_state == new_state:
        return "attribute_changed" if delta else "state_changed"
    return "state_changed"


def dedupe_key(entity_id: str, event_type: str, old_state: str | None, new_state: str | None,
               delta: dict[str, Any]) -> str:
    signature = "|".join(
        [entity_id, event_type, str(old_state), str(new_state), ",".join(sorted(delta))]
    )
    return hashlib.sha1(signature.encode("utf-8")).hexdigest()


def normalize(
    raw: RawEvent,
    *,
    registry: RegistryCache,
    now: datetime,
    event_type: EventType | None = None,
    window_id: str | None = None,
    dropped_count: int = 0,
    suppressed_count: int = 0,
) -> ProcessedEvent:
    delta = (
        attribute_delta(raw.old_state, raw.new_state)
        if raw.old_state is not None and raw.new_state is not None
        else {}
    )
    kind: EventType = event_type or classify(raw, delta)
    new_state = raw.new_state
    attributes = (new_state or {}).get("attributes") or {}
    entry = registry.entities.get(raw.entity_id) or {}
    entity_name = (
        entry.get("name")
        or entry.get("original_name")
        or attributes.get("friendly_name")
        or raw.entity_id
    )
    old_value = _state_of(raw.old_state)
    new_value = _state_of(new_state)
    return ProcessedEvent(
        event_id=raw.event_id,
        entity_id=raw.entity_id,
        entity_name=str(entity_name),
        domain=raw.domain,
        event_type=kind,
        old_state=old_value,
        new_state=new_value,
        attributes_delta=delta,
        area_id=registry.area_of(raw.entity_id),
        device_id=registry.device_of(raw.entity_id),
        occurred_at=raw.occurred_at or _parse_ts((new_state or {}).get("last_changed")) or now,
        processed_at=now,
        risk_pre=pre_risk(raw.domain, attributes, new_value),
        dedupe_key=dedupe_key(raw.entity_id, kind, old_value, new_value, delta),
        window_id=window_id,
        dropped_count=dropped_count,
        suppressed_count=suppressed_count,
    )
