from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.agent.prompt import build_messages
from app.db.models import MemberPreference, Proposal, UserFeedback
from app.events.models import ProcessedEvent
from app.ha.registry import RegistryCache
from app.safety.guard import action_signature

ATTR_WHITELIST: dict[str, tuple[str, ...]] = {
    "climate": ("current_temperature", "temperature", "hvac_mode", "fan_mode", "hvac_action"),
    "light": ("brightness", "color_temp"),
    "cover": ("current_position",),
    "sensor": ("unit_of_measurement",),
    "binary_sensor": ("device_class",),
    "switch": (),
    "fan": (),
    "humidifier": (),
    "media_player": ("source", "volume_level"),
}

SECTION_BUDGET = {
    "event": 200,
    "device_context": 1200,
    "history_feedback": 500,
    "preferences": 300,
}


def estimate_tokens(text: str) -> int:
    # 中英混排的保守估算：约 2 个字符 1 token
    return max(1, (len(text) + 1) // 2)


def _u(value: object) -> str | None:
    if value is None:
        return None
    return f"<untrusted>{value}</untrusted>"


@dataclass(slots=True)
class ContextBundle:
    event: dict
    entities: list[dict] = field(default_factory=list)
    feedback: list[dict] = field(default_factory=list)
    preferences: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)
    input_summary: str = ""
    dropped: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContextBuilder:
    def __init__(
        self,
        *,
        session_factory,
        cache,
        registry: RegistryCache,
        max_feedback: int = 3,
        max_preferences: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._cache = cache
        self._registry = registry
        self._max_feedback = max_feedback
        self._max_preferences = max_preferences

    async def build(
        self, event: ProcessedEvent, *, member_id: int | None = None
    ) -> ContextBundle:
        snapshot = await self._cache.snapshot()
        event_record = self._event_record(event)
        candidates = self._candidates(event, snapshot)
        entities, dropped = self._fit(candidates, SECTION_BUDGET["device_context"])

        feedback: list[dict] = []
        preferences: dict[str, str] = {}
        if self._session_factory is not None:
            async with self._session_factory() as session:
                feedback = await self._feedback(session, event)
                if member_id is not None:
                    preferences = await self._preferences(session, member_id, event.domain)
        feedback, _ = self._fit_items(feedback, SECTION_BUDGET["history_feedback"])
        preferences, _ = self._fit_items(preferences, SECTION_BUDGET["preferences"])

        bundle = ContextBundle(
            event=event_record,
            entities=entities,
            feedback=feedback,
            preferences=preferences,
            dropped=dropped,
        )
        bundle.messages = build_messages(bundle)
        bundle.input_summary = json.dumps(
            {
                "entity_id": event.entity_id,
                "event_type": event.event_type,
                "risk_pre": event.risk_pre,
                "entities": len(entities),
                "feedback": len(feedback),
                "preferences": len(preferences),
            },
            ensure_ascii=False,
        )
        return bundle

    def _event_record(self, event: ProcessedEvent) -> dict:
        return {
            "entity_id": event.entity_id,
            "name": _u(event.entity_name),
            "domain": event.domain,
            "event_type": event.event_type,
            "old_state": _u(event.old_state),
            "new_state": _u(event.new_state),
            "attributes_delta": {
                key: _u(value) for key, value in (event.attributes_delta or {}).items()
            },
            "area_id": event.area_id,
            "device_id": event.device_id,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "risk_pre": event.risk_pre,
            "dropped_count": event.dropped_count,
            "suppressed_count": event.suppressed_count,
        }

    def _candidates(
        self, event: ProcessedEvent, snapshot: dict[str, dict]
    ) -> list[tuple[int, str, dict]]:
        """返回 (权重, 关系, 记录)，按权重降序；同一实体只保留最高权重的一条。"""
        area_id = event.area_id or self._registry.area_of(event.entity_id)
        device_id = event.device_id or self._registry.device_of(event.entity_id)
        grouped: dict[str, tuple[int, str, dict]] = {}

        def add(weight: int, relation: str, entity_id: str) -> None:
            state = snapshot.get(entity_id)
            if state is None or entity_id in grouped:
                return
            grouped[entity_id] = (weight, relation, self._entity_record(entity_id, state, relation))

        add(100, "self", event.entity_id)

        if device_id:
            count = 0
            for entity_id in self._registry.entities:
                if count >= 6:
                    break
                if entity_id == event.entity_id:
                    continue
                if self._registry.device_of(entity_id) == device_id:
                    add(80, "same_device", entity_id)
                    count += 1

        if area_id:
            count = 0
            for entity_id in self._registry.entities:
                if count >= 8:
                    break
                if entity_id in grouped:
                    continue
                if self._registry.area_of(entity_id) == area_id:
                    add(60, "same_area", entity_id)
                    count += 1

        count = 0
        for entity_id in self._registry.entities:
            if count >= 5:
                break
            if entity_id in grouped:
                continue
            if entity_id.split(".", 1)[0] == event.domain:
                add(30, "same_domain", entity_id)
                count += 1

        return sorted(grouped.values(), key=lambda item: -item[0])

    def _entity_record(self, entity_id: str, state: dict, relation: str) -> dict:
        entry = self._registry.entities.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        domain = entity_id.split(".", 1)[0]
        name = (
            entry.get("name")
            or entry.get("original_name")
            or attributes.get("friendly_name")
            or entity_id
        )
        allowed = ATTR_WHITELIST.get(domain, ())
        record = {
            "entity_id": entity_id,
            "name": _u(name),
            "state": _u(state.get("state")),
            "relation": relation,
        }
        if allowed:
            record["attributes"] = {
                key: _u(attributes.get(key)) for key in allowed if key in attributes
            }
        return record

    @staticmethod
    def _fit(items: list[tuple[int, str, dict]], budget: int) -> tuple[list[dict], int]:
        kept: list[dict] = []
        used = 0
        dropped = 0
        for _weight, _relation, record in items:
            cost = estimate_tokens(json.dumps(record, ensure_ascii=False))
            if used + cost > budget:
                dropped += 1
                continue
            kept.append(record)
            used += cost
        return kept, dropped

    @staticmethod
    def _fit_items(value: Any, budget: int) -> tuple[Any, int]:
        if isinstance(value, dict):
            kept: dict[str, str] = {}
            used = 0
            for key, item in value.items():
                cost = estimate_tokens(f"{key}{item}")
                if used + cost > budget:
                    break
                kept[key] = item
                used += cost
            return kept, len(value) - len(kept)
        kept_list: list[dict] = []
        used = 0
        for item in value:
            cost = estimate_tokens(json.dumps(item, ensure_ascii=False))
            if used + cost > budget:
                break
            kept_list.append(item)
            used += cost
        return kept_list, len(value) - len(kept_list)

    async def _feedback(self, session, event: ProcessedEvent) -> list[dict]:
        cutoff = _utcnow() - timedelta(days=30)
        rows = list(
            (
                await session.execute(
                    select(Proposal.actions_json)
                    .where(
                        or_(
                            Proposal.entity_id == event.entity_id,
                            Proposal.entity_id.like(f"{event.domain}.%"),
                        ),
                        Proposal.created_at >= cutoff,
                    )
                    .order_by(Proposal.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        signatures: list[str] = []
        for raw in rows:
            try:
                actions = json.loads(raw or "[]")
            except ValueError:
                continue
            for action in actions:
                try:
                    service = f"{action['domain']}.{action['service']}"
                except (KeyError, TypeError):
                    continue
                signatures.append(
                    action_signature(service, action.get("entity_id"), action.get("data") or {})
                )
        if not signatures:
            return []
        unique = tuple(dict.fromkeys(signatures))
        feedback_rows = list(
            (
                await session.execute(
                    select(UserFeedback)
                    .where(UserFeedback.action_signature.in_(unique))
                    .order_by(UserFeedback.last_triggered_at.desc())
                    .limit(self._max_feedback)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "decision": row.decision,
                "count": row.count,
                "consecutive_rejects": row.consecutive_rejects,
                "last_triggered_at": row.last_triggered_at.isoformat(),
            }
            for row in feedback_rows
        ]

    async def _preferences(self, session, member_id: int, domain: str) -> dict[str, str]:
        rows = list(
            (
                await session.execute(
                    select(MemberPreference)
                    .where(
                        MemberPreference.member_id == member_id,
                        MemberPreference.key.like(f"{domain}.%"),
                    )
                    .limit(self._max_preferences)
                )
            )
            .scalars()
            .all()
        )
        return {
            row.key: _u(row.value_json or row.value) or "" for row in rows
        }
