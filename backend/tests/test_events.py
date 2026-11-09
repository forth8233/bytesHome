import asyncio
from datetime import datetime, timezone

import pytest

from app.db.models import ProcessedEventLog
from app.events.bus import EventBus
from app.events.debounce import Debouncer
from app.events.filters import DedupeFilter, StormLimiter, attribute_delta, is_effective
from app.events.log_sink import EventLogSink
from app.events.models import RawEvent
from app.events.normalize import normalize
from app.events.pipeline import EventPipeline
from app.events.risk import pre_risk
from app.ha.registry import RegistryCache


def _state(value: str, **attributes) -> dict:
    return {"entity_id": "sensor.t", "state": value, "attributes": attributes}


def _raw(old, new, event_id: str = "ctx-1") -> RawEvent:
    return RawEvent(event_id=event_id, entity_id="sensor.t", old_state=old, new_state=new)


class StubConfig:
    def __init__(self, allow=("sensor.*",), window=0.01):
        self._allow = list(allow)
        self._window = window

    def whitelist(self):
        from app.safety.whitelist import WhitelistRules

        return WhitelistRules(entity_allow=tuple(self._allow))

    def global_settings(self):
        return {
            "debounce_seconds": {"default": self._window},
            "storm_limit": {
                "per_entity_per_minute": 6,
                "per_domain_per_minute": 30,
                "storm_interval_seconds": 300,
            },
        }


def test_attribute_delta_ignores_volatile_fields() -> None:
    old = _state("on", friendly_name="A", last_updated="1", brightness=10)
    new = _state("on", friendly_name="B", last_updated="2", brightness=10)
    assert attribute_delta(old, new) == {}


def test_is_effective_rejects_noise() -> None:
    assert is_effective(_raw(_state("on", last_updated="1"), _state("on", last_updated="2"))) is False
    assert is_effective(_raw(_state("on"), _state("off"))) is True
    assert is_effective(_raw(_state("on", b=1), _state("on", b=2))) is True
    assert is_effective(_raw(None, _state("on"))) is True


def test_pre_risk_table() -> None:
    assert pre_risk("binary_sensor", {"device_class": "smoke"}, "on") == "high"
    assert pre_risk("binary_sensor", {"device_class": "door"}, "on") == "medium"
    assert pre_risk("binary_sensor", {"device_class": "door"}, "off") == "low"
    assert pre_risk("lock", {}, "locked") == "high"
    assert pre_risk("sensor", {}, "18") == "low"


def test_dedupe_filter_ttl() -> None:
    dedupe = DedupeFilter(ttl_seconds=10)
    assert dedupe.duplicate("a", now=100.0) is False
    assert dedupe.duplicate("a", now=105.0) is True
    assert dedupe.duplicate("a", now=120.0) is False


def test_storm_limiter_emits_first_storm_then_silences() -> None:
    limiter = StormLimiter(per_entity_per_minute=2, per_domain_per_minute=10, window_seconds=60)
    assert limiter.admit("sensor.t", "sensor", now=0).allowed is True
    assert limiter.admit("sensor.t", "sensor", now=1).allowed is True
    first = limiter.admit("sensor.t", "sensor", now=2)
    assert (first.allowed, first.suppressed_count) == (False, 1)
    quiet = limiter.admit("sensor.t", "sensor", now=3)
    assert (quiet.allowed, quiet.suppressed_count) == (False, 0)


def test_storm_limiter_caps_per_domain() -> None:
    limiter = StormLimiter(per_entity_per_minute=100, per_domain_per_minute=2, window_seconds=60)
    assert limiter.admit("sensor.a", "sensor", now=0).allowed is True
    assert limiter.admit("sensor.b", "sensor", now=1).allowed is True
    third = limiter.admit("sensor.c", "sensor", now=2)
    assert third.allowed is False
    assert third.suppressed_count == 1


async def test_debouncer_merges_events() -> None:
    emitted: list[tuple[str, int, str | None]] = []

    async def on_flush(entity_id: str, raw: RawEvent, dropped: int) -> None:
        emitted.append((entity_id, dropped, raw.new_state["state"]))

    debouncer = Debouncer(lambda _: 0.03, on_flush)
    await debouncer.add(_raw(_state("1"), _state("2")))
    await debouncer.add(_raw(_state("2"), _state("3")))
    await debouncer.add(_raw(_state("3"), _state("4")))
    await asyncio.sleep(0.1)
    assert emitted == [("sensor.t", 2, "4")]
    await debouncer.stop()


def test_normalize_classifies_event_types() -> None:
    registry = RegistryCache()
    registry.entities["sensor.t"] = {"entity_id": "sensor.t", "name": "温度", "area_id": "living"}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    recovered = normalize(_raw(_state("unavailable"), _state("20")), registry=registry, now=now)
    assert recovered.event_type == "recovered"
    assert recovered.entity_name == "温度"
    assert recovered.area_id == "living"

    attribute = normalize(_raw(_state("on", b=1), _state("on", b=2)), registry=registry, now=now)
    assert attribute.event_type == "attribute_changed"
    assert attribute.attributes_delta == {"b": 2}

    changed = normalize(_raw(_state("on"), _state("off")), registry=registry, now=now)
    assert changed.event_type == "state_changed"
    assert changed.dedupe_key == normalize(
        _raw(_state("on"), _state("off")), registry=registry, now=now
    ).dedupe_key


async def test_bus_drops_oldest_when_subscriber_is_full() -> None:
    bus = EventBus(maxsize=2)
    queue = bus.subscribe("test")
    for i in range(4):
        await bus.publish(_event(f"e{i}"))
    assert bus.dropped_total == 2
    remaining = [queue.get_nowait().event_id for _ in range(queue.qsize())]
    assert remaining == ["e2", "e3"]


def _event(event_id: str):
    from app.events.models import ProcessedEvent

    return ProcessedEvent(
        event_id=event_id,
        entity_id="sensor.t",
        entity_name="t",
        domain="sensor",
        event_type="state_changed",
        old_state="1",
        new_state="2",
        risk_pre="low",
        dedupe_key=event_id,
    )


async def test_pipeline_filters_and_publishes() -> None:
    bus = EventBus()
    queue = bus.subscribe("test")
    pipeline = EventPipeline(StubConfig(), RegistryCache(), bus)

    await pipeline.handle(RawEvent("", "light.x", None, _state("on")))  # 白名单外
    await pipeline.handle(_raw(_state("on"), _state("on")))  # 无信息量
    await pipeline.handle(RawEvent("dup", "sensor.t", _state("1"), _state("2")))
    await pipeline.handle(RawEvent("dup", "sensor.t", _state("1"), _state("2")))
    await asyncio.sleep(0.1)

    stats = pipeline.stats()
    assert stats["dropped_whitelist"] == 1
    assert stats["dropped_ineffective"] == 1
    assert stats["dropped_duplicate"] == 1
    published = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(published) == 1
    assert published[0].entity_id == "sensor.t"


async def test_log_sink_persists_events(client) -> None:
    from app.db.session import SessionFactory

    queue: asyncio.Queue = asyncio.Queue()
    sink = EventLogSink(SessionFactory, queue, batch_size=10, flush_interval=0.01)
    await sink.start()
    await queue.put(_event("persist-1"))
    await asyncio.sleep(0.1)

    from sqlalchemy import select

    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(ProcessedEventLog).where(ProcessedEventLog.event_id == "persist-1")
            )
        ).scalars().all()
    await sink.stop()
    assert len(rows) == 1
    assert rows[0].entity_id == "sensor.t"
