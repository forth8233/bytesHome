from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.fastpath.capabilities import device_type_for
from app.fastpath.normalize import normalize

# 情感类控制：把用户对室内环境的主观感受词，按设计文档映射成确定性设备动作。
# 仅处理 MVP 明确词；拿不准的感受交给慢路径 LLM 增强。所有动作仍过 ActionGuard。

# 明确词的命中即可快速接管；裸感受词（"冷""热""暗"）本身歧义大，不触发，留给慢路径。
_EMOTIONAL_WORDS: dict[str, tuple[str, ...]] = {
    "cold": (
        "有点冷", "好冷", "太冷了", "冷了", "有些冷", "有点凉", "好凉",
        "冻", "冻死了",
    ),
    "hot": (
        "有点热", "好热", "太热了", "热了", "有些热", "有点烫", "好烫",
        "烫", "烫死了", "闷热",
    ),
    "stuffy": ("好闷", "有点闷", "闷", "闷得慌", "透不过气", "空气不流通"),
    "dim": ("太暗", "有点暗", "好暗", "暗", "黑", "太黑了", "看不清", "黑漆漆"),
    "bright": (
        "太亮", "有点亮", "好亮", "亮", "刺眼", "晃眼", "好晃眼", "亮瞎了",
    ),
    "dry": ("好干", "有点干", "干燥", "好干燥", "干", "嗓子干"),
    "humid": ("好潮", "好潮湿", "有点潮", "潮", "湿", "好湿", "潮气", "湿气", "潮乎乎"),
    "stale": ("好臭", "有异味", "空气不好", "有股味道", "臭", "有味", "不新鲜"),
}

_NEGATIVE_WORDS = ("不要", "别", "取消", "不用", "停止开启")

# 疑问/反问措辞不应被情感兜底接管，避免顶掉正常查询。
# 只匹配确切的疑问成分（语气词 + 疑问代词），不包含裸"干/嘛"这类歧义单字。
_QUESTION_RE = re.compile(
    r"(吗|呢|啊|呀|怎么|为什么|为啥|哪个|哪些|多少|是不是|要不要|能不能|有没有)"
)

# 含这些词的表达更偏向场景/动作，不是纯粹的"现状感受"，不接管
_ACTION_HINT_WORDS = ("开", "关", "调", "设", "打开", "关闭", "调到", "设为")
# 房间/位置词会改变设备选择作用域，本版快路径只处理无指定房间的感受
_ROOM_WORDS = (
    "客厅", "卧室", "书房", "厨房", "餐厅", "卫生间", "浴室", "洗手间", "阳台", "车库",
    "主卧", "次卧", "婴儿房", "儿童房", "衣帽间", "储藏室", "门厅", "玄关", "走廊",
    "楼上", "楼下", "一层", "二层", "三楼", "一楼", "二楼", "全屋", "家里", "整个家",
)

# 说明：_QUESTION_RE 与 _ROOM_WORDS 等启发式规则用于兜底决策，属保守策略。
# 灵敏度与误接管率已与产品确认（2026-11 迭代），如需调整查找此处。

_SEASON_TARGETS = {
    # season -> (kind, (加/减幅度、下限、上限) 或 固定默认温度)
    "cold": {
        "winter": ("delta", +3.0, 22.0, 26.0),
        "summer": ("delta", +2.0, 24.0, 26.0),
        "spring_autumn": ("fixed", 25.0, None, None),
    },
    "hot": {
        "winter": ("delta", -2.0, 18.0, 22.0),
        "summer": ("delta", -3.0, 24.0, 26.0),
        "spring_autumn": ("fixed", 24.0, None, None),
    },
}

# cover 的"床帘/窗帘关闭"相关词由 device_type_for 按 label 判定，这里用名称兜底匹配
_CURTAIN_KW = ("帘", "幕", "百叶")  # noqa: F841 - 名称兜底对照，供后续扩展


@dataclass(frozen=True)
class EmotionalFeeling:
    kind: str  # cold / hot / stuffy / dim / bright / dry / humid / stale
    matched_word: str


def current_season(now: datetime | None = None) -> str:
    month = (now or datetime.now()).month
    if month in (4, 5):
        return "spring_autumn"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10):
        return "spring_autumn"
    return "winter"


def match_emotional(text: str) -> EmotionalFeeling | None:
    """识别明确的 MVP 感受词；歧义、否定、疑问或含动作/房间词的表达一律不触发。"""
    normalized = normalize(text)
    if not normalized:
        return None
    if any(word in normalized for word in _NEGATIVE_WORDS):
        return None
    if _QUESTION_RE.search(normalized):
        return None
    if any(word in normalized for word in _ACTION_HINT_WORDS):
        return None
    if any(word in normalized for word in _ROOM_WORDS):
        return None

    best: tuple[int, str, str] | None = None
    for kind, words in _EMOTIONAL_WORDS.items():
        for word in words:
            if word in normalized:
                length = len(word)
                if best is None or length > best[0]:
                    best = (length, kind, word)
    if best is None:
        return None
    return EmotionalFeeling(kind=best[1], matched_word=best[2])


def _resolve_domain_all(
    resolver, domain: str, states, view_patterns, *, want_control: bool = True
) -> list:
    resolution = resolver.resolve(
        "", domain, states, explicit_all=True, view_patterns=view_patterns,
        want_control=want_control, member=None,
    )
    return list(resolution.entities)


def _pick_entity(
    refs, states, *, preferred_device_types: tuple[str, ...] = ()
) -> object | None:
    """多设备时按 device_type 偏好挑选单个目标；无偏好时返回全部可用的。"""
    if not refs:
        return None
    by_type: dict[str, list] = {}
    for ref in refs:
        state = states.get(ref.entity_id)
        dtype = device_type_for(ref.entity_id, state, ref.name)
        by_type.setdefault(dtype, []).append(ref)
    for dtype in preferred_device_types:
        if by_type.get(dtype):
            return by_type[dtype]
    return None


def _temperature_target(kind: str, season: str, ambient: float | None) -> float | None:
    rule = _SEASON_TARGETS.get(kind, {}).get(season)
    if rule is None:
        return None
    kind_, a, lo, hi = rule
    if kind_ == "fixed":
        return a
    if ambient is None:
        # 无环境温度时按季节给个安全默认，避免空推断
        return {"cold": 24.0 if season == "summer" else 25.0,
                "hot": 24.0 if season == "summer" else 22.0}.get(kind)
    target = ambient + a
    if lo is not None:
        target = max(lo, target)
    if hi is not None:
        target = min(hi, target)
    return round(target, 1)


def _curtain_refs(refs) -> list:
    return [ref for ref in refs if "帘" in ref.name or "幕" in ref.name or "百叶" in ref.name]


def build_emotional_actions(
    feeling: EmotionalFeeling, states, resolver, view_patterns, *, now: datetime | None = None
) -> list[dict]:
    """按设计文档映射表产出动作；找不到可用设备返回空列表，自然回退慢路径。"""
    season = current_season(now)
    kind = feeling.kind
    actions: list[dict] = []

    # 环境温度：不区分房间时只取首个温度传感器读数（保守，不跨域猜测）
    ambient: float | None = None
    for ref in _resolve_domain_all(resolver, "sensor", states, view_patterns, want_control=False):
        if getattr(ref, "entity_id", "").startswith("sensor.") is False:
            continue
        state = states.get(ref.entity_id)
        attrs = (state or {}).get("attributes") or {}
        if attrs.get("device_class") == "temperature":
            try:
                ambient = float(state.get("state"))
            except (TypeError, ValueError):
                ambient = None
            if ambient is not None:
                break

    if kind in ("cold", "hot"):
        # 地暖/空调/电暖器均属 climate/switch；冷→地暖>空调制热；热→空调制冷
        heaters = _resolve_domain_all(resolver, "climate", states, view_patterns)
        switches = _resolve_domain_all(resolver, "switch", states, view_patterns)
        if kind == "cold":
            climate_ok = [_dev for _dev in heaters if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) in ("heating", "freshAir")] or heaters
            # 有地暖优先；否则空调制热
            preferred = _pick_entity(climate_ok, states, preferred_device_types=("heating",))
            if not preferred:
                preferred = climate_ok
            if preferred:
                for ref in preferred[:1]:
                    target = _temperature_target("cold", season, ambient)
                    actions.append({
                        "domain": "climate", "service": "set_temperature",
                        "entity_id": ref.entity_id, "data": {"temperature": target},
                    })
                    actions.append({
                        "domain": "climate", "service": "set_hvac_mode",
                        "entity_id": ref.entity_id, "data": {"hvac_mode": "heat"},
                    })
            else:
                # 无空调/地暖：电暖器（switch）直接开启
                if switches:
                    actions.append({
                        "domain": "switch", "service": "turn_on",
                        "entity_id": switches[0].entity_id, "data": {},
                    })
        else:  # hot
            preferred = _pick_entity(heaters, states, preferred_device_types=("AC",))
            if not preferred:
                preferred = heaters
            if preferred:
                for ref in preferred[:1]:
                    target = _temperature_target("hot", season, ambient)
                    actions.append({
                        "domain": "climate", "service": "set_temperature",
                        "entity_id": ref.entity_id, "data": {"temperature": target},
                    })
                    actions.append({
                        "domain": "climate", "service": "set_hvac_mode",
                        "entity_id": ref.entity_id, "data": {"hvac_mode": "cool"},
                    })
            else:
                fans = _resolve_domain_all(resolver, "fan", states, view_patterns)
                if fans:
                    actions.append({
                        "domain": "fan", "service": "turn_on",
                        "entity_id": fans[0].entity_id, "data": {},
                    })

    elif kind == "stuffy":
        # 新风 > 排气扇 > 加大空调风速
        new_air = _resolve_domain_all(resolver, "climate", states, view_patterns)
        new_air = [
            _dev for _dev in new_air
            if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) == "freshAir"
        ]
        exhaust = _resolve_domain_all(resolver, "fan", states, view_patterns)
        if new_air:
            actions.append({
                "domain": "climate", "service": "set_hvac_mode",
                "entity_id": new_air[0].entity_id, "data": {"hvac_mode": "fan_only"},
            })
        elif exhaust:
            actions.append({
                "domain": "fan", "service": "turn_on",
                "entity_id": exhaust[0].entity_id, "data": {},
            })
        else:
            ac = _resolve_domain_all(resolver, "climate", states, view_patterns)
            ac = [
                _dev for _dev in ac
                if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) == "AC"
            ]
            if ac:
                actions.append({
                    "domain": "climate", "service": "set_fan_mode",
                    "entity_id": ac[0].entity_id, "data": {"fan_mode": "High"},
                })

    elif kind == "dim":
        # 白天优先开窗帘，夜晚/默认开灯
        hour = (now or datetime.now()).hour if now else datetime.now().hour
        covers = _resolve_domain_all(resolver, "cover", states, view_patterns)
        curtains = _curtain_refs(covers)
        if curtains and hour and 6 <= hour < 18:
            actions.append({
                "domain": "cover", "service": "open_cover",
                "entity_id": curtains[0].entity_id, "data": {},
            })
        else:
            lights = _resolve_domain_all(resolver, "light", states, view_patterns)
            if lights:
                for ref in lights[:3]:
                    actions.append({
                        "domain": "light", "service": "turn_on",
                        "entity_id": ref.entity_id, "data": {"brightness_pct": 80},
                    })

    elif kind == "bright":
        covers = _resolve_domain_all(resolver, "cover", states, view_patterns)
        curtains = _curtain_refs(covers)
        lights = _resolve_domain_all(resolver, "light", states, view_patterns)
        if curtains:
            actions.append({
                "domain": "cover", "service": "close_cover",
                "entity_id": curtains[0].entity_id, "data": {},
            })
        elif lights:
            for ref in lights[:3]:
                actions.append({
                    "domain": "light", "service": "turn_on",
                    "entity_id": ref.entity_id, "data": {"brightness_pct": 40},
                })

    elif kind == "dry":
        hums = _resolve_domain_all(resolver, "humidifier", states, view_patterns)
        if hums:
            actions.append({
                "domain": "humidifier", "service": "turn_on",
                "entity_id": hums[0].entity_id, "data": {},
            })

    elif kind == "humid":
        # 除湿器 > 空调除湿
        hums = _resolve_domain_all(resolver, "humidifier", states, view_patterns)
        dehumid = [
            _dev for _dev in hums
            if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) == "dehumidifier"
        ]
        if dehumid:
            actions.append({
                "domain": "humidifier", "service": "turn_on",
                "entity_id": dehumid[0].entity_id, "data": {},
            })
        else:
            ac = _resolve_domain_all(resolver, "climate", states, view_patterns)
            ac = [
                _dev for _dev in ac
                if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) == "AC"
            ]
            if ac:
                actions.append({
                    "domain": "climate", "service": "set_hvac_mode",
                    "entity_id": ac[0].entity_id, "data": {"hvac_mode": "dry"},
                })

    elif kind == "stale":
        # 排气扇 > 空气净化器 > 新风
        exhaust = _resolve_domain_all(resolver, "fan", states, view_patterns)
        purifiers = _resolve_domain_all(resolver, "switch", states, view_patterns)
        if exhaust:
            actions.append({
                "domain": "fan", "service": "turn_on",
                "entity_id": exhaust[0].entity_id, "data": {},
            })
        elif purifiers:
            actions.append({
                "domain": "switch", "service": "turn_on",
                "entity_id": purifiers[0].entity_id, "data": {},
            })
        else:
            new_air = [
                _dev for _dev in _resolve_domain_all(resolver, "climate", states, view_patterns)
                if device_type_for(_dev.entity_id, states.get(_dev.entity_id), _dev.name) == "freshAir"
            ]
            if new_air:
                actions.append({
                    "domain": "climate", "service": "set_hvac_mode",
                    "entity_id": new_air[0].entity_id, "data": {"hvac_mode": "fan_only"},
                })

    return actions


def emotional_hint(feeling: EmotionalFeeling, actions: list[dict], states) -> dict[str, Any]:
    """给引擎 /test 面板与回复提供的可读摘要。"""
    return {
        "matched_word": feeling.matched_word,
        "kind": feeling.kind,
        "action_count": len(actions),
        "targets": [
            {
                "domain": a.get("domain"),
                "service": a.get("service"),
                "entity_id": a.get("entity_id"),
                "data": a.get("data"),
            }
            for a in actions
        ],
    }