from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EventType = Literal["state_changed", "attribute_changed", "recovered", "storm"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class RawEvent:
    """适配器直接交给流水线的原始 state_changed 事件。"""

    event_id: str
    entity_id: str
    old_state: dict[str, Any] | None
    new_state: dict[str, Any] | None
    occurred_at: datetime | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class ProcessedEvent:
    event_id: str
    entity_id: str
    entity_name: str
    domain: str
    event_type: EventType
    old_state: str | None
    new_state: str | None
    attributes_delta: dict[str, Any] = field(default_factory=dict)
    area_id: str | None = None
    device_id: str | None = None
    occurred_at: datetime | None = None
    processed_at: datetime | None = None
    risk_pre: RiskLevel = "low"
    dedupe_key: str = ""
    window_id: str | None = None
    dropped_count: int = 0
    suppressed_count: int = 0
