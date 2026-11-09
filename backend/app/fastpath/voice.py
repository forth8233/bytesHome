from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.fastpath.capabilities import (
    DEFAULT_STEPS,
    DEVICE_TYPE_LABELS,
    FUNCTION_LABELS,
    DeviceCapability,
    FunctionSpec,
    build_capability,
)
from app.fastpath.entities import AliasRecord, EntityRef, EntityResolver
from app.fastpath.grammar import DOMAIN_WORDS
from app.fastpath.normalize import normalize_voice
from app.fastpath.voice_lexicon import LexiconEntry

MULTI_WORDS = ("然后", "同时", "顺便", "并且")
MAX_TEXT_LENGTH = 64

DOMAIN_TYPE_HINTS = {
    "灯光": "light",
    "灯": "light",
    "空调": "AC",
    "地暖": "heating",
    "暖气": "heating",
    "新风": "freshAir",
    "换气": "freshAir",
    "窗帘": "curtain",
    "卷帘": "curtain",
    "百叶窗": "curtain",
    "窗户": "window",
    "电视": "tv",
    "音箱": "media",
    "音响": "media",
    "音乐": "media",
    "风扇": "freshAir",
    "电扇": "freshAir",
    "加湿器": "humidifier",
    "加湿": "humidifier",
    "除湿机": "dehumidifier",
    "门锁": "smartLock",
    "锁": "smartLock",
    "插座": "switch",
    "开关": "switch",
    "扫地机": "cleaning",
    "扫地机器人": "cleaning",
}

DOMAIN_PREFERENCE = {
    "light": ("brightness", "colorTemp", "color", "status"),
    "climate": ("targetTemp", "mode", "speed", "status"),
    "water_heater": ("targetTemp", "mode", "status"),
    "cover": ("course", "angle", "status"),
    "media_player": ("volume", "source", "status"),
    "fan": ("speed", "mode", "status"),
    "humidifier": ("targetHumidity", "mode", "status"),
    "switch": ("systemSwitch", "status"),
    "lock": ("status",),
    "vacuum": ("status",),
}

DEFAULT_FUNCTIONS = {
    "relative": (
        "brightness",
        "course",
        "volume",
        "targetTemp",
        "speed",
        "angle",
        "targetHumidity",
        "colorTemp",
    ),
    "state": ("systemSwitch", "status"),
    "query": ("status", "value", "fault"),
}

MODE_SYNONYMS = {
    "cool": ("cool", "制冷"),
    "heat": ("heat", "制热"),
    "dehum": ("dry", "dehumidify", "除湿"),
    "vent": ("fan_only", "fan", "vent", "送风", "通风"),
    "auto": ("auto", "自动"),
    "sleepmode": ("sleep", "睡眠"),
    "energysaving": ("eco", "energy_saving", "节能"),
    "refreshing": ("refreshing", "清爽"),
    "freshair": ("fresh_air", "新风"),
    "freshairdehum": ("fresh_air_dry", "新风除湿"),
    "freshairvent": ("fresh_air_vent", "新风通风"),
}

SOURCE_SYNONYMS = {
    "aux": ("aux", "aux输入"),
    "mp3": ("mp3", "mp3音乐"),
    "local": ("local", "本地音乐", "本地"),
    "netmusic": ("net_music", "网络音乐", "网络"),
    "bluetooth": ("bluetooth", "蓝牙"),
    "tv": ("tv", "电视"),
}

SPEED_ALIASES = {
    "speedoff": 0,
    "speed1": 1,
    "speed2": 2,
    "speed3": 3,
    "speed4": 4,
    "speed5": 5,
}

SPEED_ENUM_FALLBACK = {
    "高档": ("high", "高档"),
    "高速": ("high", "高速"),
    "高风": ("high", "高风"),
    "强风": ("high", "强风"),
    "中档": ("medium", "中档"),
    "中速": ("medium", "中速"),
    "中风": ("medium", "中风"),
    "低档": ("low", "低档"),
    "低速": ("low", "低速"),
    "低风": ("low", "低风"),
    "弱风": ("low", "弱风"),
    "微风": ("low", "微风"),
    "一档": ("speed1", "一档"),
    "二档": ("speed2", "二档"),
    "三档": ("speed3", "三档"),
    "四档": ("speed4", "四档"),
    "五档": ("speed5", "五档"),
}

SPEED_LEVEL_SYNONYMS = {
    "high": ("high", "高", "高档", "高速"),
    "medium": ("medium", "mid", "中", "中档", "中速"),
    "low": ("low", "低", "低档", "低速"),
}

FUNCTION_FALLBACK = {
    "颜色": "color",
    "色彩": "color",
    "灯光颜色": "color",
}

STATE_WORDS = {
    "on": ("on", "打开", "开启", "开"),
    "off": ("off", "关闭", "关掉", "关"),
    "open": ("open", "打开"),
    "closed": ("closed", "关闭"),
    "locked": ("locked", "上锁"),
    "unlocked": ("unlocked", "解锁"),
}

VALUE_LABELS = {
    "cool": "制冷",
    "heat": "制热",
    "dry": "除湿",
    "fan_only": "送风",
    "auto": "自动",
    "off": "关闭",
    "low": "低档",
    "medium": "中档",
    "high": "高档",
}

ALLOWED_RESIDUE = (
    "帮我",
    "帮忙",
    "麻烦",
    "给我",
    "把",
    "将",
    "请",
    "我",
    "的",
    "里",
    "在",
    "到",
    "为",
    "成",
    "设置",
    "设",
    "置",
    "调整",
    "调",
    "弄",
    "搞",
    "整",
    "打",
    "换",
    "切",
    "至",
    "一下",
    "下",
    "个",
    "那",
    "这",
    "了",
    "吧",
    "呢",
    "啊",
    "呀",
    "就",
    "要",
    "想",
    "能",
    "可以",
    "现在",
    "当前",
    "先",
    "再",
    "一次",
    "左右",
    "大概",
    "大约",
    "约",
    "功能",
    "设备",
    "家里",
    "全屋",
    "所有",
    "全部",
    "每个",
    "都",
    "给",
    "让",
    "多",
    "多少",
    "几",
    "是",
    "着",
    "吗",
    "么",
    "什么",
    "有",
    "哪些",
    "哪",
    "些",
    "有没有",
    "亮",
    "亮着",
    "暗",
)

_SCENE_TARGET_DOMAINS = ("scene", "script", "automation")
_SCENE_VERBS = {
    "执行": "execute",
    "运行": "execute",
    "激活": "execute",
    "启动": "execute",
    "开始": "execute",
    "调用": "execute",
    "进入": "execute",
    "触发": "trigger",
    "开启": "on",
    "打开": "on",
    "启用": "on",
    "关闭": "stop",
    "停止": "stop",
}
_SCENE_NOUNS = ("场景", "脚本", "自动化")
_SCENE_OP_WORDS = {
    "execute": "执行",
    "trigger": "触发",
    "stop": "停止",
    "on": "开启",
}

QUERY_HINTS = (
    "多少",
    "几度",
    "多大",
    "几档",
    "几",
    "多亮",
    "多暗",
    "多热",
    "多冷",
    "什么",
    "是开着",
    "是关着",
    "开着吗",
    "关了吗",
    "哪些",
    "有没有",
    "亮着",
    "亮着吗",
    "亮着没",
)

# 设备名尾部常被 HA 追加一个域词（“筒灯”被记成“筒灯  灯”），去掉尾部域词再登记一个核心名。
_TRAILING_DOMAIN_WORDS = tuple(
    sorted({word for _, words in DOMAIN_WORDS for word in words}, key=len, reverse=True)
)
_DOMAIN_WORD_SET = frozenset(_TRAILING_DOMAIN_WORDS)
_TYPE_KEYS = frozenset(normalize_voice(key) for key in DOMAIN_TYPE_HINTS)


def _derived_core(friendly: str) -> str:
    """去掉规范化友好名尾部「一个」域词，得到派生核心名（如「筒灯灯」→「筒灯」）。

    HA 常给设备友好名再拼一个域词（「筒灯」被记成「筒灯灯」），指令里只说到核心词时，
    只有这一个派生键能命中。只剥尾部一个域词，避免把核心词的语义剥掉；剥不出结果或剥
    完长度不足 2 个字符（退化为单字，容易误伤）时返回空串表示不登记派生键。
    """
    core = friendly
    for word in _TRAILING_DOMAIN_WORDS:
        if core.endswith(word):
            core = core[: -len(word)]
            break
    if core == friendly or len(core) < 2 or core in ("", "的", "里"):
        return ""
    return core

QUERY_FUNCTION_HINTS = {
    "多亮": "brightness",
    "多暗": "brightness",
    "多热": "targetTemp",
    "多冷": "targetTemp",
    "几档": "speed",
    "什么模式": "mode",
}

LITTLE_STEP_RATIO = 0.2
LITTLE_ABSOLUTE = {
    "targetTemp": 0.5,
    "speed": 1.0,
    "targetHumidity": 1.0,
}

UNIT_COMPATIBLE = {
    "%": ("%", "百分之"),
    "°": ("°", "度", "摄氏度"),
    "°C": ("°", "度", "摄氏度"),
    "°F": ("°", "度", "华氏度"),
    "档": ("档",),
    "K": ("k", "开尔文"),
}

_NUMBER_TEXT_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class VoiceLexicon:
    functions: dict[str, str] = field(default_factory=dict)
    enums: dict[str, tuple[str, tuple[str, ...], str]] = field(default_factory=dict)
    units: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    max_words: dict[str, tuple[str, ...]] = field(default_factory=dict)
    min_words: dict[str, tuple[str, ...]] = field(default_factory=dict)
    device_types: dict[str, str] = field(default_factory=dict)
    areas: dict[str, str] = field(default_factory=dict)
    floors: dict[str, str] = field(default_factory=dict)
    device_names: dict[str, str] = field(default_factory=dict)
    query_words: set[str] = field(default_factory=set)
    little_words: set[str] = field(default_factory=set)
    increase_bases: dict[str, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    decrease_bases: dict[str, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    stems: dict[str, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    values: dict[str, tuple[float, str]] = field(default_factory=dict)

    @classmethod
    def from_entries(cls, entries: Iterable[LexiconEntry]) -> "VoiceLexicon":
        lexicon = cls()
        for entry in entries:
            key = normalize_voice(entry.alias)
            if not key:
                continue
            name = entry.lexicon
            if name == "function":
                lexicon.functions.setdefault(key, entry.canonical)
            elif name == "enum":
                functions = tuple(entry.functions)
                if set(functions) & {"red", "green", "blue"}:
                    functions += ("color",)
                lexicon.enums.setdefault(
                    key, (entry.canonical, functions, entry.term)
                )
            elif name == "unit":
                lexicon.units.setdefault(
                    key, (entry.canonical, tuple(entry.functions))
                )
            elif name == "max":
                lexicon.max_words.setdefault(key, tuple(entry.functions))
            elif name == "min":
                lexicon.min_words.setdefault(key, tuple(entry.functions))
            elif name == "deviceType":
                lexicon.device_types.setdefault(key, entry.canonical)
            elif name == "areaName":
                lexicon.areas.setdefault(key, entry.canonical)
            elif name == "floorName":
                lexicon.floors.setdefault(key, entry.canonical)
            elif name == "deviceName":
                lexicon.device_names.setdefault(key, entry.canonical)
            elif name == "query":
                lexicon.query_words.add(key)
            elif name == "little":
                lexicon.little_words.add(key)
            elif name in ("increase", "decrease"):
                direction = 1 if name == "increase" else -1
                if entry.numeric_value is None:
                    target = (
                        lexicon.increase_bases
                        if direction == 1
                        else lexicon.decrease_bases
                    )
                    target.setdefault(key, (direction, tuple(entry.functions)))
                    if entry.source == "pdf":
                        lexicon.stems.setdefault(
                            key, (direction, tuple(entry.functions))
                        )
            elif name == "value" and entry.numeric_value is not None:
                unit = "%" if entry.canonical.endswith("%") else ""
                lexicon.values.setdefault(key, (entry.numeric_value, unit))
        # 词库未覆盖的功能说法与风速档位，补齐后仍走同一套枚举匹配
        for key, function in FUNCTION_FALLBACK.items():
            lexicon.functions.setdefault(normalize_voice(key), function)
        for key, (canonical, term) in SPEED_ENUM_FALLBACK.items():
            lexicon.enums.setdefault(
                normalize_voice(key), (canonical, ("speed",), term)
            )
        # “调暖些”“调亮点儿”里的“些/点儿”等同“一点”，词库只收了“一些/少些”一类组合
        for key in ("些", "点儿"):
            lexicon.little_words.add(normalize_voice(key))
        # “打开/关闭”等状态枚举优先于同形的方向词干，避免开关指令被当成相对调节
        for key in lexicon.enums:
            lexicon.increase_bases.pop(key, None)
            lexicon.decrease_bases.pop(key, None)
            lexicon.stems.pop(key, None)
        return lexicon

    def max_key_length(self) -> int:
        lengths = [
            len(key)
            for mapping in (
                self.functions,
                self.enums,
                self.units,
                self.max_words,
                self.min_words,
                self.device_types,
                self.areas,
                self.floors,
                self.device_names,
                self.increase_bases,
                self.decrease_bases,
                self.values,
            )
            for key in mapping
        ]
        lengths.extend(len(key) for key in self.query_words | self.little_words)
        return max(lengths, default=1)


@dataclass
class VoiceEntity:
    entity_id: str
    name: str
    area_id: str | None
    area_name: str
    domain: str
    state: dict[str, Any]
    capability: DeviceCapability
    floor_name: str = ""


@dataclass
class VoiceSlots:
    original: str
    target_kind: str = ""
    target_text: str = ""
    device_type: str = ""
    area_text: str | None = None
    floor_id: str | None = None
    invalid_floor: str = ""
    function_options: tuple[str, ...] = ()
    kind: str = "control"
    op: str = ""
    function: str | None = None
    candidates: tuple[str, ...] = ()
    value: float | None = None
    unit: str = ""
    unit_functions: tuple[str, ...] = ()
    direction: int = 0
    direction_word: str = ""
    little: bool = False
    extreme: str | None = None
    enum: str | None = None
    enum_term: str = ""
    enum_functions: tuple[str, ...] = ()
    validate: str = ""
    targets: list[VoiceEntity] = field(default_factory=list)
    clarify: list[EntityRef] = field(default_factory=list)
    query: bool = False
    derived_target: bool = False
    derive_candidates: list[EntityRef] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "op": self.op,
            "target_kind": self.target_kind,
            "target": self.target_text,
            "device_type": self.device_type,
            "area": self.area_text,
            "floor_id": self.floor_id,
            "function": self.function,
            "function_options": list(self.function_options),
            "candidates": list(self.candidates),
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
            "direction_word": self.direction_word,
            "little": self.little,
            "extreme": self.extreme,
            "enum": self.enum,
            "query": self.query,
        }


@dataclass
class VoiceResult:
    status: str
    reply: str
    slots: VoiceSlots
    actions: list[dict[str, Any]] = field(default_factory=list)
    targets: list[VoiceEntity] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    candidates: list[EntityRef] = field(default_factory=list)
    function: str = ""
    value: float | None = None
    amount: float | None = None
    unit: str = ""
    enum_term: str = ""
    group: bool = False
    device_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reply": self.reply,
            "slots": self.slots.as_dict() if self.slots else None,
            "function": self.function,
            "value": self.value,
            "amount": self.amount,
            "unit": self.unit,
            "group": self.group,
            "device_type": self.device_type,
            "targets": [
                {"entity_id": item.entity_id, "name": item.name, "area_id": item.area_id}
                for item in self.targets
            ],
            "skipped": self.skipped,
            "actions": self.actions,
            "candidates": [
                {"entity_id": ref.entity_id, "name": ref.name, "area_id": ref.area_id}
                for ref in self.candidates
            ],
        }


class VoiceParser:
    def __init__(
        self,
        lexicon: VoiceLexicon,
        aliases: list[AliasRecord],
        registry=None,
        profiles: dict[str, dict[str, Any]] | None = None,
        group_lists: list[dict[str, Any]] | None = None,
    ) -> None:
        self._lexicon = lexicon
        self._aliases = aliases
        self._registry = registry
        self._profiles = profiles or {}
        self._group_lists = group_lists or []
        self._steps = dict(DEFAULT_STEPS)
        self._max_len = max(lexicon.max_key_length(), 2)
        self._names: dict[str, str] = {}
        for record in aliases:
            for value in (record.alias, record.display_name):
                key = normalize_voice(value or "")
                if len(key) >= 2:
                    self._names.setdefault(key, record.display_name or record.alias)

    # ---------- public API ----------

    def parse(
        self,
        text: str,
        *,
        states: dict[str, dict],
        resolver: EntityResolver,
        member,
        view_patterns: list[str] | None,
        settings: dict[str, Any],
    ) -> VoiceResult | None:
        raw = (text or "").strip()
        normalized = normalize_voice(raw)
        if not normalized or len(normalized) > MAX_TEXT_LENGTH:
            return None
        if any(word in normalized for word in MULTI_WORDS):
            return None

        spans = _Spans(len(normalized))
        slots = VoiceSlots(original=raw)
        self._steps = {**DEFAULT_STEPS, **(settings.get("voice_default_steps") or {})}
        targets, clarify = self._target(
            normalized, spans, slots, states, resolver, member, view_patterns
        )
        if not slots.target_kind:
            return None
        if clarify:
            return self._clarify_result(slots, clarify)

        self._area_and_floor(normalized, spans, slots)
        self._apply_terminal_scope(slots, settings)
        if slots.invalid_floor:
            return self._reject(slots, f"当前家庭没有{slots.invalid_floor}。")
        if slots.target_kind == "entity" and (slots.area_text or slots.floor_id):
            scoped = [
                target
                for target in targets
                if (not slots.area_text or self._matches_area(target.capability, slots.area_text))
                and (not slots.floor_id or self._in_floor(target.entity_id, slots.floor_id))
            ]
            if not scoped:
                # 有候选但被区域/楼层筛掉，是范围问题；本来就没候选才可能是权限问题
                denied = (
                    None
                    if targets
                    else self._permission_denied_reply(
                        slots, states, resolver, member, view_patterns
                    )
                )
                return self._reject(slots, denied or self._missing_scope_reply(slots))
            targets = scoped
        slots.targets = targets

        self._action_slots(normalized, spans, slots)
        if slots.target_kind == "type":
            targets = self._group_targets(
                slots,
                states,
                view_patterns,
                want_control=slots.kind == "control" and not slots.query,
            )
            if not targets:
                denied = self._permission_denied_reply(
                    slots, states, resolver, member, view_patterns
                )
                return self._reject(slots, denied or self._missing_scope_reply(slots))
            limit = int(settings.get("voice_max_group_actions", 50))
            if not slots.query and len(targets) > limit:
                return self._reject(
                    slots,
                    f"匹配到 {len(targets)} 个设备，超过单次群控上限 {limit} 个，"
                    "请缩小区域或设备范围。",
                )
        slots.targets = targets
        if not slots.query and not slots.op and not slots.enum:
            self._supplement_enum(normalized, spans, slots)

        if not slots.op and slots.function and slots.kind == "control":
            slots.kind = "query"
            slots.op = "query"
            slots.query = True
        if not slots.query and not slots.op:
            return None
        if (
            slots.derived_target
            and len(targets) > 1
            and slots.target_kind == "entity"
        ):
            if slots.query:
                # 派生名命中多个候选、查询指令 → 聚合汇总全部目标，
                # 并按共同设备类型显示真实类型名（修复「设备设备」退化）。
                cap_types = {
                    target.capability.device_type
                    for target in targets
                    if target.capability.device_type
                }
                if len(cap_types) == 1:
                    slots.device_type = next(iter(cap_types))
            elif slots.derive_candidates:
                # 派生名命中多个候选、控制指令 → 退回澄清，不静默切换所有匹配设备。
                return self._clarify_result(slots, slots.derive_candidates)
        if not _residue_ok(normalized, spans):
            return None
        if not targets:
            denied = self._permission_denied_reply(
                slots, states, resolver, member, view_patterns
            )
            return self._reject(slots, denied or self._missing_scope_reply(slots))

        self._prefer_explicit(slots)
        if slots.kind == "query":
            return self._query_result(slots, states)
        return self._control_result(slots, states)

    def build_for_target(
        self,
        slots: VoiceSlots,
        ref: EntityRef,
        *,
        states: dict[str, dict],
    ) -> VoiceResult:
        target = self.entity_from_ref(ref, states)
        slots.targets = [target]
        slots.target_kind = "entity"
        slots.clarify = []
        if slots.kind == "query":
            return self._query_result(slots, states)
        return self._control_result(slots, states)

    def build_with_function(
        self, slots: VoiceSlots, function: str, *, states: dict[str, dict]
    ) -> VoiceResult:
        slots.function = function
        slots.candidates = ()
        slots.function_options = ()
        if slots.kind == "query":
            return self._query_result(slots, states)
        return self._control_result(slots, states)

    def entity_from_ref(self, ref: EntityRef, states: dict[str, dict]) -> VoiceEntity:
        state = states.get(ref.entity_id) or {}
        capability = self._capability(ref.entity_id, state, ref.name)
        return VoiceEntity(
            entity_id=ref.entity_id,
            name=ref.name,
            area_id=ref.area_id,
            area_name=self._area_name(ref.area_id),
            domain=ref.domain,
            state=state,
            capability=capability,
            floor_name=self._floor_name_of(ref.entity_id),
        )

    def preview(
        self,
        text: str,
        *,
        states: dict[str, dict],
        resolver: EntityResolver,
        member,
        view_patterns: list[str] | None,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.parse(
            text,
            states=states,
            resolver=resolver,
            member=member,
            view_patterns=view_patterns,
            settings=settings,
        )
        if result is None:
            return {"route": "slow_path", "reason": "voice_no_match"}
        return {"route": "fast_path", **result.as_dict()}

    # ---------- target extraction ----------

    def _target(
        self,
        text: str,
        spans: "_Spans",
        slots: VoiceSlots,
        states: dict[str, dict],
        resolver: EntityResolver,
        member,
        view_patterns: list[str] | None,
    ) -> tuple[list[VoiceEntity], list[EntityRef]]:
        combined: list[tuple[str, str, str]] = []
        names = dict(self._names)
        derived_cores: set[str] = set()
        # 派生核心名不得与区域/楼层名冲突（“客厅调亮一点”不能被传感器“客厅温度”的
        # 派生核心“客厅”抢走），否则会用实体列表覆盖完整的区域+意图解析。
        place_names: set[str] = set()
        if self._registry is not None:
            for area_id in getattr(self._registry, "areas", {}) or {}:
                if self._area_name(area_id):
                    place_names.add(normalize_voice(self._area_name(area_id)))
            floor_name = getattr(self._registry, "floor_name", None)
            for floor_id in getattr(self._registry, "floors", {}) or {}:
                name = floor_name(floor_id) if callable(floor_name) else None
                if name:
                    place_names.add(normalize_voice(str(name)))
        for entity_id, state in states.items():
            attributes = (state or {}).get("attributes") or {}
            friendly = normalize_voice(str(attributes.get("friendly_name") or ""))
            if len(friendly) >= 2:
                names.setdefault(friendly, str(attributes["friendly_name"]))
            derived = _derived_core(friendly)
            if derived and derived not in place_names:
                # HA 会把尾部域词追加到友好名（如「筒灯灯」），
                # 注册「去掉末尾一个域词」的派生名作为额外匹配键。
                names.setdefault(derived, str(attributes["friendly_name"]))
                derived_cores.add(derived)
        for key, display in names.items():
            combined.append((key, "name", display))
        types = dict(self._lexicon.device_types)
        for key, device_type in DOMAIN_TYPE_HINTS.items():
            types.setdefault(normalize_voice(key), device_type)
        for key, device_type in types.items():
            combined.append((key, "type", device_type))

        match = self._find_combined(text, spans, combined)
        if match is None:
            return [], []
        key, kind, payload, start, end = match
        spans.add(start, end)

        if kind == "type":
            slots.target_kind = "type"
            slots.target_text = key
            slots.device_type = payload
            return [], []

        slots.target_kind = "entity"
        slots.target_text = key
        if key in derived_cores:
            slots.derived_target = True
        domain = self._domain_of_name(key, states)
        resolution = resolver.resolve(
            key,
            domain,
            states,
            member=member,
            view_patterns=view_patterns,
            want_control=False,
        )
        if slots.derived_target:
            # 派生名匹配到多个候选时，控制指令退回澄清、查询聚合汇总，
            # 交给 parse() 按意图决定，不在这里静默截断。
            slots.derive_candidates = resolution.entities
        if resolution.status == "none":
            return [], []
        if resolution.status == "ambiguous":
            if slots.derived_target:
                # 派生名命中多个候选：是否聚合/澄清由 parse() 按意图决定。
                # 先把全部候选作为目标对象返回，交给 parse() 裁量。
                return [
                    self.entity_from_ref(ref, states) for ref in resolution.entities
                ], []
            return [], resolution.entities
        return [self.entity_from_ref(ref, states) for ref in resolution.entities], []

    def _domain_of_name(self, key: str, states: dict[str, dict]) -> str | None:
        """场景、脚本、自动化按名字定位时不能靠词面推断域，否则会被误判成灯具。"""
        for entity_id, state in states.items():
            attributes = (state or {}).get("attributes") or {}
            if normalize_voice(str(attributes.get("friendly_name") or "")) != key:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain in _SCENE_TARGET_DOMAINS:
                return domain
        return None

    def _area_and_floor(
        self,
        text: str,
        spans: "_Spans",
        slots: VoiceSlots,
    ) -> None:
        area_map: dict[str, str] = dict(self._lexicon.areas)
        if self._registry is not None:
            for area_id in getattr(self._registry, "areas", {}) or {}:
                name = self._area_name(area_id)
                if name:
                    area_map.setdefault(normalize_voice(name), name)
        found = self._find_first(text, spans, area_map)
        if found is not None:
            key, value, start, end = found
            slots.area_text = value or key
            spans.add(start, end)

        floors = getattr(self._registry, "floors", {}) or {}
        valid_floor_ids = set(floors)
        floor_map: dict[str, str] = {}
        for floor_id in valid_floor_ids:
            name = (
                self._registry.floor_name(floor_id)
                if hasattr(self._registry, "floor_name")
                else None
            )
            if name:
                floor_map.setdefault(normalize_voice(name), floor_id)
            floor_map.setdefault(normalize_voice(floor_id), floor_id)
        if valid_floor_ids:
            for alias, canonical in self._lexicon.floors.items():
                if canonical in valid_floor_ids:
                    floor_map.setdefault(alias, canonical)
        else:
            for alias in self._lexicon.floors:
                floor_map.setdefault(alias, "")
        found = self._find_first(text, spans, floor_map)
        if found is not None:
            key, value, start, end = found
            slots.floor_id = value or None
            spans.add(start, end)
        elif valid_floor_ids:
            unknown = self._find_first(
                text, spans, {alias: alias for alias in self._lexicon.floors}
            )
            if unknown is not None:
                key, _, start, end = unknown
                slots.invalid_floor = (self._lexicon.floors.get(key) and key) or key
                spans.add(start, end)

    def _apply_terminal_scope(
        self, slots: VoiceSlots, settings: dict[str, Any]
    ) -> None:
        """表述缺少楼层/区域时，按语音终端安装位置补全（PDF 单控/群控查找规则）。"""
        terminal_area = str(settings.get("voice_terminal_area_id") or "").strip()
        terminal_floor = str(settings.get("voice_terminal_floor_id") or "").strip()
        if slots.area_text and not slots.floor_id:
            slots.floor_id = self._floor_id_for(terminal_floor) if terminal_floor else None
        elif not slots.area_text and not slots.floor_id:
            if terminal_area:
                slots.area_text = self._area_display(terminal_area)
            elif terminal_floor:
                slots.floor_id = self._floor_id_for(terminal_floor)

    def _floor_id_for(self, value: str) -> str | None:
        if not value:
            return None
        floors = getattr(self._registry, "floors", {}) or {}
        if value in floors:
            return value
        target = normalize_voice(value)
        for floor_id in floors:
            name = (
                self._registry.floor_name(floor_id)
                if hasattr(self._registry, "floor_name")
                else None
            )
            if name and normalize_voice(name) == target:
                return floor_id
        return None

    def _area_display(self, value: str) -> str:
        if not value:
            return ""
        areas = getattr(self._registry, "areas", {}) or {}
        if value in areas:
            return self._area_name(value) or value
        target = normalize_voice(value)
        for area_id in areas:
            name = self._area_name(area_id)
            if name and normalize_voice(name) == target:
                return name
        return value

    def _area_id_for_text(self, text: str | None) -> str | None:
        if not text:
            return None
        areas = getattr(self._registry, "areas", {}) or {}
        if text in areas:
            return text
        target = normalize_voice(text)
        for area_id in areas:
            name = self._area_name(area_id)
            if name and normalize_voice(name) == target:
                return area_id
        return None

    def _floor_name(self, floor_id: str | None) -> str:
        if not floor_id or self._registry is None:
            return ""
        floor_name = getattr(self._registry, "floor_name", None)
        return (floor_name(floor_id) if callable(floor_name) else None) or ""

    def _floor_of_target(self, target: VoiceEntity) -> str:
        return self._floor_name_of(target.entity_id)

    def _floor_name_of(self, entity_id: str) -> str:
        if self._registry is None:
            return ""
        floor_of = getattr(self._registry, "floor_of", None)
        if not callable(floor_of):
            return ""
        return self._floor_name(floor_of(entity_id))

    def _multi_floor(self) -> bool:
        floors = getattr(self._registry, "floors", {}) or {}
        return len(floors) > 1

    # ---------- intent slots ----------

    def _action_slots(self, text: str, spans: "_Spans", slots: VoiceSlots) -> None:
        query = self._find_first(
            text, spans, {word: word for word in self._lexicon.query_words}
        )
        if query is not None:
            slots.query = True
            slots.kind = "query"
            slots.op = "query"
            spans.add(query[2], query[3])

        scene_op = self._scene_activation(
            text, spans, slots, allow_op=not slots.query
        )
        if scene_op is not None:
            slots.enum = scene_op
            slots.enum_term = _SCENE_OP_WORDS.get(scene_op, scene_op)
            slots.function = "status"
            slots.op = "state"
            slots.kind = "control"
            return

        found = self._find_first(text, spans, self._lexicon.functions)
        if found is not None:
            key, value, start, end = found
            slots.function = value
            spans.add(start, end)

        extreme = self._find_first(text, spans, self._lexicon.max_words)
        extreme_kind = "max"
        if extreme is None:
            extreme = self._find_first(text, spans, self._lexicon.min_words)
            extreme_kind = "min"
        if extreme is not None:
            key, functions, start, end = extreme
            slots.extreme = extreme_kind
            slots.candidates += tuple(functions)
            spans.add(start, end)

        enum = self._find_first(text, spans, self._lexicon.enums)
        if enum is not None:
            key, (canonical, functions, term), start, end = enum
            slots.enum = canonical
            slots.enum_term = term
            slots.enum_functions = tuple(functions)
            slots.candidates += tuple(functions)
            spans.add(start, end)

        direction = self._find_first(text, spans, self._lexicon.increase_bases)
        if direction is None:
            direction = self._find_first(text, spans, self._lexicon.decrease_bases)
        direction_end: int | None = None
        if direction is not None:
            key, (sign, functions), start, end = direction
            slots.direction = sign
            slots.direction_word = key
            slots.candidates += tuple(functions)
            spans.add(start, end)
            direction_end = end
        elif enum is None:
            stem = self._find_stem(text, spans)
            if stem is not None:
                key, sign, functions, start, end = stem
                slots.direction = sign
                slots.direction_word = key
                slots.candidates += tuple(functions)
                spans.add(start, end)
                direction_end = end

        little = self._find_first(
            text, spans, {word: word for word in self._lexicon.little_words}
        )
        if little is not None:
            slots.little = True
            spans.add(little[2], little[3])
        if (
            slots.direction
            and not slots.little
            and text.endswith("点")
            and spans.free(len(text) - 1)
        ):
            # 只在已识别出相对方向时把句尾“点”当少量，避免“七点开灯”被误判
            slots.little = True
            spans.add(len(text) - 1, len(text))

        quantity = self._find_quantity(text, spans)
        if quantity is not None:
            value, unit, unit_functions, start, end = quantity
            slots.value = value
            slots.unit = unit
            slots.unit_functions = tuple(unit_functions)
            slots.candidates += tuple(unit_functions)
            spans.add(start, end)
            gap = (
                text[direction_end:start]
                if direction_end is not None and start >= direction_end
                else ""
            )
            if (
                slots.direction
                and ("到" in gap or "至" in gap)
                and all(char in "到至为 " for char in gap)
            ):
                # “调冷到5000K”“调高到26度”给的是目标值，不是相对增减
                slots.direction = 0

        if not slots.query and any(hint in text for hint in QUERY_HINTS):
            slots.query = True
        if slots.query and not slots.function:
            for hint, function in QUERY_FUNCTION_HINTS.items():
                if hint in text:
                    slots.function = function
                    break

        if slots.query:
            slots.kind = "query"
            slots.op = "query"
        elif slots.extreme:
            slots.op = "extreme"
            slots.kind = "control"
        elif slots.direction:
            slots.op = "relative"
            slots.kind = "control"
        elif slots.value is not None and slots.enum is None:
            slots.op = "set"
            slots.kind = "control"
        elif slots.enum:
            slots.op = "state"
            slots.kind = "control"
        elif slots.function in ("status", "fault", "value"):
            slots.kind = "query"
            slots.op = "query"
            slots.query = True

    def _scene_activation(
        self, text: str, spans: "_Spans", slots: VoiceSlots, *, allow_op: bool = True
    ) -> str | None:
        """HA 场景、脚本与自动化的激活动作，单独于普通开关联动。"""
        target = slots.targets[0] if slots.targets else None
        if target is None or target.domain not in _SCENE_TARGET_DOMAINS:
            return None
        verb = None
        for candidate in _SCENE_VERBS:
            index = text.find(candidate)
            while index >= 0:
                if spans.free(index, index + len(candidate)):
                    verb = candidate
                    spans.add(index, index + len(candidate))
                    break
                index = text.find(candidate, index + 1)
            if verb:
                break
        noun = False
        for word in _SCENE_NOUNS:
            index = text.find(word)
            while index >= 0:
                if spans.free(index, index + len(word)):
                    spans.add(index, index + len(word))
                    noun = True
                    break
                index = text.find(word, index + 1)
        if not allow_op:
            # 查询句只消化“场景/脚本”这类名词，不产生控制动作
            return None
        if verb is None and not noun:
            return None
        if verb is not None and _SCENE_VERBS[verb] == "stop":
            if target.domain == "scene":
                return None
            return "stop"
        if verb is None:
            return "execute" if target.domain != "automation" else "trigger"
        return _SCENE_VERBS[verb]

    def _supplement_enum(
        self, text: str, spans: "_Spans", slots: VoiceSlots
    ) -> None:
        """词库没有收录、但设备本身提供的枚举值（如 HDMI1）直接按设备能力匹配。"""
        best: tuple[tuple[int, int], str, str, str, int, int] | None = None
        for target in slots.targets:
            for name, spec in target.capability.functions.items():
                if spec.kind != "enum":
                    continue
                if slots.function and name != slots.function:
                    continue
                for value in spec.values:
                    key = normalize_voice(value)
                    if len(key) < 2 or key in ("on", "off"):
                        continue
                    start = text.find(key)
                    while start >= 0:
                        if spans.free(start, start + len(key)):
                            rank = (start, -len(key))
                            if best is None or rank < best[0]:
                                best = (rank, value, value, name, start, start + len(key))
                            break
                        start = text.find(key, start + 1)
        if best is None:
            return
        rank, canonical, term, function, start, end = best
        slots.enum = canonical
        slots.enum_term = term
        slots.enum_functions = (function,)
        slots.candidates += (function,)
        slots.op = "state"
        slots.kind = "control"
        spans.add(start, end)

    def _prefer_explicit(self, slots: VoiceSlots) -> None:
        if slots.function:
            slots.candidates = (slots.function,) + tuple(
                item for item in slots.candidates if item != slots.function
            )

    # ---------- targets by device type ----------

    def _custom_group_ids(self, slots: VoiceSlots) -> set[str] | None:
        """PDF：管理员配置了群控设备列表时，按列表取设备而不是自动推断。"""
        if not self._group_lists or not slots.device_type:
            return None
        area_id = self._area_id_for_text(slots.area_text) if slots.area_text else None
        best: tuple[int, dict[str, Any]] | None = None
        for item in self._group_lists:
            if item.get("device_type") not in (None, "", slots.device_type):
                continue
            list_area = str(item.get("area_id") or "")
            list_floor = str(item.get("floor_id") or "")
            if list_area:
                if not area_id or list_area != area_id:
                    continue
                score = 2
            elif list_floor:
                if not slots.floor_id or list_floor != slots.floor_id:
                    continue
                score = 1
            else:
                score = 0
            if best is None or score > best[0]:
                best = (score, item)
        if best is None:
            return None
        return {str(value) for value in best[1].get("entity_ids") or []}

    def _group_targets(
        self,
        slots: VoiceSlots,
        states: dict[str, dict],
        view_patterns: list[str] | None,
        *,
        want_control: bool,
    ) -> list[VoiceEntity]:
        custom = self._custom_group_ids(slots)
        result: list[VoiceEntity] = []
        for entity_id, state in states.items():
            if custom is not None and entity_id not in custom:
                continue
            if view_patterns is not None and not any(
                fnmatch.fnmatchcase(entity_id, pattern) for pattern in view_patterns
            ):
                continue
            attributes = (state or {}).get("attributes") or {}
            name = str(attributes.get("friendly_name") or entity_id)
            capability = self._capability(entity_id, state, name)
            if capability.device_type != slots.device_type:
                continue
            if want_control and not capability.controllable:
                continue
            area_id = capability.area_id
            if slots.area_text and not self._matches_area(capability, slots.area_text):
                continue
            if slots.floor_id and not self._in_floor(entity_id, slots.floor_id):
                continue
            result.append(
                VoiceEntity(
                    entity_id=entity_id,
                    name=name,
                    area_id=area_id,
                    area_name=self._area_name(area_id),
                    domain=capability.domain,
                    state=state,
                    capability=capability,
                    floor_name=self._floor_name_of(entity_id),
                )
            )
        result.sort(key=lambda item: (item.area_id or "", item.name, item.entity_id))
        return result

    def _matches_area(self, capability: DeviceCapability, area_text: str) -> bool:
        target = normalize_voice(area_text)
        names = {normalize_voice(capability.area_id or "")}
        name = self._area_name(capability.area_id)
        if name:
            names.add(normalize_voice(name))
        return any(
            value and (target == value or target in value or value in target)
            for value in names
        )

    def _in_floor(self, entity_id: str, floor_id: str) -> bool:
        if self._registry is None or not hasattr(self._registry, "floor_of"):
            return True
        return self._registry.floor_of(entity_id) == floor_id

    # ---------- control plan ----------

    def _function_ambiguity(self, slots: VoiceSlots) -> VoiceResult | None:
        """PDF：绝对值歧义给文字提示，极限值/相对值歧义列出功能让用户选。"""
        if slots.function or slots.op not in ("set", "relative", "extreme"):
            return None
        options: list[str] | None = None
        for target in slots.targets:
            current = self._function_candidates(target.capability, slots)
            options = current if options is None else [
                name for name in options if name in current
            ]
        if not options or len(options) < 2:
            return None
        if slots.op == "set":
            return self._reject(slots, self._ambiguity_hint(slots, options))
        slots.function_options = tuple(options)
        lines = [
            f"{index + 1} {FUNCTION_LABELS.get(name, name)}"
            for index, name in enumerate(options)
        ]
        return VoiceResult(
            status="clarify",
            reply="请选择要调节的功能：" + "、".join(lines) + "。请回复序号或功能名称。",
            slots=slots,
            targets=slots.targets,
            group=slots.target_kind == "type",
            device_type=slots.device_type,
        )

    def _ambiguity_hint(self, slots: VoiceSlots, options: list[str]) -> str:
        function = options[0]
        label = FUNCTION_LABELS.get(function, function)
        target = slots.targets[0] if slots.targets else None
        example = label
        if target is not None:
            spec = target.capability.spec(function)
            if spec is not None:
                value = slots.value if slots.value is not None else 0
                example = (
                    f"{_type_label(target.capability.device_type)}{label}"
                    f"{_format_number(value)}{spec.unit or ''}"
                )
        return (
            "推断不出唯一的控制指令，可在现有的指令中加上"
            f"“功能名称”再试，比如“{example}”。"
        )

    def _control_result(
        self, slots: VoiceSlots, states: dict[str, dict]
    ) -> VoiceResult:
        ambiguous = self._function_ambiguity(slots)
        if ambiguous is not None:
            return ambiguous
        actions: list[dict[str, Any]] = []
        skipped: list[str] = []
        function_used = ""
        value_used: float | None = None
        amount_used: float | None = None
        unit_used = ""
        enum_used = ""

        for target in slots.targets:
            if not target.capability.controllable:
                skipped.append(target.name)
                continue
            position = self._position_value(target.capability, slots)
            if position is not None:
                spec = target.capability.spec("course")
                action, new_value, _, error = self._number_action(
                    spec, target, position
                )
                if error:
                    return self._reject(slots, error)
                actions.append(action)
                function_used = "course"
                value_used = new_value
                amount_used = None
                unit_used = spec.unit
                continue
            function = self._resolve_function(target.capability, slots)
            if function is None:
                skipped.append(target.name)
                continue
            spec = target.capability.spec(function)
            if spec is None or spec.readonly:
                skipped.append(target.name)
                continue
            action, new_value, amount, error = self._action_for(
                target, function, spec, slots
            )
            if error:
                return self._reject(slots, error)
            if action is None:
                skipped.append(target.name)
                continue
            actions.append(action)
            function_used = function
            value_used = new_value
            amount_used = amount
            unit_used = spec.unit
            if spec.kind == "enum" and slots.op == "extreme":
                raw_enum = str(action["data"].get(spec.field, ""))
                enum_used = VALUE_LABELS.get(raw_enum, raw_enum)

        if not actions:
            if slots.function:
                label = FUNCTION_LABELS.get(slots.function, slots.function)
            elif slots.op == "relative":
                label = ""
            else:
                key = slots.candidates[0] if slots.candidates else ""
                label = FUNCTION_LABELS.get(key, key)
            if slots.target_kind == "type":
                if not label:
                    return self._reject(
                        slots,
                        f"没有找到支持这项调节的{_type_label(slots.device_type)}类设备。",
                    )
                return self._reject(
                    slots,
                    f"没有找到支持{label}功能的"
                    f"{_type_label(slots.device_type)}类设备。",
                )
            target = slots.targets[0] if slots.targets else None
            available = (
                "、".join(
                    FUNCTION_LABELS.get(item, item)
                    for item in target.capability.function_names
                )
                if target
                else ""
            )
            if available:
                if not label:
                    return self._reject(
                        slots,
                        f"这个设备不支持这项调节，这个设备具有以下功能：{available}。",
                    )
                return self._reject(
                    slots,
                    f"这个设备不支持{label}功能，这个设备具有以下功能：{available}。",
                )
            name = target.name if target else "该设备"
            return self._reject(slots, f"{name}不支持这个操作。")

        result = VoiceResult(
            status="ok",
            reply="",
            slots=slots,
            actions=actions,
            targets=slots.targets,
            skipped=skipped,
            function=function_used,
            value=value_used,
            amount=amount_used,
            unit=unit_used,
            enum_term=enum_used,
            group=slots.target_kind == "type",
            device_type=slots.device_type,
        )
        result.reply = self._control_reply(result, slots)
        return result

    def _action_for(
        self,
        target: VoiceEntity,
        function: str,
        spec: FunctionSpec,
        slots: VoiceSlots,
    ) -> tuple[dict[str, Any] | None, float | None, float | None, str]:
        if slots.op == "state":
            return self._enum_action(target, function, spec, slots)

        if slots.op == "extreme":
            if spec.kind == "enum":
                if not spec.values:
                    return None, None, None, ""
                want = "high" if slots.extreme == "max" else "low"
                value = self._enum_value(spec, want, want)
                if value is None:
                    value = (
                        spec.values[-1] if slots.extreme == "max" else spec.values[0]
                    )
                return (
                    {
                        "domain": target.domain,
                        "service": spec.service,
                        "entity_id": target.entity_id,
                        "data": {spec.field: value},
                    },
                    None,
                    None,
                    "",
                )
            value = spec.maximum if slots.extreme == "max" else spec.minimum
            if value is None:
                return None, None, None, ""
            return self._number_action(spec, target, value)

        if slots.op == "relative":
            if spec.kind == "enum":
                return self._relative_enum_action(target, function, spec, slots)
            if slots.value is not None:
                if slots.unit and not _unit_compatible(slots.unit, spec.unit):
                    return None, None, None, ""
                amount = abs(float(slots.value))
            elif slots.little:
                amount = _little_amount(function, spec)
            else:
                amount = spec.step or self._steps.get(function) or 1.0
            current = spec.read(target.state)
            if current is None:
                return (
                    None,
                    None,
                    None,
                    f"{target.name}当前{spec.unit or ''}值读取不到，暂时无法调节。",
                )
            try:
                number = float(current)
            except (TypeError, ValueError):
                return None, None, None, ""
            desired = spec.clamp(number + self._relative_sign(function, slots) * amount)
            action, shown, _, error = self._number_action(spec, target, desired)
            return action, shown, amount, error

        if slots.op == "set":
            if slots.value is None:
                return None, None, None, ""
            if slots.unit and not _unit_compatible(slots.unit, spec.unit):
                return None, None, None, ""
            action, value, _, error = self._number_action(spec, target, slots.value)
            return action, value, None, error

        return self._enum_action(target, function, spec, slots)

    def _relative_sign(self, function: str, slots: VoiceSlots) -> int:
        """色温与空调温度方向相反：调冷升高开尔文，调暖降低开尔文。"""
        if function != "colorTemp":
            return slots.direction
        word = slots.direction_word
        if any(char in word for char in ("冷", "凉", "寒")):
            return 1
        if any(char in word for char in ("暖", "热")):
            return -1
        return slots.direction

    def _relative_enum_action(
        self,
        target: VoiceEntity,
        function: str,
        spec: FunctionSpec,
        slots: VoiceSlots,
    ) -> tuple[dict[str, Any] | None, float | None, float | None, str]:
        label = FUNCTION_LABELS.get(function, function)
        if slots.unit and not _unit_compatible(slots.unit, spec.unit):
            return None, None, None, ""
        values = list(spec.values)
        current = spec.read(target.state)
        if current is None:
            return (
                None,
                None,
                None,
                f"{target.name}当前{label}读取不到，暂时无法调节。",
            )
        by_norm = {normalize_voice(value): index for index, value in enumerate(values)}
        index = by_norm.get(normalize_voice(str(current)))
        if index is None:
            return None, None, None, ""
        if slots.value is not None:
            amount = max(1, int(round(abs(float(slots.value)))))
        else:
            amount = 1
        desired = max(0, min(len(values) - 1, index + slots.direction * amount))
        if desired == index:
            edge = "最高" if slots.direction > 0 else "最低"
            return (
                None,
                None,
                None,
                f"{_place(target)}{target.name}{label}已经是{edge}档，无法继续"
                f"{'调高' if slots.direction > 0 else '调低'}。",
            )
        return (
            {
                "domain": target.domain,
                "service": spec.service,
                "entity_id": target.entity_id,
                "data": {spec.field: values[desired]},
            },
            float(desired + 1),
            float(abs(desired - index)),
            "",
        )

    def _number_action(
        self, spec: FunctionSpec, target: VoiceEntity, value: float
    ) -> tuple[dict[str, Any] | None, float | None, None, str]:
        if not spec.in_range(value):
            minimum = _format_number(spec.minimum or 0)
            maximum = _format_number(spec.maximum or 0)
            label = FUNCTION_LABELS.get(spec.name, spec.name)
            return (
                None,
                None,
                None,
                f"{_place(target)}{target.name}{label}的设定范围为 "
                f"{minimum}{spec.unit} 到 {maximum}{spec.unit}，请重试。",
            )
        if spec.write_factor != 1.0:
            raw = round(value * spec.write_factor, 3)
            shown = round(raw / spec.write_factor, 2)
        elif spec.name in (
            "brightness",
            "course",
            "angle",
            "targetHumidity",
            "volume",
            "speed",
        ):
            raw = int(round(value))
            shown = float(raw)
        else:
            raw = round(value, 1)
            shown = raw
        if spec.name == "targetTemp" and spec.unit not in ("°F",):
            raw = round(float(raw), 1)
            shown = raw
        return (
            {
                "domain": target.domain,
                "service": spec.service,
                "entity_id": target.entity_id,
                "data": {spec.field: raw},
            },
            shown,
            None,
            "",
        )

    def _enum_action(
        self,
        target: VoiceEntity,
        function: str,
        spec: FunctionSpec,
        slots: VoiceSlots,
    ) -> tuple[dict[str, Any] | None, float | None, None, str]:
        canonical = slots.enum or ""
        if function in ("status", "systemSwitch"):
            service = _status_service(canonical, target.domain)
            if service is None:
                return None, None, None, ""
            return (
                {
                    "domain": target.domain,
                    "service": service,
                    "entity_id": target.entity_id,
                    "data": {},
                },
                None,
                None,
                "",
            )
        if spec.kind == "color":
            rgb = _rgb_tuple(canonical)
            if rgb is None:
                return None, None, None, ""
            return (
                {
                    "domain": target.domain,
                    "service": spec.service,
                    "entity_id": target.entity_id,
                    "data": {spec.field: list(rgb)},
                },
                None,
                None,
                "",
            )
        value = self._enum_value(spec, canonical, slots.enum_term)
        if value is None:
            return None, None, None, ""
        return (
            {
                "domain": target.domain,
                "service": spec.service,
                "entity_id": target.entity_id,
                "data": {spec.field: value},
            },
            None,
            None,
            "",
        )

    def _enum_value(self, spec: FunctionSpec, canonical: str, term: str) -> Any | None:
        lowered = canonical.lower()
        values = spec.values
        by_norm = {normalize_voice(item): item for item in values}
        candidates: list[str] = [canonical, term]
        candidates.extend(MODE_SYNONYMS.get(lowered, ()))
        candidates.extend(SOURCE_SYNONYMS.get(lowered, ()))
        candidates.extend(STATE_WORDS.get(lowered, ()))
        candidates.extend(SPEED_LEVEL_SYNONYMS.get(lowered, ()))
        for candidate in candidates:
            if candidate in values:
                return candidate
            key = normalize_voice(candidate)
            if key in by_norm:
                return by_norm[key]
        speed = SPEED_ALIASES.get(lowered)
        if speed is not None and values:
            if spec.unit == "%":
                return speed * 20
            index = max(0, min(len(values) - 1, speed - 1))
            return values[index]
        return None

    # ---------- replies ----------

    def _control_reply(self, result: VoiceResult, slots: VoiceSlots) -> str:
        label = FUNCTION_LABELS.get(result.function, result.function)
        if len(result.targets) == 1:
            return self._single_control_reply(result, slots, label)
        return self._group_control_reply(result, slots, label)

    def _single_control_reply(
        self, result: VoiceResult, slots: VoiceSlots, label: str
    ) -> str:
        target = result.targets[0]
        place = _place(target)
        unit = result.unit
        if slots.op == "state" and target.domain in _SCENE_TARGET_DOMAINS:
            verb = _state_verb(slots.enum or "", slots.enum_term)
            noun = {"scene": "场景", "script": "脚本"}.get(target.domain, "自动化")
            return f"已{verb}{place}{target.name}{noun}。"
        if slots.op == "state" and result.function in ("status", "systemSwitch"):
            verb = _state_verb(slots.enum or "", slots.enum_term)
            return f"已{verb}{place}{target.name}。"
        if slots.op == "state":
            term = slots.enum_term or slots.enum or ""
            return f"{place}{target.name}{label}已设为{term}。"
        if slots.op == "extreme":
            if result.enum_term:
                return f"{place}{target.name}{label}已设为{result.enum_term}。"
            word = "最大值" if slots.extreme == "max" else "最小值"
            return (
                f"{place}{target.name}{label}已设为{word}："
                f"{_format_number(result.value or 0)}{unit}。"
            )
        return (
            f"{place}{target.name}的{label}已设为"
            f"{_format_number(result.value or 0)}{unit}。"
        )

    def _group_control_reply(
        self, result: VoiceResult, slots: VoiceSlots, label: str
    ) -> str:
        place = self._scope_prefix(slots)
        type_label = _type_label(slots.device_type)
        done = len(result.actions)
        total = len(result.targets)
        if slots.op == "state" and result.function in ("status", "systemSwitch"):
            verb = _state_verb(slots.enum or "", slots.enum_term)
            if done >= total:
                return f"已{verb}{place}所有{type_label}。"
            return self._partial_group_reply(
                place, type_label, done, label, verb, result
            )
        if slots.op == "state":
            value = slots.enum_term or slots.enum or ""
        elif slots.op == "extreme" and result.enum_term:
            value = result.enum_term
        else:
            value = f"{_format_number(result.value or 0)}{result.unit}"
        if done >= total:
            return f"已将{place}所有{type_label}{label}设为{value}。"
        return self._partial_group_reply(
            place, type_label, done, label, value, result
        )

    def _partial_group_reply(
        self,
        place: str,
        type_label: str,
        done: int,
        label: str,
        value: str,
        result: VoiceResult,
    ) -> str:
        executed = {action.get("entity_id") for action in result.actions}
        listed = [
            f"“{item.name}”"
            for item in result.targets
            if item.entity_id in executed
        ][:6]
        names = "、".join(listed)
        return (
            f"已将{place}{done}个{type_label}{label}设为{value}，"
            f"分别为：{names}。"
        )

    def _query_result(self, slots: VoiceSlots, states: dict[str, dict]) -> VoiceResult:
        specs: list[tuple[VoiceEntity, FunctionSpec]] = []
        missing: list[str] = []
        for target in slots.targets:
            function = slots.function or self._resolve_function(
                target.capability, slots
            )
            spec = target.capability.spec(function) if function else None
            if spec is None and slots.function:
                missing.append(target.name)
                continue
            if spec is None:
                for fallback in ("status", "value", "fault"):
                    if target.capability.supports(fallback):
                        spec = target.capability.spec(fallback)
                        break
            if spec is None:
                missing.append(target.name)
                continue
            specs.append((target, spec))
        if not specs:
            target = slots.targets[0]
            available = "、".join(
                FUNCTION_LABELS.get(item, item)
                for item in target.capability.function_names
            )
            label = FUNCTION_LABELS.get(slots.function or "", slots.function or "")
            return VoiceResult(
                status="reject",
                reply=f"这个设备不支持查询{label}功能，这个设备具有以下功能：{available}。",
                slots=slots,
                targets=slots.targets,
                skipped=missing,
                group=slots.target_kind == "type",
                device_type=slots.device_type,
            )

        names = {spec.name for _, spec in specs}
        if len(specs) == 1:
            reply = self._single_query_reply(*specs[0])
        elif len(names) == 1 and next(iter(names)) in ("status", "systemSwitch"):
            reply = self._group_query_reply(specs, slots)
        else:
            reply = (
                "；".join(
                    self._single_query_reply(target, spec).rstrip("。")
                    for target, spec in specs[:6]
                )
                + "。"
            )
        return VoiceResult(
            status="query",
            reply=reply,
            slots=slots,
            targets=[target for target, _ in specs],
            skipped=missing,
            function=next(iter(names)),
            group=len(specs) > 1,
            device_type=slots.device_type,
        )

    def _single_query_reply(self, target: VoiceEntity, spec: FunctionSpec) -> str:
        place = _place(target)
        prefix = f"查询到：{place}的{target.name}" if place else f"查询到：{target.name}"
        if spec.name not in ("status", "systemSwitch", "value", "fault"):
            label = FUNCTION_LABELS.get(spec.name, spec.name)
            value = self._value_text(target, spec.name)
            if not value:
                return f"{prefix}{label}暂时读取不到。"
            return f"{prefix}{label}为{value}{spec.unit}。"
        dtype = target.capability.device_type
        if dtype in ("curtain", "window"):
            return self._cover_query_reply(target, prefix)
        if dtype == "light":
            return self._light_query_reply(target, prefix)
        if dtype == "AC":
            return self._climate_query_reply(target, prefix)
        if dtype == "heating":
            return self._heating_query_reply(target, prefix)
        if dtype == "freshAir":
            return self._air_query_reply(target, prefix)
        if dtype in ("media", "tv"):
            return self._media_query_reply(target, prefix)
        if dtype == "envSensor":
            return self._sensor_query_reply(target, prefix)
        return self._status_query_reply(target, prefix)

    def _number_text(self, target: VoiceEntity, function: str) -> str:
        spec = target.capability.spec(function)
        if spec is None:
            return ""
        value = spec.read(target.state)
        if value is None:
            return ""
        try:
            return _format_number(float(value))
        except (TypeError, ValueError):
            return ""

    def _value_text(self, target: VoiceEntity, function: str) -> str:
        spec = target.capability.spec(function)
        if spec is None:
            return ""
        value = spec.read(target.state)
        if value is None:
            return ""
        if spec.kind == "number":
            try:
                return _format_number(float(value))
            except (TypeError, ValueError):
                return str(value)
        text = spec.value_labels.get(str(value))
        if text:
            return str(text)
        return str(VALUE_LABELS.get(str(value), value))

    def _rgb_text(self, target: VoiceEntity) -> str:
        rgb = (target.state.get("attributes") or {}).get("rgb_color")
        if not rgb:
            return ""
        try:
            return "/".join(str(int(item)) for item in rgb)
        except (TypeError, ValueError):
            return ""

    def _on(self, target: VoiceEntity) -> bool:
        return str(target.state.get("state") or "") in ("on", "open", "unlocked")

    def _light_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        if not self._on(target):
            return f"{prefix}已关闭。"
        parts: list[str] = []
        brightness = self._number_text(target, "brightness")
        if brightness:
            parts.append(f"亮度{brightness}%")
        color_temp = self._number_text(target, "colorTemp")
        if color_temp:
            parts.append(f"色温{color_temp}K")
        rgb = self._rgb_text(target)
        if rgb:
            parts.append(f"颜色值为（{rgb}）")
        if not parts:
            return f"{prefix}已打开。"
        return prefix + parts[0] + "".join(f"、{part}" for part in parts[1:]) + "。"

    def _cover_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        course = self._number_text(target, "course")
        angle = self._number_text(target, "angle")
        if not course and not angle:
            return f"{prefix}不支持查询功能。"
        if course:
            if float(course) <= 0:
                return f"{prefix}已关闭。"
            if angle:
                return f"{prefix}已打开{course}%，角度{angle}°。"
            return f"{prefix}已打开{course}%。"
        state = "已打开" if self._on(target) else "已关闭"
        return f"{prefix}{state}，角度{angle}°。"

    def _climate_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        if not self._on(target):
            return f"{prefix}已关闭。"
        temp = self._number_text(target, "targetTemp")
        mode = self._value_text(target, "mode")
        speed = self._value_text(target, "speed")
        parts: list[str] = []
        if temp:
            parts.append(f"温度{temp}℃")
        if mode:
            parts.append(f"模式为{mode}")
        if speed:
            parts.append(f"风速为{speed}")
        if not parts:
            return f"{prefix}已打开。"
        return prefix + "、".join(parts) + "。"

    def _heating_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        if not self._on(target):
            return f"{prefix}已关闭。"
        temp = self._number_text(target, "targetTemp")
        if not temp:
            return f"{prefix}已打开。"
        return f"{prefix}温度为{temp}℃。"

    def _air_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        if not self._on(target):
            return f"{prefix}已关闭。"
        speed = self._value_text(target, "speed")
        if not speed:
            return f"{prefix}已打开。"
        spec = target.capability.spec("speed")
        unit = spec.unit if spec is not None else ""
        return f"{prefix}风速为{speed}{unit}。"

    def _media_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        if not self._on(target):
            return f"{prefix}已关闭。"
        volume = self._number_text(target, "volume")
        source = self._value_text(target, "source")
        parts: list[str] = []
        if volume:
            parts.append(f"音量为{volume}%")
        if source:
            parts.append(f"音源为{source}")
        if not parts:
            return f"{prefix}已打开。"
        return prefix + "、".join(parts) + "。"

    def _sensor_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        spec = target.capability.spec("value")
        value = self._number_text(target, "value")
        if not value:
            return f"{prefix}暂时读取不到。"
        abbr = _sensor_abbr(target)
        unit = spec.unit if spec is not None else ""
        if not abbr:
            return f"{prefix}数值为{value}{unit}。"
        return f"{prefix}{abbr}值为{value}{unit}。"

    def _status_query_reply(self, target: VoiceEntity, prefix: str) -> str:
        state = str(target.state.get("state") or "")
        if target.domain == "scene":
            return f"{prefix}最近一次执行于{_moment_label(state)}。"
        return f"{prefix}状态为{_QUERY_STATE.get(state, _state_label(state))}。"

    def _group_query_reply(
        self, specs: list[tuple[VoiceEntity, FunctionSpec]], slots: VoiceSlots
    ) -> str:
        scope = self._scope_prefix(slots)
        type_label = _type_label(slots.device_type)
        on = [target for target, _ in specs if self._on(target)]
        return (
            f"查询到：{scope}下有{len(specs)}个{type_label}设备，"
            f"其中已打开{len(on)}个设备。"
        )

    # ---------- helpers ----------

    def _position_value(
        self, capability: DeviceCapability, slots: VoiceSlots
    ) -> float | None:
        """“窗帘打开一半/打开 30%”落到行程而不是单纯开合。"""
        if slots.op != "state" or slots.value is None:
            return None
        if not capability.supports("course"):
            return None
        if slots.enum not in ("on", "open") and slots.function != "course":
            return None
        return slots.value * 100 if slots.value <= 1 else slots.value

    def _function_candidates(
        self, capability: DeviceCapability, slots: VoiceSlots
    ) -> list[str]:
        explicit = slots.function
        if explicit:
            if capability.supports(explicit):
                return [explicit]
            if explicit in ("coolTemp", "warmTemp") and capability.supports("colorTemp"):
                return ["colorTemp"]
            return []
        candidates = list(dict.fromkeys(slots.candidates))
        if not candidates:
            if slots.op == "set" and slots.value is not None:
                candidates = _value_candidates(slots.value)
            else:
                candidates = list(DEFAULT_FUNCTIONS.get(slots.op, ()))
        supported = [name for name in candidates if capability.supports(name)]
        if not supported and slots.op == "state" and capability.supports("status"):
            supported = ["status"]
        if not supported and slots.kind == "query":
            supported = [
                name for name in DEFAULT_FUNCTIONS["query"] if capability.supports(name)
            ]
        return list(dict.fromkeys(supported))

    def _resolve_function(
        self, capability: DeviceCapability, slots: VoiceSlots
    ) -> str | None:
        names = self._function_candidates(capability, slots)
        if len(names) == 1:
            return names[0]
        if not names:
            return None
        preference = DOMAIN_PREFERENCE.get(capability.domain, ())
        for name in preference:
            if name in names:
                return name
        return names[0]

    def _capability(
        self, entity_id: str, state: dict[str, Any], name: str
    ) -> DeviceCapability:
        profile = self._profiles.get(entity_id)
        capability = build_capability(
            entity_id,
            state,
            name=name,
            area_id=self._area_of(entity_id),
            profile=profile,
        )
        if profile and profile.get("device_type"):
            capability = DeviceCapability(
                entity_id=capability.entity_id,
                domain=capability.domain,
                device_type=str(profile["device_type"]),
                name=capability.name,
                area_id=capability.area_id,
                functions=capability.functions,
            )
        return capability

    def _area_of(self, entity_id: str) -> str | None:
        if self._registry is None:
            return None
        area_of = getattr(self._registry, "area_of", None)
        return area_of(entity_id) if callable(area_of) else None

    def _area_name(self, area_id: str | None) -> str:
        if not area_id or self._registry is None:
            return ""
        area_name = getattr(self._registry, "area_name", None)
        return (area_name(area_id) if callable(area_name) else None) or ""

    def _scope_prefix(self, slots: VoiceSlots) -> str:
        floor_name = self._floor_name(slots.floor_id)
        if slots.area_text:
            return slots.area_text
        return floor_name or "全屋"

    def _area_floor_id(self, area_id: str) -> str | None:
        area = (getattr(self._registry, "areas", {}) or {}).get(area_id) or {}
        return area.get("floor_id") or None

    def _visible(self, entity_id: str, view_patterns: list[str] | None) -> bool:
        if view_patterns is None:
            return True
        return any(fnmatch.fnmatchcase(entity_id, pattern) for pattern in view_patterns)

    def _permission_denied_reply(
        self,
        slots: VoiceSlots,
        states: dict[str, dict],
        resolver: EntityResolver,
        member,
        view_patterns: list[str] | None,
    ) -> str | None:
        """设备在 HA 里确实存在，只是这位成员看不到时，别再说「没有这个设备」。"""
        if member is None or getattr(member, "role", None) == "admin":
            return None
        if view_patterns is None:
            return None
        label = ""
        if slots.target_kind == "type":
            hidden = 0
            for entity_id, state in states.items():
                if self._visible(entity_id, view_patterns):
                    continue
                attributes = (state or {}).get("attributes") or {}
                name = str(attributes.get("friendly_name") or entity_id)
                if self._capability(entity_id, state, name).device_type != slots.device_type:
                    continue
                hidden += 1
            if not hidden:
                return None
            label = f"{_type_label(slots.device_type)}类设备"
        elif slots.target_kind == "entity":
            resolution = resolver.resolve(
                slots.target_text,
                None,
                states,
                member=None,
                view_patterns=None,
                want_control=False,
            )
            if resolution.status == "none" or not resolution.entities:
                return None
            label = slots.target_text
        else:
            return None
        return (
            f"你没有{label}的查看权限，所以无法执行。"
            "请联系管理员在「配置 → 成员 → 设备权限」里授权。"
        )

    def _missing_scope_reply(self, slots: VoiceSlots) -> str:
        area = slots.area_text or ""
        floor_name = self._floor_name(slots.floor_id)
        if area and self._registry is not None:
            area_id = self._area_id_for_text(area)
            if area_id is None or (
                slots.floor_id
                and self._area_floor_id(area_id)
                and self._area_floor_id(area_id) != slots.floor_id
            ):
                if floor_name:
                    return f"当前家庭{floor_name}下没有{area}这个区域。"
                return f"当前家庭没有{area}这个区域。"
        if slots.target_kind == "type":
            return f"在{self._scope_prefix(slots)}里面没有找到{_type_label(slots.device_type)}类设备。"
        place = f"{floor_name}{area}"
        if place:
            return f"在{place}里面没有找到{slots.target_text}。"
        return f"当前家庭没有找到{slots.target_text}。"

    def _clarify_result(
        self, slots: VoiceSlots, candidates: list[EntityRef]
    ) -> VoiceResult:
        slots.clarify = candidates
        lines = [f"{index + 1} {ref.name}" for index, ref in enumerate(candidates)]
        return VoiceResult(
            status="clarify",
            reply="找到多个设备：" + "、".join(lines) + "。请回复序号或完整名称。",
            slots=slots,
            candidates=candidates,
        )

    def _reject(self, slots: VoiceSlots, reply: str) -> VoiceResult:
        return VoiceResult(status="reject", reply=reply, slots=slots, targets=slots.targets)

    # ---------- matching ----------

    def _find_first(
        self, text: str, spans: "_Spans", mapping: dict[str, Any]
    ) -> tuple[str, Any, int, int] | None:
        for index in range(len(text)):
            if not spans.free(index):
                continue
            limit = min(self._max_len, len(text) - index)
            for length in range(limit, 0, -1):
                key = text[index : index + length]
                if key in mapping:
                    return key, mapping[key], index, index + length
        return None

    def _find_combined(
        self,
        text: str,
        spans: "_Spans",
        items: list[tuple[str, str, str]],
    ) -> tuple[str, str, str, int, int] | None:
        best: tuple[tuple[int, int, int], str, str, str] | None = None
        for key, kind, payload in items:
            start = text.find(key)
            while start >= 0:
                if spans.free(start, start + len(key)):
                    rank = (start, -len(key), 0 if kind == "name" else 1)
                    if best is None or rank < best[0]:
                        best = (rank, key, kind, payload)
                    break
                start = text.find(key, start + 1)
        if best is None:
            return None
        rank, key, kind, payload = best
        start = rank[0]
        return key, kind, payload, start, start + len(key)

    def _find_stem(
        self, text: str, spans: "_Spans"
    ) -> tuple[str, int, tuple[str, ...], int, int] | None:
        found = self._find_first(text, spans, self._lexicon.stems)
        if found is None:
            return None
        key, (sign, functions), start, end = found
        rest = text[end : end + 6]
        if (
            any(char.isdigit() for char in rest)
            or any(word in rest for word in self._lexicon.little_words)
            or rest.startswith("点")
        ):
            return key, sign, tuple(functions), start, end
        return None

    def _find_quantity(
        self, text: str, spans: "_Spans"
    ) -> tuple[float, str, tuple[str, ...], int, int] | None:
        candidates: list[tuple[int, int, float, str, tuple[str, ...]]] = []
        for index in range(len(text)):
            if not spans.free(index) or _number_tail(text, index):
                continue
            prefix = None
            limit = min(self._max_len, len(text) - index)
            for length in range(limit, 0, -1):
                key = text[index : index + length]
                if key in self._lexicon.units:
                    canonical, functions = self._lexicon.units[key]
                    if canonical == "%":
                        prefix = (length, functions)
                    break
            if prefix is not None:
                consumed, functions = prefix
                inner = self._quantity_at(text, index + consumed, spans)
                if inner is not None:
                    value, end, _unit, _hint = inner
                    candidates.append((index, end, value, "%", functions))
                    continue
            inner = self._quantity_at(text, index, spans)
            if inner is not None:
                value, end, unit, functions = inner
                candidates.append((index, end, value, unit, functions))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        merged: list[tuple[int, int, float, str, tuple[str, ...]]] = []
        for candidate in candidates:
            if merged and candidate[0] < merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (
                    previous[0],
                    max(previous[1], candidate[1]),
                    previous[2],
                    previous[3],
                    previous[4],
                )
                continue
            merged.append(candidate)
        index, end, value, unit, functions = merged[-1]
        return value, unit, functions, index, end

    def _quantity_at(
        self, text: str, index: int, spans: "_Spans"
    ) -> tuple[float, int, str, tuple[str, ...]] | None:
        if index >= len(text) or not spans.free(index):
            return None
        best: tuple[float, int, str, tuple[str, ...]] | None = None
        limit = min(self._max_len, len(text) - index)
        for length in range(limit, 0, -1):
            key = text[index : index + length]
            if key in self._lexicon.values and spans.free(index, index + length):
                value, hint = self._lexicon.values[key]
                end = index + length
                if _bare_numeral_before_little(text, index, end):
                    continue
                unit, functions, end = self._unit_after(text, end, spans, hint)
                best = (value, end, unit, functions)
                break
        match = _NUMBER_TEXT_RE.match(text, index)
        if match and spans.free(index, match.end()):
            try:
                value = float(match.group(0))
            except ValueError:
                value = None
            if value is not None and not _bare_numeral_before_little(
                text, index, match.end()
            ):
                end = match.end()
                unit, functions, end = self._unit_after(text, end, spans, "")
                if best is None or end > best[1]:
                    best = (value, end, unit, functions)
        return best

    def _unit_after(
        self, text: str, index: int, spans: "_Spans", hint: str
    ) -> tuple[str, tuple[str, ...], int]:
        if index < len(text) and spans.free(index):
            limit = min(self._max_len, len(text) - index)
            for length in range(limit, 0, -1):
                key = text[index : index + length]
                if key in self._lexicon.units and spans.free(index, index + length):
                    canonical, functions = self._lexicon.units[key]
                    return canonical or hint, tuple(functions), index + length
        return hint, (), index


class _Spans:
    def __init__(self, length: int) -> None:
        self._covered = [False] * length

    def add(self, start: int, end: int) -> None:
        for index in range(max(0, start), min(len(self._covered), end)):
            self._covered[index] = True

    def free(self, start: int, end: int | None = None) -> bool:
        if end is None:
            return 0 <= start < len(self._covered) and not self._covered[start]
        if start < 0 or end > len(self._covered):
            return False
        return all(not self._covered[index] for index in range(start, end))


def _little_amount(function: str, spec: FunctionSpec) -> float:
    if function in LITTLE_ABSOLUTE:
        return LITTLE_ABSOLUTE[function]
    step = spec.step or DEFAULT_STEPS.get(function) or 1.0
    return max(step * LITTLE_STEP_RATIO, 1.0)


_CN_NUMERAL_CHARS = set("零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟")


def _number_tail(text: str, index: int) -> bool:
    """数字中段不单独成量：50% 里的 0%、三十里的十不应被重复识别。"""
    if index <= 0:
        return False
    previous = text[index - 1]
    return (
        previous.isdigit()
        or previous in (".",)
        or previous in _CN_NUMERAL_CHARS
        or (previous.isascii() and previous.isalpha())
    )


def _bare_numeral_before_little(text: str, start: int, end: int) -> bool:
    """“一点/一点点”里的“一”不是数值。"""
    if text[start:end] not in ("一", "1"):
        return False
    return end < len(text) and text[end] in "点些丢下许"


def _unit_compatible(given: str, expected: str) -> bool:
    if not given or not expected:
        return True
    if given == expected:
        return True
    return given in UNIT_COMPATIBLE.get(expected, (expected,))


def _value_candidates(value: float) -> list[str]:
    if value < 0:
        return ["targetTemp"]
    if value <= 100:
        return ["brightness", "course", "volume", "angle", "targetHumidity", "targetTemp"]
    if value < 2000:
        return ["targetTemp", "colorTemp"]
    return ["colorTemp"]


def _status_service(state: str, domain: str) -> str | None:
    if state == "execute":
        return {
            "scene": "turn_on",
            "script": "turn_on",
            "automation": "trigger",
        }.get(domain)
    if state == "trigger":
        return {"automation": "trigger"}.get(domain)
    if state == "stop":
        return {"script": "turn_off", "automation": "turn_off"}.get(domain)
    if state == "on":
        return {
            "cover": "open_cover",
            "lock": "unlock",
            "light": "turn_on",
            "switch": "turn_on",
            "fan": "turn_on",
            "humidifier": "turn_on",
            "climate": "turn_on",
            "media_player": "turn_on",
            "water_heater": "turn_on",
            "vacuum": "turn_on",
            "scene": "turn_on",
            "script": "turn_on",
            "automation": "turn_on",
        }.get(domain)
    if state == "off":
        return {
            "cover": "close_cover",
            "lock": "lock",
            "light": "turn_off",
            "switch": "turn_off",
            "fan": "turn_off",
            "humidifier": "turn_off",
            "climate": "turn_off",
            "media_player": "turn_off",
            "water_heater": "turn_off",
            "vacuum": "turn_off",
            "script": "turn_off",
            "automation": "turn_off",
        }.get(domain)
    if state == "open":
        return "open_cover" if domain == "cover" else None
    if state == "closed":
        return "close_cover" if domain == "cover" else None
    if state == "locked":
        return "lock" if domain == "lock" else None
    if state == "unlocked":
        return "unlock" if domain == "lock" else None
    return None


def _state_verb(canonical: str, term: str) -> str:
    return {
        "on": "打开",
        "off": "关闭",
        "open": "打开",
        "closed": "关闭",
        "locked": "锁定",
        "unlocked": "解锁",
        "execute": "执行",
        "trigger": "触发",
        "stop": "停止",
    }.get(canonical, term or canonical)


def _state_label(value: str) -> str:
    return {
        "on": "开着的",
        "off": "关着的",
        "open": "开着的",
        "closed": "关着的",
        "locked": "已上锁",
        "unlocked": "已解锁",
        "unavailable": "不可用",
        "unknown": "状态未知",
    }.get(value, value or "未知")


def _rgb_tuple(canonical: str) -> tuple[int, int, int] | None:
    parts = canonical.split("/")
    if len(parts) != 3:
        return None
    try:
        red, green, blue = (int(float(part)) for part in parts)
    except ValueError:
        return None
    if any(value < 0 or value > 255 for value in (red, green, blue)):
        return None
    return red, green, blue


def _place(target: VoiceEntity) -> str:
    parts: list[str] = []
    for value in (target.floor_name, target.area_name):
        if value and value not in target.name and value not in parts:
            parts.append(value)
    return "".join(parts)


def _type_label(device_type: str) -> str:
    return DEVICE_TYPE_LABELS.get(device_type, device_type or "设备")


_QUERY_STATE = {
    "on": "打开",
    "off": "关闭",
    "open": "打开",
    "closed": "关闭",
    "locked": "已上锁",
    "unlocked": "已解锁",
    "unavailable": "不可用",
    "unknown": "未知",
}

SENSOR_ABBR = {
    "temperature": "温度",
    "humidity": "湿度",
    "illuminance": "照度",
    "co2": "二氧化碳",
    "pm25": "PM2.5",
    "pm10": "PM10",
    "tvoc": "TVOC",
    "voc": "VOC",
    "noise": "噪声",
    "battery": "电量",
    "smoke": "烟雾",
    "gas": "燃气",
    "moisture": "水浸",
    "motion": "人体",
    "door": "门磁",
    "window": "窗磁",
    "power": "功率",
    "energy": "能耗",
    "voltage": "电压",
    "current": "电流",
}


def _sensor_abbr(target: VoiceEntity) -> str:
    device_class = str((target.state.get("attributes") or {}).get("device_class") or "")
    return SENSOR_ABBR.get(device_class, "")


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:g}"


def _updated_label(state: dict[str, Any]) -> str:
    raw = state.get("last_updated") or state.get("last_changed")
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%H:%M:%S")


def _moment_label(raw: str) -> str:
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _residue_ok(text: str, spans: "_Spans") -> bool:
    leftover = "".join(
        char for index, char in enumerate(text) if spans.free(index)
    )
    for token in sorted(ALLOWED_RESIDUE, key=len, reverse=True):
        leftover = leftover.replace(token, "")
    return not leftover.strip()
