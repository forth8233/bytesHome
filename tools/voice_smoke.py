"""语音解析器冒烟脚本：离线加载词库，用合成设备状态跑黄金语料。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.fastpath.entities import AliasRecord, EntityResolver  # noqa: E402
from app.fastpath.voice import VoiceLexicon, VoiceParser  # noqa: E402
from app.fastpath.voice_import import load_seed_entries  # noqa: E402
from app.fastpath.voice_lexicon import parse_workbook  # noqa: E402

WORKBOOK = ROOT / "智能家居语音技能" / "语音控制技能_系统词库_汇总版20250208170728.xlsx"

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
        "last_updated": "2026-11-05T10:00:00+00:00",
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
    AliasRecord("media_player.living_tv", "客厅电视", "客厅电视", "living_room", "media_player", True),
    AliasRecord("switch.kitchen_socket", "厨房插座", "厨房插座", "kitchen", "switch", True),
    AliasRecord("sensor.living_temperature", "客厅温度", "客厅温度", "living_room", "sensor", False),
]


class FakeRegistry:
    areas = {"living_room": {"name": "客厅"}, "bedroom": {"name": "卧室"}, "kitchen": {"name": "厨房"}}
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


PHRASES = [
    "打开客厅灯",
    "关闭卧室灯",
    "把客厅灯亮度调到50%",
    "客厅灯亮度调到百分之三十",
    "把客厅灯调亮一点",
    "客厅灯调暗一点点",
    "把客厅灯色温调到4000K",
    "客厅灯调到最亮",
    "查询客厅灯的状态",
    "客厅灯现在多亮",
    "把客厅空调温度调到26度",
    "空调温度调高2度",
    "客厅空调制冷",
    "客厅空调风速高档",
    "把客厅窗帘打开",
    "窗帘打开一半",
    "客厅电视音量调到40%",
    "客厅电视音量调低一点",
    "客厅电视切换蓝牙",
    "打开厨房插座",
    "查询全屋灯光",
    "关闭客厅所有灯",
    "客厅温度多少",
    "客厅调亮一点",
    "把客厅灯打开然后关闭卧室灯",
    "客厅灯",
    "空调调到26度",
    "全屋灯光调到50%",
    "把卧室灯调到200%",
    "卧室灯调暗",
    "客厅灯亮度调到最低",
    "客厅空调温度调到最低",
    "查一下客厅灯亮度",
    "客厅窗帘行程调到30%",
    "客厅电视音源切到hdmi1",
    "客厅灯颜色调成红色",
    "全屋空调关闭",
    "客厅空调温度多少",
    "卧室灯状态",
    "客厅灯调到50",
    "客厅灯亮度50",
    "客厅灯最亮",
    "把客厅灯亮度降低百分之二十",
    "窗帘关一半",
    "客厅空调风速调低一档",
    "客厅灯是开着的吗",
    "查询客厅窗帘",
    "客厅空调现在什么模式",
]


def main() -> None:
    parsed = parse_workbook(WORKBOOK)
    entries = list(parsed.entries) + load_seed_entries()
    lexicon = VoiceLexicon.from_entries(entries)
    parser = VoiceParser(lexicon, ALIASES, FakeRegistry())
    resolver = EntityResolver(ALIASES, FakeRegistry())
    print(
        json.dumps(
            {
                "entries": len(entries),
                "functions": len(lexicon.functions),
                "enums": len(lexicon.enums),
                "units": len(lexicon.units),
                "bases": [len(lexicon.increase_bases), len(lexicon.decrease_bases)],
                "stems": len(lexicon.stems),
                "values": len(lexicon.values),
            },
            ensure_ascii=False,
        )
    )
    for phrase in PHRASES:
        result = parser.parse(
            phrase,
            states=STATES,
            resolver=resolver,
            member=None,
            view_patterns=None,
            settings={"voice_max_group_actions": 50},
        )
        if result is None:
            print(f"[NONE ] {phrase}")
            continue
        print(f"[{result.status.upper():5}] {phrase} -> {result.reply}")
        if result.actions:
            print("        actions:", json.dumps(result.actions, ensure_ascii=False))


if __name__ == "__main__":
    main()
