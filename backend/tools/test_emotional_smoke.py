# -*- coding: utf-8 -*-
"""情感兜底单元冒烟测试（独立于 pytest，直接 python 运行）。"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.fastpath import emotional as E
from app.fastpath.entities import EntityRef


class FakeResolver:
    def resolve(self, target, domain, states, explicit_all=False,
                view_patterns=None, want_control=True, member=None):
        refs = {
            "climate": [EntityRef("climate.living_room", "客厅空调", "area_living", "climate", True)],
            "light": [EntityRef("light.living", "客厅灯", "area_living", "light", True)],
            "cover": [EntityRef("cover.living", "客厅窗帘", "area_living", "cover", True)],
            "humidifier": [EntityRef("humidifier.living", "加湿器", "area_living", "humidifier", True)],
            "fan": [EntityRef("fan.bath", "排气扇", "area_bath", "fan", True)],
            "switch": [EntityRef("switch.purifier", "空气净化器", "area_living", "switch", True)],
            "sensor": [EntityRef("sensor.temp", "温度计", "area_living", "sensor", False)],
        }.get(domain, [])
        return SimpleNamespace(entities=refs)


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


states = {
    "sensor.temp": {"state": "18.0", "attributes": {"device_class": "temperature"}},
    "climate.living_room": {"state": "heat", "attributes": {"friendly_name": "客厅空调"}},
    "light.living": {"state": "on", "attributes": {"friendly_name": "客厅灯"}},
}
res = FakeResolver()
res.climate = None  # noqa

cases = ["有点冷", "好热", "好闷", "太暗", "太亮了", "好干", "好潮", "好臭"]
for word in cases:
    f = E.match_emotional(word)
    assert f is not None, f"should match: {word}"
    acts = E.build_emotional_actions(f, states, res, None)
    print(f"{word!r:6} -> {len(acts)} act: "
          f"{[ (a['domain'], a['service'], a.get('data')) for a in acts ]}")

# 白天 vs 夜间 dim 行为
from datetime import datetime
f = E.match_emotional("太暗")
day = E.build_emotional_actions(f, states, res, None, now=datetime(2026, 11, 9, 12, 0))
night = E.build_emotional_actions(f, states, res, None, now=datetime(2026, 11, 9, 22, 0))
print("dim 白天:", [(a['domain'], a['service']) for a in day])
print("dim 夜间:", [(a['domain'], a['service']) for a in night])
assert day and day[0]["service"] == "open_cover", "白天应开窗帘"
assert night and night[0]["domain"] == "light", "夜间应开灯"

print("COLD winter high-band short:", E._temperature_target("cold", "winter", 30.0))
print("ALL EMOTIONAL TESTS OK")