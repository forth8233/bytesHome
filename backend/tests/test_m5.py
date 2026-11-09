from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.chat.service import ChatService
from app.db.models import EntityAlias, IntentRule, IntentRuleVersion
from app.db.session import SessionFactory
from app.fastpath.engine import FastPathEngine
from app.fastpath.grammar import parse_choice, parse_intent
from app.fastpath.normalize import normalize
from app.safety.guard import ActionGuard
from app.safety.whitelist import WhitelistRules

OLD = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
NOW = datetime.now(timezone.utc).isoformat()


class FPConfig:
    def __init__(self, *, enabled: bool = True, entity_allow=None) -> None:
        self.enabled = enabled
        self.entity_allow = entity_allow or (
            "light.*",
            "climate.*",
            "cover.*",
            "media_player.*",
            "lock.*",
            "sensor.*",
        )

    def global_settings(self) -> dict:
        return {
            "fast_path": {
                "enabled": self.enabled,
                "state_max_age_seconds": 60,
                "session_entity_ttl_seconds": 120,
                "max_candidates": 3,
                "member_rate_limit_per_minute": 30,
            },
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
        }

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=tuple(self.entity_allow),
            service_allow=(
                "light.turn_on",
                "light.turn_off",
                "climate.turn_on",
                "climate.turn_off",
                "climate.set_temperature",
                "climate.set_hvac_mode",
                "cover.open_cover",
                "cover.close_cover",
                "media_player.media_pause",
                "media_player.media_play",
                "lock.unlock",
                "lock.lock",
            ),
            param_rules={
                "light.turn_on": {"brightness_pct": [1, 100]},
                "climate.set_temperature": {"temperature": [16, 30]},
            },
        )


class FakeCache:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}

    async def get(self, entity_id):
        return self.states.get(entity_id)

    async def snapshot(self):
        return dict(self.states)


class FakeRegistry:
    def __init__(self) -> None:
        self.area_ids = {
            "light.living": "living",
            "light.bedroom": "bedroom",
            "light.kitchen": "kitchen",
            "climate.living": "living",
            "cover.living": "living",
            "sensor.living_temp": "living",
            "sensor.bedroom_humidity": "bedroom",
            "lock.front": "entry",
        }
        self.names = {
            "living": "客厅",
            "bedroom": "卧室",
            "kitchen": "厨房",
            "entry": "玄关",
        }

    def area_of(self, entity_id):
        return self.area_ids.get(entity_id)

    def area_name(self, area_id):
        return self.names.get(area_id)


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.cache = FakeCache()
        self.registry = FakeRegistry()
        self.cache.states = {
            "light.living": {
                "state": "off",
                "attributes": {"friendly_name": "客厅灯"},
                "last_updated": OLD,
            },
            "light.bedroom": {
                "state": "on",
                "attributes": {"friendly_name": "卧室灯"},
                "last_updated": OLD,
            },
            "light.kitchen": {
                "state": "off",
                "attributes": {"friendly_name": "厨房灯"},
                "last_updated": OLD,
            },
            "climate.living": {
                "state": "off",
                "attributes": {"friendly_name": "客厅空调", "current_temperature": 24.5},
                "last_updated": OLD,
            },
            "cover.living": {
                "state": "closed",
                "attributes": {"friendly_name": "客厅窗帘"},
                "last_updated": OLD,
            },
            "sensor.living_temp": {
                "state": "24.5",
                "attributes": {"friendly_name": "客厅温度", "device_class": "temperature"},
                "last_updated": OLD,
            },
            "sensor.bedroom_humidity": {
                "state": "48",
                "attributes": {"friendly_name": "卧室湿度", "device_class": "humidity"},
                "last_updated": OLD,
            },
            "lock.front": {
                "state": "locked",
                "attributes": {"friendly_name": "大门锁"},
                "last_updated": OLD,
            },
        }

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return []


class FakeProposals:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return (
            {
                "id": len(self.created),
                "status": "pending",
                "risk_level": kwargs.get("risk_level"),
            },
            "created",
        )


class ExplodingLLM:
    async def structured_with_fallback(self, *args, **kwargs):
        raise AssertionError("fast path must not call the LLM")


def member() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin", role="admin")


def build_engine(config=None, adapter=None):
    config = config or FPConfig()
    adapter = adapter or FakeAdapter()
    proposals = FakeProposals()
    guard = ActionGuard(config, SessionFactory)
    engine = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposals,
    )
    return engine, adapter, proposals, config


async def cleanup() -> None:
    async with SessionFactory() as session:
        rule_ids = list(
            (
                await session.scalars(
                    select(IntentRule.id).where(IntentRule.name.like("M5%"))
                )
            ).all()
        )
        if rule_ids:
            await session.execute(
                delete(IntentRuleVersion).where(IntentRuleVersion.rule_id.in_(rule_ids))
            )
        await session.execute(delete(IntentRule).where(IntentRule.name.like("M5%")))
        await session.execute(delete(EntityAlias).where(EntityAlias.alias.like("m5-%")))
        await session.commit()


@pytest.fixture
async def env(client):
    await cleanup()
    engine, adapter, proposals, config = build_engine()
    await engine.reload()
    yield SimpleNamespace(
        engine=engine, adapter=adapter, proposals=proposals, config=config
    )
    await cleanup()


async def test_adapter_validates_each_batched_entity() -> None:
    from app.ha.adapter import HAAdapter
    from app.ha.client_rest import HAClientError

    class FakeRest:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def call_service(self, domain, service, data):
            self.calls.append((domain, service, data))
            return []

    adapter = HAAdapter(FPConfig())
    adapter.rest = FakeRest()
    await adapter.call_service(
        "light", "turn_off", {"entity_id": ["light.living", "light.bedroom"]}
    )
    assert len(adapter.rest.calls) == 1

    with pytest.raises(HAClientError):
        await adapter.call_service(
            "light", "turn_off", {"entity_id": ["light.living", "shell_command.reboot"]}
        )


def test_normalize_removes_fillers_and_converts_numbers() -> None:
    assert normalize("请帮我把客厅的灯关一下。") == "把客厅的灯关"
    assert normalize("空调调到二十六度") == "空调调到26度"
    assert normalize("亮度调到５０％") == "亮度调到50%"


GOLDEN = [
    ("关掉客厅的灯", "control", "light.turn_off", "客厅"),
    ("把客厅灯关了", "control", "light.turn_off", "客厅"),
    ("客厅灯关一下", "control", "light.turn_off", "客厅"),
    ("打开卧室灯", "control", "light.turn_on", "卧室"),
    ("全屋灯关掉", "control", "light.turn_off", "全屋"),
    ("关闭厨房的灯", "control", "light.turn_off", "厨房"),
    ("关空调", "control", "climate.turn_off", ""),
    ("把客厅空调调到26度", "control", "climate.set_temperature", "客厅"),
    ("空调调到二十六度", "control", "climate.set_temperature", ""),
    ("客厅空调设为制热模式", "control", "climate.set_hvac_mode", "客厅"),
    ("打开窗帘", "control", "cover.open_cover", ""),
    ("关上客厅窗帘", "control", "cover.close_cover", "客厅"),
    ("暂停电视", "control", "media_player.media_pause", ""),
    ("继续播放音乐", "control", "media_player.media_play", ""),
    ("把客厅灯亮度调到30%", "control", "light.turn_on", "客厅"),
    ("开锁", "control", "lock.unlock", ""),
    ("锁门", "control", "lock.lock", ""),
    ("/light off 客厅", "control", "light.turn_off", "客厅"),
    ("/climate temp 26 客厅", "control", "climate.set_temperature", "客厅"),
    ("客厅灯开着吗", "query", "state", "客厅"),
    ("卧室灯关着吗", "query", "state", "卧室"),
    ("客厅温度多少度", "query", "temperature", "客厅"),
    ("卧室湿度是多少", "query", "humidity", "卧室"),
    ("把灯关掉", "control", "light.turn_off", ""),
    ("帮我打开客厅的灯", "control", "light.turn_on", "客厅"),
    ("麻烦关掉卧室灯", "control", "light.turn_off", "卧室"),
    ("全部灯都关掉", "control", "light.turn_off", "全部"),
    ("客厅空调调至24度", "control", "climate.set_temperature", "客厅"),
]


def test_golden_set_hit_rate_at_least_90_percent() -> None:
    hits = 0
    misses = []
    for text, kind, expected, room in GOLDEN:
        intent = parse_intent(text)
        got = None
        target = ""
        if intent is not None:
            target = intent.target or ""
            if kind == "control" and intent.kind == "control" and intent.service:
                got = f"{intent.domain}.{intent.service}"
            elif kind == "query" and intent.kind == "query":
                got = intent.query
        if got == expected and (not room or normalize(room) in normalize(target)):
            hits += 1
        else:
            misses.append((text, expected, got, target))
    rate = hits / len(GOLDEN)
    assert rate >= 0.9, f"hit rate {rate:.2%}, misses={misses}"


def test_parse_choice() -> None:
    assert parse_choice("第一个") == 0
    assert parse_choice("2") == 1
    assert parse_choice("第三个") == 2
    assert parse_choice("客厅灯") is None


async def test_fast_path_executes_without_llm(env) -> None:
    result = await env.engine.route(member(), "关客厅灯")
    assert result is not None
    assert result.status == "executed"
    assert env.adapter.calls == [
        ("light", "turn_off", {"entity_id": "light.living"})
    ]
    assert "已关闭客厅灯" in result.reply
    assert result.latency_ms >= 0


async def test_shorthand_all_lights(env) -> None:
    result = await env.engine.route(member(), "关灯")
    assert result.status == "executed"
    assert len(env.adapter.calls) == 1
    assert set(env.adapter.calls[0][2]["entity_id"]) == {
        "light.living",
        "light.bedroom",
        "light.kitchen",
    }
    assert "3 盏灯" in result.reply


async def test_alias_resolution(env) -> None:
    async with SessionFactory() as session:
        session.add(
            EntityAlias(
                entity_id="light.living",
                alias="m5-主灯",
                display_name="客厅主灯",
                area_id="living",
                domain="light",
                can_control=True,
            )
        )
        await session.commit()
    await env.engine.reload()
    result = await env.engine.route(member(), "关m5-主灯")
    assert result.status == "executed"
    assert env.adapter.calls == [
        ("light", "turn_off", {"entity_id": "light.living"})
    ]


async def test_ambiguous_target_clarifies_then_executes(env) -> None:
    env.adapter.cache.states["light.living"]["attributes"]["friendly_name"] = "吸顶灯"
    env.adapter.cache.states["light.bedroom"]["attributes"]["friendly_name"] = "吸顶灯"

    first = await env.engine.route(member(), "关吸顶灯", session_id=7)
    assert first.status == "clarify"
    assert len(first.candidates) == 2
    assert env.adapter.calls == []
    assert "请回复序号" in first.reply

    second = await env.engine.route(member(), "1", session_id=7)
    assert second.status == "executed"
    assert len(env.adapter.calls) == 1


async def test_high_risk_becomes_proposal(env) -> None:
    result = await env.engine.route(member(), "开大门锁")
    assert result.status == "proposal"
    assert env.adapter.calls == []
    assert env.proposals.created
    assert "待确认提案" in result.reply


async def test_denied_when_entity_not_whitelisted(env) -> None:
    engine, adapter, _, _ = build_engine(
        FPConfig(entity_allow=("light.living",))
    )
    await engine.reload()
    result = await engine.route(member(), "关灯")
    assert result.status == "denied"
    assert adapter.calls == []
    assert "安全策略拦截" in result.reply


async def test_query_state_and_temperature(env) -> None:
    state = await env.engine.route(member(), "客厅灯开着吗")
    assert state.status == "executed"
    assert "关着" in state.reply

    temperature = await env.engine.route(member(), "客厅温度是多少")
    assert temperature.status == "executed"
    assert "24.5 度" in temperature.reply


async def test_scene_rule_executes_all_targets(env) -> None:
    async with SessionFactory() as session:
        session.add(
            IntentRule(
                name="M5晚安",
                kind="scene",
                enabled=True,
                priority=100,
                match_json=json.dumps({"phrases": ["m5-晚安模式"]}, ensure_ascii=False),
                action_json=json.dumps(
                    [
                        {
                            "domain": "light",
                            "service": "turn_off",
                            "target": {"type": "domain_all", "domain": "light"},
                        }
                    ],
                    ensure_ascii=False,
                ),
                reply_template="晚安，已关闭 {count} 盏灯。",
                version=1,
            )
        )
        await session.commit()
    await env.engine.reload()
    result = await env.engine.route(member(), "m5-晚安模式")
    assert result.status == "executed"
    assert result.rule_id is not None
    assert len(env.adapter.calls) == 1
    assert len(env.adapter.calls[0][2]["entity_id"]) == 3
    assert result.reply == "晚安，已关闭 3 盏灯。"


async def test_session_reference_repeats_last_action(env) -> None:
    await env.engine.route(member(), "关客厅灯", session_id=9)
    result = await env.engine.route(member(), "再关一次", session_id=9)
    assert result.status == "executed"
    assert len(env.adapter.calls) == 2


async def test_already_satisfied_state_is_not_executed(env) -> None:
    env.adapter.cache.states["light.living"]["last_updated"] = NOW
    result = await env.engine.route(member(), "关客厅灯")
    assert result.status == "executed"
    assert env.adapter.calls == []
    assert "本来就是关着的" in result.reply


async def test_disabled_fast_path_falls_back(env) -> None:
    engine, adapter, _, _ = build_engine(FPConfig(enabled=False))
    await engine.reload()
    assert await engine.route(member(), "关客厅灯") is None
    assert adapter.calls == []


async def test_chat_service_uses_fast_path_when_llm_down(client) -> None:
    await cleanup()
    config = FPConfig()
    adapter = FakeAdapter()
    guard = ActionGuard(config, SessionFactory)
    proposals = FakeProposals()
    engine = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposals,
    )
    await engine.reload()
    service = ChatService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposals,
        client=ExplodingLLM(),
        fast_path=engine,
    )
    result = await service.chat(member(), "关客厅灯")
    assert result["route"] == "fast_path"
    assert result["route_status"] == "executed"
    assert "已关闭客厅灯" in result["reply"]
    assert adapter.calls
    await cleanup()


def test_fastpath_rule_api_crud_and_rollback(client, auth_headers) -> None:
    client.post(
        "/api/config/whitelist",
        json={
            "entities": {"allow": ["light.*", "sensor.*"], "deny": []},
            "services": {
                "allow": ["light.turn_on", "light.turn_off"],
                "deny": [],
            },
            "param_rules": {"light.turn_on": {"brightness_pct": [1, 100]}},
        },
        headers=auth_headers,
    )
    created = client.post(
        "/api/fastpath/rules",
        json={
            "name": "M5API回家",
            "phrases": ["m5-我回家了"],
            "actions": [
                {
                    "domain": "light",
                    "service": "turn_on",
                    "target": {"type": "entity", "entity_id": "light.m5api"},
                }
            ],
            "reply_template": "欢迎回家。",
        },
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    rule = created.json()
    rule_id = rule["id"]
    assert rule["version"] == 1

    invalid = client.post(
        "/api/fastpath/rules",
        json={
            "name": "M5API非法",
            "phrases": ["m5-非法"],
            "actions": [
                {
                    "domain": "shell_command",
                    "service": "reboot",
                    "target": {"type": "entity", "entity_id": "shell_command.x"},
                }
            ],
        },
        headers=auth_headers,
    )
    assert invalid.status_code == 422

    updated = client.put(
        f"/api/fastpath/rules/{rule_id}",
        json={
            "name": "M5API回家",
            "phrases": ["m5-我回来了", "m5-到家了"],
            "actions": rule["actions"],
            "reply_template": "欢迎回来。",
            "priority": 120,
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    versions = client.get(
        f"/api/fastpath/rules/{rule_id}/versions", headers=auth_headers
    )
    assert versions.status_code == 200
    assert len(versions.json()["items"]) == 2

    rollback = client.post(
        f"/api/fastpath/rules/{rule_id}/rollback",
        json={"version": 1},
        headers=auth_headers,
    )
    assert rollback.status_code == 200
    assert rollback.json()["version"] == 3
    assert rollback.json()["phrases"] == ["m5-我回家了"]

    deleted = client.delete(f"/api/fastpath/rules/{rule_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_fastpath_alias_api_crud(client, auth_headers) -> None:
    created = client.post(
        "/api/fastpath/aliases",
        json={
            "entity_id": "light.m5alias",
            "alias": "m5-门口灯",
            "display_name": "门口灯",
            "area_id": "entry",
        },
        headers=auth_headers,
    )
    assert created.status_code == 200, created.text
    alias = created.json()
    assert alias["domain"] == "light"

    duplicate = client.post(
        "/api/fastpath/aliases",
        json={"entity_id": "light.m5alias2", "alias": "m5-门口灯"},
        headers=auth_headers,
    )
    assert duplicate.status_code == 409

    listed = client.get(
        "/api/fastpath/aliases", params={"q": "m5-门口灯"}, headers=auth_headers
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    updated = client.put(
        f"/api/fastpath/aliases/{alias['id']}",
        json={
            "entity_id": "light.m5alias",
            "alias": "m5-门厅灯",
            "display_name": "门厅灯",
            "area_id": "entry",
            "can_control": True,
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["alias"] == "m5-门厅灯"

    deleted = client.delete(
        f"/api/fastpath/aliases/{alias['id']}", headers=auth_headers
    )
    assert deleted.status_code == 200


def test_fastpath_test_and_stats_endpoints(client, auth_headers) -> None:
    preview = client.post(
        "/api/fastpath/test", json={"text": "关灯"}, headers=auth_headers
    )
    assert preview.status_code == 200
    assert preview.json()["intent"]["domain"] == "light"

    stats = client.get("/api/stats/fastpath", headers=auth_headers)
    assert stats.status_code == 200
    assert stats.json()["available"] is True
