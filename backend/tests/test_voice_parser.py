from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.fastpath.entities import AliasRecord, EntityResolver
from app.fastpath.voice import VoiceLexicon, VoiceParser
from app.fastpath.voice_import import load_seed_entries
from app.fastpath.voice_lexicon import parse_workbook

WORKBOOK = (
    Path(__file__).resolve().parents[2]
    / "智能家居语音技能"
    / "语音控制技能_系统词库_汇总版20250208170728.xlsx"
)

STATES = {
    "light.living_ceiling": {
        "state": "on",
        "attributes": {
            "friendly_name": "客厅主灯",
            "brightness": 128,
            "color_temp_kelvin": 4000,
            "supported_color_modes": ["color_temp", "rgb"],
            "rgb_color": [255, 255, 255],
            "min_color_temp_kelvin": 2700,
            "max_color_temp_kelvin": 6500,
        },
    },
    "light.bedroom_light": {
        "state": "off",
        "attributes": {"friendly_name": "卧室灯", "brightness": 0},
    },
    "climate.living_ac": {
        "state": "cool",
        "attributes": {
            "friendly_name": "客厅空调",
            "temperature": 26,
            "hvac_mode": "cool",
            "fan_mode": "medium",
            "min_temp": 16,
            "max_temp": 30,
            "target_temp_step": 1,
            "hvac_modes": ["off", "cool", "heat", "dry", "fan_only", "auto"],
            "fan_modes": ["low", "medium", "high", "auto"],
        },
    },
    "cover.living_curtain": {
        "state": "open",
        "attributes": {"friendly_name": "客厅窗帘", "current_position": 60},
    },
    "media_player.living_tv": {
        "state": "on",
        "attributes": {
            "friendly_name": "客厅电视",
            "volume_level": 0.3,
            "source_list": ["TV", "HDMI1", "Bluetooth"],
            "source": "TV",
        },
    },
    "switch.kitchen_socket": {
        "state": "off",
        "attributes": {"friendly_name": "厨房插座"},
    },
    "sensor.living_temperature": {
        "state": "24.5",
        "attributes": {
            "friendly_name": "客厅温度",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
        },
    },
}

ALIASES = [
    AliasRecord("light.living_ceiling", "客厅灯", "客厅主灯", "living_room", "light", True),
    AliasRecord("light.bedroom_light", "卧室灯", "卧室灯", "bedroom", "light", True),
    AliasRecord("climate.living_ac", "客厅空调", "客厅空调", "living_room", "climate", True),
    AliasRecord("cover.living_curtain", "客厅窗帘", "客厅窗帘", "living_room", "cover", True),
    AliasRecord(
        "media_player.living_tv", "客厅电视", "客厅电视", "living_room", "media_player", True
    ),
    AliasRecord("switch.kitchen_socket", "厨房插座", "厨房插座", "kitchen", "switch", True),
    AliasRecord(
        "sensor.living_temperature", "客厅温度", "客厅温度", "living_room", "sensor", False
    ),
]


class FakeRegistry:
    areas = {
        "living_room": {"name": "客厅"},
        "bedroom": {"name": "卧室"},
        "kitchen": {"name": "厨房"},
    }
    floors = {}

    def area_of(self, entity_id: str) -> str | None:
        for record in ALIASES:
            if record.entity_id == entity_id:
                return record.area_id
        return None

    def area_name(self, area_id: str) -> str | None:
        return (self.areas.get(area_id) or {}).get("name")

    def floor_of(self, entity_id: str):
        return None


@pytest.fixture(scope="module")
def parser() -> VoiceParser:
    if not WORKBOOK.exists():
        pytest.skip("voice lexicon workbook missing")
    parsed = parse_workbook(WORKBOOK)
    lexicon = VoiceLexicon.from_entries(list(parsed.entries) + load_seed_entries())
    return VoiceParser(lexicon, ALIASES, FakeRegistry())


@pytest.fixture(scope="module")
def resolver() -> EntityResolver:
    return EntityResolver(ALIASES, FakeRegistry())


def parse(parser, resolver, text: str, *, states=None, **settings):
    merged = {"voice_max_group_actions": 50}
    merged.update(settings)
    return parser.parse(
        text,
        states=states or STATES,
        resolver=resolver,
        member=None,
        view_patterns=None,
        settings=merged,
    )


CONTROL_CASES = [
    ("打开客厅灯", "light.turn_on", {}),
    ("关闭卧室灯", "light.turn_off", {}),
    ("把客厅灯亮度调到50%", "light.turn_on", {"brightness_pct": 50}),
    ("客厅灯亮度调到百分之三十", "light.turn_on", {"brightness_pct": 30}),
    ("把客厅灯色温调到4000K", "light.turn_on", {"color_temp_kelvin": 4000.0}),
    ("客厅灯调到最亮", "light.turn_on", {"brightness_pct": 100}),
    ("客厅灯亮度调到最低", "light.turn_on", {"brightness_pct": 1}),
    ("客厅灯调到50", "light.turn_on", {"brightness_pct": 50}),
    ("客厅灯亮度50", "light.turn_on", {"brightness_pct": 50}),
    ("客厅灯颜色调成红色", "light.turn_on", {"rgb_color": [255, 0, 0]}),
    ("把客厅灯亮度降低百分之二十", "light.turn_on", {"brightness_pct": 30}),
    ("把客厅空调温度调到26度", "climate.set_temperature", {"temperature": 26.0}),
    ("空调温度调高2度", "climate.set_temperature", {"temperature": 28.0}),
    ("客厅空调温度调到最低", "climate.set_temperature", {"temperature": 16.0}),
    ("客厅空调制冷", "climate.set_hvac_mode", {"hvac_mode": "cool"}),
    ("客厅空调风速高档", "climate.set_fan_mode", {"fan_mode": "high"}),
    ("客厅空调风速调低一档", "climate.set_fan_mode", {"fan_mode": "low"}),
    ("把客厅窗帘打开", "cover.open_cover", {}),
    ("窗帘打开一半", "cover.set_cover_position", {"position": 50}),
    ("客厅窗帘行程调到30%", "cover.set_cover_position", {"position": 30}),
    ("客厅电视音量调到40%", "media_player.volume_set", {"volume_level": 0.4}),
    ("客厅电视切换蓝牙", "media_player.select_source", {"source": "Bluetooth"}),
    ("客厅电视音源切到hdmi1", "media_player.select_source", {"source": "HDMI1"}),
    ("打开厨房插座", "switch.turn_on", {}),
    ("全屋灯光调到50%", "light.turn_on", {"brightness_pct": 50}),
    ("关闭客厅所有灯", "light.turn_off", {}),
    ("全屋空调关闭", "climate.turn_off", {}),
]


@pytest.mark.parametrize(("phrase", "service", "expected"), CONTROL_CASES)
def test_golden_control(parser, resolver, phrase, service, expected) -> None:
    result = parse(parser, resolver, phrase)
    assert result is not None, phrase
    assert result.status == "ok", (phrase, result.status, result.reply)
    assert result.actions, phrase
    for action in result.actions:
        assert f"{action['domain']}.{action['service']}" == service, phrase
        for key, value in expected.items():
            assert action["data"].get(key) == value, (phrase, action)
    assert result.reply


QUERY_CASES = [
    ("查询客厅灯的状态", "status"),
    ("客厅灯现在多亮", "brightness"),
    ("查一下客厅灯亮度", "brightness"),
    ("客厅灯是开着的吗", "status"),
    ("卧室灯状态", "status"),
    ("查询客厅窗帘", "status"),
    ("客厅空调现在什么模式", "mode"),
    ("客厅空调温度多少", "targetTemp"),
    ("客厅温度多少", "value"),
    ("查询全屋灯光", "status"),
]


@pytest.mark.parametrize(("phrase", "function"), QUERY_CASES)
def test_golden_query(parser, resolver, phrase, function) -> None:
    result = parse(parser, resolver, phrase)
    assert result is not None, phrase
    assert result.status == "query", (phrase, result.status, result.reply)
    assert result.function == function, (phrase, result.function)
    assert result.reply


def test_relative_light_uses_current_value(parser, resolver) -> None:
    brighter = parse(parser, resolver, "把客厅灯调亮一点")
    darker = parse(parser, resolver, "客厅灯调暗一点点")
    assert brighter is not None and darker is not None
    assert brighter.actions[0]["data"]["brightness_pct"] > 50
    assert darker.actions[0]["data"]["brightness_pct"] < 50


def test_relative_volume_reports_new_percentage(parser, resolver) -> None:
    result = parse(parser, resolver, "客厅电视音量调低一点")
    assert result is not None and result.status == "ok"
    assert result.actions[0]["data"]["volume_level"] < 0.3
    assert result.reply == "客厅电视的音量已设为28%。"


def test_relative_enum_clips_at_edge(parser, resolver) -> None:
    states = copy.deepcopy(STATES)
    states["climate.living_ac"]["attributes"]["fan_mode"] = "low"
    result = parse(parser, resolver, "客厅空调风速调低一档", states=states)
    assert result is not None
    assert result.status == "reject"
    assert "最低" in result.reply
    assert not result.actions


def test_out_of_range_is_rejected_without_action(parser, resolver) -> None:
    result = parse(parser, resolver, "把卧室灯调到200%")
    assert result is not None
    assert result.status == "reject"
    assert "1%" in result.reply and "100%" in result.reply
    assert not result.actions


def test_group_control_counts_actions_and_targets(parser, resolver) -> None:
    result = parse(parser, resolver, "全屋灯光调到50%")
    assert result is not None and result.status == "ok"
    assert result.group is True
    assert result.device_type == "light"
    assert len(result.actions) == 2
    assert {action["entity_id"] for action in result.actions} == {
        "light.living_ceiling",
        "light.bedroom_light",
    }


def test_group_control_respects_action_limit(parser, resolver) -> None:
    result = parse(parser, resolver, "全屋灯光调到50%", voice_max_group_actions=1)
    assert result is not None
    assert result.status == "reject"
    assert "上限" in result.reply
    assert not result.actions


@pytest.mark.parametrize(
    "phrase",
    [
        "客厅调亮一点",
        "把客厅灯打开然后关闭卧室灯",
        "客厅灯",
    ],
)
def test_no_action_or_no_target_returns_none(parser, resolver, phrase) -> None:
    assert parse(parser, resolver, phrase) is None


def test_target_candidates_clarify(parser, resolver) -> None:
    states = copy.deepcopy(STATES)
    states["light.study_a"] = {
        "state": "on",
        "attributes": {"friendly_name": "书房灯"},
    }
    states["light.study_b"] = {
        "state": "off",
        "attributes": {"friendly_name": "书房灯"},
    }
    result = parse(parser, resolver, "打开书房灯", states=states)
    assert result is not None
    assert result.status == "clarify"
    assert len(result.candidates) == 2


def test_unsupported_function_lists_available(parser, resolver) -> None:
    result = parse(parser, resolver, "客厅电视亮度调到50%")
    assert result is not None
    assert result.status == "reject"
    assert "不支持" in result.reply or "没有" in result.reply


class FloorRegistry(FakeRegistry):
    areas = {
        "living_room": {"name": "客厅", "floor_id": "floor1"},
        "bedroom": {"name": "卧室", "floor_id": "floor2"},
        "kitchen": {"name": "厨房", "floor_id": "floor1"},
    }
    floors = {"floor1": {"name": "1楼"}, "floor2": {"name": "2楼"}}

    def floor_name(self, floor_id: str) -> str | None:
        return (self.floors.get(floor_id) or {}).get("name")

    def floor_of(self, entity_id: str):
        area_id = self.area_of(entity_id)
        return (self.areas.get(area_id) or {}).get("floor_id")


@pytest.fixture(scope="module")
def floor_env():
    if not WORKBOOK.exists():
        pytest.skip("voice lexicon workbook missing")
    parsed = parse_workbook(WORKBOOK)
    lexicon = VoiceLexicon.from_entries(list(parsed.entries) + load_seed_entries())
    registry = FloorRegistry()
    return VoiceParser(lexicon, ALIASES, registry), EntityResolver(ALIASES, registry)


def test_terminal_floor_limits_single_entity(floor_env) -> None:
    parser, resolver = floor_env
    result = parse(parser, resolver, "客厅灯打开", voice_terminal_floor_id="floor2")
    assert result is not None
    assert result.status == "reject"
    assert "2楼" in result.reply


def test_derived_core_control_ambiguous_clarifies(floor_env) -> None:
    """派生核心名命中多个候选 + 控制指令 → 澄清，不静默切换所有匹配设备。"""
    parser, resolver = floor_env
    states = copy.deepcopy(STATES)
    states["light.kitchen_spot_1"] = {
        "state": "on",
        "attributes": {"friendly_name": "筒灯灯"},
    }
    states["light.kitchen_spot_2"] = {
        "state": "off",
        "attributes": {"friendly_name": "筒灯灯"},
    }
    result = parse(parser, resolver, "关筒灯", states=states)
    assert result is not None
    assert result.status == "clarify"
    assert "多个设备" in result.reply
    assert not result.targets


def test_derived_core_query_aggregates(floor_env) -> None:
    """派生核心名命中多个候选 + 查询指令 → 聚合汇总，展示真实设备类型，不弹选项。"""
    parser, resolver = floor_env
    states = copy.deepcopy(STATES)
    states["light.kitchen_spot_1"] = {
        "state": "on",
        "attributes": {"friendly_name": "筒灯灯"},
    }
    states["light.kitchen_spot_2"] = {
        "state": "off",
        "attributes": {"friendly_name": "筒灯灯"},
    }
    result = parse(parser, resolver, "筒灯开着吗", states=states)
    assert result is not None
    assert result.status == "query"
    assert len(result.targets) == 2
    assert "灯光" in result.reply


def test_terminal_area_used_when_instruction_has_none(floor_env) -> None:
    parser, resolver = floor_env
    result = parse(parser, resolver, "打开灯光", voice_terminal_area_id="living_room")
    assert result is not None
    assert result.status == "ok"
    assert [target.entity_id for target in result.targets] == ["light.living_ceiling"]


def test_unknown_floor_rejects_with_pdf_wording(floor_env) -> None:
    parser, resolver = floor_env
    result = parse(parser, resolver, "3楼客厅灯打开")
    assert result is not None
    assert result.status == "reject"
    assert result.reply == "当前家庭没有3楼。"


def test_single_entity_floor_mismatch_rejects(floor_env) -> None:
    parser, resolver = floor_env
    result = parse(parser, resolver, "1楼卧室灯打开")
    assert result is not None
    assert result.status == "reject"
    assert "卧室灯" in result.reply


def test_absolute_value_ambiguity_returns_text_hint(floor_env) -> None:
    parser, resolver = floor_env
    states = copy.deepcopy(STATES)
    states["cover.living_curtain"] = {
        "state": "open",
        "attributes": {
            "friendly_name": "客厅窗帘",
            "current_position": 60,
            "current_tilt_position": 40,
        },
    }
    result = parse(parser, resolver, "客厅窗帘调到50", states=states)
    assert result is not None
    assert result.status == "reject"
    assert result.reply.startswith("推断不出唯一的控制指令")
    assert "功能名称" in result.reply


def test_extreme_ambiguity_lists_function_options(floor_env) -> None:
    parser, resolver = floor_env
    states = copy.deepcopy(STATES)
    states["cover.living_curtain"] = {
        "state": "open",
        "attributes": {
            "friendly_name": "客厅窗帘",
            "current_position": 60,
            "current_tilt_position": 40,
        },
    }
    result = parse(parser, resolver, "客厅窗帘调到最大", states=states)
    assert result is not None
    assert result.status == "clarify"
    assert set(result.slots.function_options) == {"angle", "course"}
    assert "请选择" in result.reply
    assert "角度" in result.reply and "行程" in result.reply


def test_build_with_function_resumes_after_clarify(floor_env) -> None:
    parser, resolver = floor_env
    states = copy.deepcopy(STATES)
    states["cover.living_curtain"] = {
        "state": "open",
        "attributes": {
            "friendly_name": "客厅窗帘",
            "current_position": 60,
            "current_tilt_position": 40,
        },
    }
    first = parse(parser, resolver, "客厅窗帘调到最大", states=states)
    assert first is not None and first.status == "clarify"
    resumed = parser.build_with_function(first.slots, "angle", states=states)
    assert resumed.status == "ok"
    assert resumed.actions[0]["data"]["tilt_position"] == 100


def test_custom_group_list_overrides_inference(floor_env) -> None:
    parser, resolver = floor_env
    custom = VoiceParser(
        parser._lexicon,
        ALIASES,
        FloorRegistry(),
        group_lists=[
            {
                "device_type": "light",
                "floor_id": "",
                "area_id": "",
                "entity_ids": ["light.living_ceiling"],
            }
        ],
    )
    result = parse(custom, resolver, "全屋灯光调到50%")
    assert result is not None
    assert result.status == "ok"
    assert [target.entity_id for target in result.targets] == ["light.living_ceiling"]


class _FakeMember:
    def __init__(self, role: str = "member") -> None:
        self.role = role


def _parse_as(parser, resolver, text: str, *, view_patterns, role="member", states=None):
    return parser.parse(
        text,
        states=states or STATES,
        resolver=resolver,
        member=_FakeMember(role),
        view_patterns=view_patterns,
        settings={"voice_max_group_actions": 50},
    )


def test_no_permission_type_reply_points_at_member_permissions(floor_env) -> None:
    """普通成员没配权限时，不能说「没有这类设备」，要提示去授权。"""
    parser, resolver = floor_env

    result = _parse_as(parser, resolver, "关筒灯", view_patterns=[])
    assert result is not None and result.status == "reject"
    assert "没有找到" not in result.reply
    assert "权限" in result.reply


def test_no_permission_entity_reply_points_at_member_permissions(floor_env) -> None:
    parser, resolver = floor_env

    result = _parse_as(parser, resolver, "关客厅灯", view_patterns=[])
    assert result is not None and result.status == "reject"
    assert "没有找到" not in result.reply
    assert "权限" in result.reply


def test_granted_permission_still_executes(floor_env) -> None:
    parser, resolver = floor_env

    result = _parse_as(parser, resolver, "关客厅灯", view_patterns=["light.*"])
    assert result is not None and result.status == "ok"
    assert result.actions[0]["entity_id"] == "light.living_ceiling"


def test_missing_device_keeps_original_wording(floor_env) -> None:
    """权限齐全、家里确实没有这类设备时，仍用原来的「没有找到」措辞。"""
    parser, resolver = floor_env

    result = _parse_as(parser, resolver, "打开加湿器", view_patterns=["light.*"])
    assert result is not None and result.status == "reject"
    assert "没有找到" in result.reply
    assert "权限" not in result.reply


def test_admin_is_unaffected(floor_env) -> None:
    parser, resolver = floor_env

    result = _parse_as(parser, resolver, "关客厅灯", view_patterns=None, role="admin")
    assert result is not None and result.status == "ok"
