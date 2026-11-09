from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.session import SessionFactory
from app.fastpath.engine import FastPathEngine
from app.fastpath.entities import AliasRecord
from app.fastpath.voice import VoiceLexicon, VoiceParser
from app.fastpath.voice_import import load_seed_entries
from app.fastpath.voice_lexicon import parse_workbook
from app.safety.guard import ActionGuard
from app.safety.whitelist import WhitelistRules

WORKBOOK = (
    Path(__file__).resolve().parents[2]
    / "智能家居语音技能"
    / "语音控制技能_系统词库_汇总版20250208170728.xlsx"
)
NOW = datetime.now(timezone.utc).isoformat()

ALIASES = [
    AliasRecord("light.living", "客厅灯", "客厅灯", "living", "light", True),
    AliasRecord("light.bedroom", "卧室灯", "卧室灯", "bedroom", "light", True),
    AliasRecord("light.kitchen", "厨房灯", "厨房灯", "kitchen", "light", True),
    AliasRecord("climate.living", "客厅空调", "客厅空调", "living", "climate", True),
    AliasRecord("cover.living", "客厅窗帘", "客厅窗帘", "living", "cover", True),
    AliasRecord(
        "media_player.living_tv", "客厅电视", "客厅电视", "living", "media_player", True
    ),
    AliasRecord("switch.kitchen", "厨房插座", "厨房插座", "kitchen", "switch", True),
    AliasRecord(
        "sensor.living_temp", "客厅温度", "客厅温度", "living", "sensor", False
    ),
]


class VoiceConfig:
    def __init__(self, *, voice_enabled: bool = True, max_group: int = 50) -> None:
        self.voice_enabled = voice_enabled
        self.max_group = max_group

    def global_settings(self) -> dict:
        return {
            "fast_path": {
                "enabled": True,
                "state_max_age_seconds": 300,
                "session_entity_ttl_seconds": 120,
                "max_candidates": 3,
                "member_rate_limit_per_minute": 60,
                "voice_enabled": self.voice_enabled,
                "voice_max_group_actions": self.max_group,
                "voice_default_steps": {},
            },
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
        }

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=(
                "light.*",
                "climate.*",
                "cover.*",
                "media_player.*",
                "sensor.*",
                "scene.*",
                "script.*",
                "automation.*",
            ),
            service_allow=(
                "light.turn_on",
                "light.turn_off",
                "climate.turn_on",
                "climate.turn_off",
                "climate.set_temperature",
                "climate.set_hvac_mode",
                "climate.set_fan_mode",
                "cover.open_cover",
                "cover.close_cover",
                "cover.set_cover_position",
                "media_player.volume_set",
                "media_player.select_source",
                "scene.turn_on",
                "script.turn_on",
                "script.turn_off",
                "automation.trigger",
                "automation.turn_on",
                "automation.turn_off",
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
    areas = {"living": {"name": "客厅"}, "bedroom": {"name": "卧室"}, "kitchen": {"name": "厨房"}}
    floors = {}

    def __init__(self) -> None:
        self.area_ids = {
            "light.living": "living",
            "light.bedroom": "bedroom",
            "light.kitchen": "kitchen",
            "climate.living": "living",
            "cover.living": "living",
            "media_player.living_tv": "living",
            "switch.kitchen": "kitchen",
            "sensor.living_temp": "living",
        }

    def area_of(self, entity_id):
        return self.area_ids.get(entity_id)

    def area_name(self, area_id):
        return (self.areas.get(area_id) or {}).get("name")

    def floor_of(self, entity_id):
        return None


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.cache = FakeCache()
        self.registry = FakeRegistry()
        self.cache.states = {
            "light.living": {
                "state": "on",
                "attributes": {"friendly_name": "客厅灯", "brightness": 128},
                "last_updated": NOW,
            },
            "light.bedroom": {
                "state": "on",
                "attributes": {
                    "friendly_name": "卧室灯",
                    "brightness": 200,
                    "supported_color_modes": ["color_temp"],
                    "color_temp_kelvin": 4000,
                    "min_color_temp_kelvin": 2700,
                    "max_color_temp_kelvin": 6500,
                },
                "last_updated": NOW,
            },
            "light.kitchen": {
                "state": "off",
                "attributes": {"friendly_name": "厨房灯", "brightness": 0},
                "last_updated": NOW,
            },
            "climate.living": {
                "state": "cool",
                "attributes": {
                    "friendly_name": "客厅空调",
                    "temperature": 26,
                    "hvac_mode": "cool",
                    "fan_mode": "medium",
                    "min_temp": 16,
                    "max_temp": 30,
                    "target_temp_step": 1,
                    "hvac_modes": ["off", "cool", "heat", "auto"],
                    "fan_modes": ["low", "medium", "high", "auto"],
                },
                "last_updated": NOW,
            },
            "cover.living": {
                "state": "open",
                "attributes": {"friendly_name": "客厅窗帘", "current_position": 60},
                "last_updated": NOW,
            },
            "media_player.living_tv": {
                "state": "on",
                "attributes": {
                    "friendly_name": "客厅电视",
                    "volume_level": 0.3,
                    "source_list": ["TV", "HDMI1", "Bluetooth"],
                    "source": "TV",
                },
                "last_updated": NOW,
            },
            "switch.kitchen": {
                "state": "off",
                "attributes": {"friendly_name": "厨房插座"},
                "last_updated": NOW,
            },
            "sensor.living_temp": {
                "state": "24.5",
                "attributes": {
                    "friendly_name": "客厅温度",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                },
                "last_updated": NOW,
            },
            "scene.hui_jia": {
                "state": "2026-11-05T06:53:01.271327+00:00",
                "attributes": {
                    "friendly_name": "回家",
                    "entity_id": ["light.living", "light.kitchen"],
                },
                "last_updated": NOW,
            },
            "script.qing_jian": {
                "state": "off",
                "attributes": {"friendly_name": "夜间清理"},
                "last_updated": NOW,
            },
            "automation.guan_deng": {
                "state": "on",
                "attributes": {
                    "friendly_name": "22点关灯",
                    "id": "ha_agent_test",
                    "mode": "single",
                },
                "last_updated": NOW,
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
        return ({"id": len(self.created), "status": "pending"}, "created")


def admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin", role="admin")


def build_parser() -> VoiceParser:
    if not WORKBOOK.exists():
        pytest.skip("voice lexicon workbook missing")
    parsed = parse_workbook(WORKBOOK)
    lexicon = VoiceLexicon.from_entries(list(parsed.entries) + load_seed_entries())
    return VoiceParser(lexicon, ALIASES, FakeRegistry())


@pytest.fixture(scope="module")
def parser() -> VoiceParser:
    return build_parser()


@pytest.fixture()
def env(client, parser):
    config = VoiceConfig()
    adapter = FakeAdapter()
    proposals = FakeProposals()
    engine = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=ActionGuard(config, SessionFactory),
        proposals=proposals,
    )
    engine._voice = parser
    return SimpleNamespace(
        engine=engine, adapter=adapter, proposals=proposals, config=config, parser=parser
    )


async def test_voice_directory_lists_live_entities(env) -> None:
    directory = await env.engine.voice_directory()

    by_id = {item["entity_id"]: item for item in directory["entities"]}
    assert by_id["light.living"]["device_type"] == "light"
    assert by_id["light.living"]["area_id"] == "living"
    assert by_id["climate.living"]["device_type"] == "AC"
    assert by_id["cover.living"]["device_type"] == "curtain"
    assert "sensor.living_temp" not in by_id


async def test_single_control_executes_with_voice_reply(env) -> None:
    result = await env.engine.route(admin(), "把客厅灯亮度调到50%")

    assert result is not None
    assert result.status == "executed"
    assert result.reply == "客厅灯的亮度已设为50%。"
    assert env.adapter.calls == [
        ("light", "turn_on", {"brightness_pct": 50, "entity_id": "light.living"})
    ]


async def test_group_control_batches_entities(env) -> None:
    result = await env.engine.route(admin(), "全屋灯光调到50%")

    assert result is not None
    assert result.status == "executed"
    assert len(env.adapter.calls) == 1
    domain, service, payload = env.adapter.calls[0]
    assert (domain, service) == ("light", "turn_on")
    assert payload["brightness_pct"] == 50
    assert set(payload["entity_id"]) == {
        "light.living",
        "light.bedroom",
        "light.kitchen",
    }
    assert env.engine.stats()["counters"]["voice_group_actions"] == 3


async def test_group_control_limit_rejects_without_calls(client, parser) -> None:
    config = VoiceConfig(max_group=1)
    adapter = FakeAdapter()
    engine = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=ActionGuard(config, SessionFactory),
        proposals=FakeProposals(),
    )
    engine._voice = parser

    result = await engine.route(admin(), "全屋灯光调到50%")

    assert result is not None
    assert result.status == "rejected"
    assert "上限" in result.reply
    assert adapter.calls == []


async def test_query_does_not_execute(env) -> None:
    result = await env.engine.route(admin(), "查询客厅灯的状态")

    assert result is not None
    assert result.status == "executed"
    assert result.reply == "查询到：客厅灯亮度50.2%。"
    assert env.adapter.calls == []


async def test_already_satisfied_state_is_not_executed(env) -> None:
    result = await env.engine.route(admin(), "打开客厅灯")

    assert result is not None
    assert "本来就是这个状态" in result.reply
    assert env.adapter.calls == []


async def test_guard_denies_unlisted_entity(env) -> None:
    result = await env.engine.route(admin(), "打开厨房插座")

    assert result is not None
    assert result.status == "denied"
    assert "安全策略" in result.reply
    assert env.adapter.calls == []


async def test_unsupported_target_falls_back_to_slow_path(env) -> None:
    assert await env.engine.route(admin(), "客厅调亮一点") is None
    assert await env.engine.route(admin(), "把客厅灯打开然后关闭卧室灯") is None
    assert env.adapter.calls == []


async def test_disabled_voice_restores_old_path(client, parser) -> None:
    config = VoiceConfig(voice_enabled=False)
    adapter = FakeAdapter()
    engine = FastPathEngine(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=ActionGuard(config, SessionFactory),
        proposals=FakeProposals(),
    )
    engine._voice = parser

    assert await engine.route(admin(), "客厅灯亮度调到百分之三十") is None
    assert adapter.calls == []


async def test_preview_does_not_execute(env) -> None:
    payload = await env.engine.test("把客厅灯亮度调到50%", admin())

    voice = payload["voice"]
    assert voice["route"] == "fast_path"
    assert voice["status"] == "ok"
    assert voice["actions"] == [
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.living",
            "data": {"brightness_pct": 50},
        }
    ]
    assert voice["guard"]["passed"] is True
    assert env.adapter.calls == []


async def test_relative_enum_control_executes(env) -> None:
    result = await env.engine.route(admin(), "客厅空调风速调低一档")

    assert result is not None
    assert result.status == "executed"
    assert env.adapter.calls == [
        (
            "climate",
            "set_fan_mode",
            {"fan_mode": "low", "entity_id": "climate.living"},
        )
    ]


async def test_group_partial_reply_lists_only_executed(env) -> None:
    result = await env.engine.route(admin(), "全屋灯光色温调到3000K")

    assert result is not None
    assert result.status == "executed", result.reply
    assert result.reply == "已将全屋1个灯光色温设为3000K，分别为：“卧室灯”。"


async def test_relative_with_trailing_suffix_executes(env) -> None:
    result = await env.engine.route(admin(), "客厅空调调暖些")

    assert result is not None, "调暖些 应被快路径认领"
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "climate",
            "set_temperature",
            {"temperature": 26.5, "entity_id": "climate.living"},
        )
    ]


async def test_relative_with_trailing_dot_executes(env) -> None:
    result = await env.engine.route(admin(), "客厅空调调暖点")

    assert result is not None, "调暖点 应被快路径认领"
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "climate",
            "set_temperature",
            {"temperature": 26.5, "entity_id": "climate.living"},
        )
    ]


async def test_color_temp_relative_cold_raises_kelvin(env) -> None:
    result = await env.engine.route(admin(), "卧室灯色温调冷500k")

    assert result is not None
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "light",
            "turn_on",
            {"color_temp_kelvin": 4500, "entity_id": "light.bedroom"},
        )
    ]


async def test_color_temp_relative_warm_lowers_kelvin(env) -> None:
    result = await env.engine.route(admin(), "卧室灯色温调暖500k")

    assert result is not None
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "light",
            "turn_on",
            {"color_temp_kelvin": 3500, "entity_id": "light.bedroom"},
        )
    ]


async def test_air_conditioner_cold_still_lowers_temperature(env) -> None:
    result = await env.engine.route(admin(), "客厅空调调冷2度")

    assert result is not None
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "climate",
            "set_temperature",
            {"temperature": 24, "entity_id": "climate.living"},
        )
    ]


async def test_color_temp_set_to_value_after_direction_word(env) -> None:
    result = await env.engine.route(admin(), "色温灯调冷到5000k")

    assert result is not None
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        (
            "light",
            "turn_on",
            {"color_temp_kelvin": 5000, "entity_id": "light.bedroom"},
        )
    ]


async def test_ha_scene_can_be_triggered_by_voice(env) -> None:
    result = await env.engine.route(admin(), "执行回家场景")

    assert result is not None, "HA 场景应被快路径认领"
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        ("scene", "turn_on", {"entity_id": "scene.hui_jia"})
    ]


async def test_ha_scene_without_verb_is_not_claimed(env) -> None:
    assert await env.engine.route(admin(), "回家") is None
    assert env.adapter.calls == []


async def test_automation_can_be_triggered_by_voice(env) -> None:
    result = await env.engine.route(admin(), "触发22点关灯自动化")

    assert result is not None, "自动化应被快路径认领"
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        ("automation", "trigger", {"entity_id": "automation.guan_deng"})
    ]


async def test_script_can_be_run_by_voice(env) -> None:
    result = await env.engine.route(admin(), "执行夜间清理脚本")

    assert result is not None, "脚本应被快路径认领"
    assert result.status == "executed", result.reply
    assert env.adapter.calls == [
        ("script", "turn_on", {"entity_id": "script.qing_jian"})
    ]


async def test_scene_query_is_not_treated_as_control(env) -> None:
    result = await env.engine.route(admin(), "查询回家场景")

    assert result is not None
    assert result.status == "executed", result.reply
    assert env.adapter.calls == []


async def test_clock_phrase_is_not_claimed(env) -> None:
    assert await env.engine.route(admin(), "七点开灯") is None
    assert env.adapter.calls == []


async def test_function_clarify_then_select_by_index(env) -> None:
    first = await env.engine.route(admin(), "空调调到最大")

    assert first is not None
    assert first.status == "clarify"
    assert "请选择" in first.reply
    assert env.adapter.calls == []

    second = await env.engine.route(admin(), "1")

    assert second is not None
    assert second.status == "executed", second.reply
    domain, service, payload = env.adapter.calls[0]
    assert (domain, service) == ("climate", "set_fan_mode")
    assert payload["fan_mode"] == "high"


async def test_function_clarify_then_select_by_label(env) -> None:
    first = await env.engine.route(admin(), "空调调到最大")
    assert first is not None and first.status == "clarify"

    second = await env.engine.route(admin(), "温度")

    assert second is not None
    assert second.status == "executed"
    domain, service, payload = env.adapter.calls[0]
    assert (domain, service) == ("climate", "set_temperature")
    assert payload["temperature"] == 30
