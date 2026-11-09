from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.fastpath.normalize import normalize

DOMAIN_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lock", ("门锁", "锁")),
    ("light", ("灯光", "灯")),
    ("climate", ("空调", "地暖", "暖气")),
    ("cover", ("窗帘", "卷帘", "百叶窗")),
    ("media_player", ("电视", "音箱", "音响", "音乐")),
    ("fan", ("风扇", "电扇")),
    ("humidifier", ("加湿器", "加湿")),
    ("switch", ("插座", "开关")),
)

DOMAIN_ALIASES = {
    "light": "light",
    "lights": "light",
    "climate": "climate",
    "ac": "climate",
    "cover": "cover",
    "curtain": "cover",
    "media_player": "media_player",
    "media": "media_player",
    "fan": "fan",
    "humidifier": "humidifier",
    "switch": "switch",
    "lock": "lock",
}

OFF_VERBS = ("关闭", "关掉", "关上", "熄灭", "关了", "关")
ON_VERBS = ("打开", "开启", "启动", "拉开", "开了", "开")
OFF_ON = tuple(sorted(OFF_VERBS + ON_VERBS, key=len, reverse=True))

RELATIVE_WORDS = ("太亮", "太暗", "亮一点", "暗一点", "高一点", "低一点", "大一点", "小一点", "一点")
MULTI_WORDS = ("并", "然后", "同时", "顺便")

TEMP_RE = re.compile(
    r"(?:把|将)?(?P<target>.*?)(?:调到|调至|设为|设成|设置到|设置成|调成|改成)(?P<temp>\d+(?:\.\d+)?)度?"
)
TEMP_SUFFIX_RE = re.compile(r"(?:把|将)?(?P<target>.*?)(?P<temp>\d+(?:\.\d+)?)度")
MODE_RE = re.compile(r"(?:把|将)?(?P<target>.*?)(?P<mode>制冷|制热|除湿|送风|自动)(?:模式)?")
BRIGHTNESS_RE = re.compile(
    r"(?:把|将)?(?P<target>.*?)(?:亮度)?(?:调到|调至|设为|设成|调成|改成)?(?P<value>\d{1,3})%"
)

MODE_VALUES = {
    "制冷": "cool",
    "制热": "heat",
    "除湿": "dry",
    "送风": "fan_only",
    "自动": "auto",
}

QUERY_TEMP_RE = re.compile(
    r"(?:把|将)?(?P<target>.*?)(?:现在|当前)?(?:几度|多少度|温度(?:是)?多少|温度几度|室温)"
)
QUERY_HUMIDITY_RE = re.compile(r"(?:把|将)?(?P<target>.*?)(?:现在)?湿度(?:是)?(?:多少|几)?")
QUERY_STATE_RE = re.compile(
    r"(?:把|将)?(?P<target>.*?)(?:开着吗|开了吗|关着吗|关了吗|亮着吗|亮着没|开着没|关了没|状态(?:是什么|怎么样|如何)?)"
)

ALL_WORDS = ("所有", "全部", "全屋", "每个", "都")

UNIT_BY_DOMAIN = {
    "light": "盏灯",
    "climate": "台空调",
    "cover": "幅窗帘",
    "media_player": "台设备",
    "fan": "台风扇",
    "humidifier": "台加湿器",
    "switch": "个开关",
    "lock": "把锁",
}


@dataclass
class ParsedIntent:
    kind: str
    op: str | None = None
    domain: str | None = None
    service: str | None = None
    target: str = ""
    data: dict = field(default_factory=dict)
    explicit_all: bool = False
    query: str | None = None
    scene: str | None = None
    original: str = ""


def detect_domain(text: str) -> str | None:
    for domain, words in DOMAIN_WORDS:
        if any(word in text for word in words):
            return domain
    return None


def strip_domain_words(text: str) -> str:
    result = text
    for _, words in DOMAIN_WORDS:
        for word in words:
            result = result.replace(word, "")
    return result.replace("的", "").replace("里", "").strip()


def has_all_word(text: str) -> bool:
    return any(word in text for word in ALL_WORDS)


def service_for(op: str, domain: str | None) -> tuple[str | None, str | None]:
    if op == "on":
        return domain, {
            "cover": "open_cover",
            "light": "turn_on",
            "switch": "turn_on",
            "fan": "turn_on",
            "humidifier": "turn_on",
            "climate": "turn_on",
            "media_player": "turn_on",
            "lock": "unlock",
        }.get(domain or "")
    if op == "off":
        return domain, {
            "cover": "close_cover",
            "light": "turn_off",
            "switch": "turn_off",
            "fan": "turn_off",
            "humidifier": "turn_off",
            "climate": "turn_off",
            "media_player": "turn_off",
            "lock": "lock",
        }.get(domain or "")
    if op == "pause":
        return domain, "media_pause" if domain == "media_player" else None
    if op == "play":
        return domain, "media_play" if domain == "media_player" else None
    if op == "unlock":
        return domain, "unlock"
    if op == "lock":
        return domain, "lock"
    return domain, None


def _clean_target(text: str) -> str:
    return text.strip().lstrip("把将").strip()


def parse_slash(text: str) -> ParsedIntent | None:
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    head = parts[0].lower()
    if head in ("status", "state") and len(parts) >= 2:
        target = normalize("".join(parts[1:]))
        return ParsedIntent(kind="query", query="state", target=target, original=text)
    if head == "scene" and len(parts) >= 2:
        name = normalize("".join(parts[1:]))
        return ParsedIntent(kind="scene", scene=name, original=text)

    domain = DOMAIN_ALIASES.get(head)
    if domain is None or len(parts) < 2:
        return None
    action = parts[1].lower()
    target = normalize("".join(parts[2:]))
    op_map = {
        "on": "on",
        "off": "off",
        "open": "on",
        "close": "off",
        "unlock": "unlock",
        "lock": "lock",
        "pause": "pause",
        "play": "play",
    }
    if action in ("temp", "temperature") and len(parts) >= 3:
        try:
            value = float(parts[2])
        except ValueError:
            return None
        target = normalize("".join(parts[3:]))
        return ParsedIntent(
            kind="control",
            op="set_temp",
            domain="climate",
            service="set_temperature",
            target=target,
            data={"temperature": value},
            original=text,
        )
    op = op_map.get(action)
    if op is None:
        return None
    resolved_domain, service = service_for(op, domain)
    if service is None:
        return None
    return ParsedIntent(
        kind="control",
        op=op,
        domain=resolved_domain,
        service=service,
        target=target,
        explicit_all=has_all_word(target),
        original=text,
    )


def parse_intent(text: str) -> ParsedIntent | None:
    """把一句中文解析成确定性意图；无法确定时返回 None 交给慢思考。"""
    original = text
    raw = (text or "").strip()
    if raw.startswith("/"):
        return parse_slash(raw)
    text = normalize(text)
    if not text:
        return None
    if len(text) > 64:
        return None
    if any(word in text for word in RELATIVE_WORDS):
        return None
    if any(word in text for word in MULTI_WORDS):
        return None

    query = _parse_query(text, original)
    if query is not None:
        return query

    header = _parse_climate_or_brightness(text, original)
    if header is not None:
        return header

    special = _parse_special(text, original)
    if special is not None:
        return special

    return _parse_generic(text, original)


def _parse_query(text: str, original: str) -> ParsedIntent | None:
    match = QUERY_STATE_RE.search(text)
    if match:
        target = _clean_target(match.group("target"))
        return ParsedIntent(
            kind="query",
            query="state",
            target=target,
            explicit_all=has_all_word(target),
            original=original,
        )
    match = QUERY_HUMIDITY_RE.search(text)
    if match:
        target = _clean_target(match.group("target"))
        return ParsedIntent(kind="query", query="humidity", target=target, original=original)
    match = QUERY_TEMP_RE.search(text)
    if match:
        target = _clean_target(match.group("target"))
        target = target.replace("室内", "").replace("屋里", "")
        return ParsedIntent(kind="query", query="temperature", target=target, original=original)
    return None


def _parse_climate_or_brightness(text: str, original: str) -> ParsedIntent | None:
    if "%" in text or "亮度" in text:
        match = BRIGHTNESS_RE.search(text)
        if match:
            value = int(match.group("value"))
            if 1 <= value <= 100:
                target = _clean_target(match.group("target"))
                return ParsedIntent(
                    kind="control",
                    op="set_brightness",
                    domain="light",
                    service="turn_on",
                    target=target,
                    data={"brightness_pct": value},
                    original=original,
                )
        return None

    match = TEMP_RE.search(text) or TEMP_SUFFIX_RE.search(text)
    if match and ("空调" in text or "地暖" in text or "调到" in text or "设为" in text):
        temp = float(match.group("temp"))
        target = _clean_target(match.group("target"))
        return ParsedIntent(
            kind="control",
            op="set_temp",
            domain="climate",
            service="set_temperature",
            target=target,
            data={"temperature": temp},
            original=original,
        )

    match = MODE_RE.search(text)
    if match and detect_domain(text) == "climate":
        target = _clean_target(match.group("target"))
        return ParsedIntent(
            kind="control",
            op="set_mode",
            domain="climate",
            service="set_hvac_mode",
            target=target,
            data={"hvac_mode": MODE_VALUES[match.group("mode")]},
            original=original,
        )
    return None


def _parse_special(text: str, original: str) -> ParsedIntent | None:
    if "开锁" in text or "解锁" in text:
        target = _clean_target(text.replace("开锁", "").replace("解锁", ""))
        return ParsedIntent(
            kind="control",
            op="unlock",
            domain="lock",
            service="unlock",
            target=target,
            original=original,
        )
    if "锁门" in text or "上锁" in text or text.endswith("锁上"):
        target = _clean_target(
            text.replace("锁门", "").replace("上锁", "").removesuffix("锁上")
        )
        return ParsedIntent(
            kind="control",
            op="lock",
            domain="lock",
            service="lock",
            target=target,
            original=original,
        )
    if "暂停" in text:
        domain = detect_domain(text)
        target = _clean_target(text.replace("暂停", ""))
        return ParsedIntent(
            kind="control",
            op="pause",
            domain=domain,
            service="media_pause",
            target=target,
            original=original,
        )
    if "继续播放" in text or "开始播放" in text:
        domain = detect_domain(text) or "media_player"
        target = _clean_target(
            text.replace("继续播放", "").replace("开始播放", "")
        )
        return ParsedIntent(
            kind="control",
            op="play",
            domain=domain,
            service="media_play",
            target=target,
            original=original,
        )
    return None


def _parse_generic(text: str, original: str) -> ParsedIntent | None:
    best: tuple[int, int, str] | None = None
    for verb in OFF_ON:
        index = text.find(verb)
        if index < 0:
            continue
        candidate = (index, -len(verb), verb)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None

    index, _, verb = best
    raw_target = _clean_target(text[:index] + text[index + len(verb) :])
    if not raw_target and len(text) > len(verb):
        raw_target = _clean_target(text[index + len(verb) :])
    domain = detect_domain(raw_target) or detect_domain(text)
    op = "off" if verb in OFF_VERBS else "on"
    resolved_domain, service = service_for(op, domain)
    return ParsedIntent(
        kind="control",
        op=op,
        domain=resolved_domain,
        service=service,
        target=raw_target,
        explicit_all=has_all_word(raw_target),
        original=original,
    )


def parse_choice(text: str) -> int | None:
    """澄清回复：第一个 / 第二个 / 2。"""
    normalized = normalize(text)
    if normalized in ("1", "2", "3"):
        return int(normalized) - 1
    ordinal = {"第一个": 0, "第二个": 1, "第三个": 2, "第一": 0, "第二": 1, "第三": 2}
    return ordinal.get(normalized)
