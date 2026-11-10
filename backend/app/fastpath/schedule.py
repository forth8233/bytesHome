from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.fastpath.grammar import MULTI_WORDS, RELATIVE_WORDS

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _cn_num(text: str) -> int | None:
    total = 0
    number = 0
    seen = False
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            seen = True
        elif char == "十":
            total += (number or 1) * 10
            number = 0
            seen = True
        else:
            return None
    return total + number if seen else None


def _num_value(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _cn_num(token)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_moment(hour: int, minute: int = 0) -> datetime:
    """今天该时刻；若已过则推到明天。换算成 UTC naive 返回。"""
    local = datetime.now().astimezone()
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 模糊时段与正则
# ---------------------------------------------------------------------------
# 时段词: label 用于展示; is_afternoon 决定 12h→24h 调整
FUZZY_PERIODS: dict[str, dict] = {
    "凌晨": {"hour": 4, "label": "凌晨", "afternoon": False},
    "清晨": {"hour": 6, "label": "清晨", "afternoon": False},
    "早晨": {"hour": 7, "label": "早晨", "afternoon": False},
    "早上": {"hour": 7, "label": "早上", "afternoon": False},
    "上午": {"hour": 9, "label": "上午", "afternoon": False},
    "中午": {"hour": 12, "label": "中午", "afternoon": False},
    "午后": {"hour": 13, "label": "午后", "afternoon": True},
    "下午": {"hour": 15, "label": "下午", "afternoon": True},
    "傍晚": {"hour": 18, "label": "傍晚", "afternoon": True},
    "晚上": {"hour": 21, "label": "晚上", "afternoon": True},
    "深夜": {"hour": 1, "label": "深夜", "afternoon": True},
}
_PERIOD_WORDS = tuple(FUZZY_PERIODS)
_PERIOD_RE = re.compile(
    "(?P<period>" + "|".join(re.escape(w) for w in sorted(_PERIOD_WORDS, key=len, reverse=True)) + ")"
)

_NUM = r"[0-9一二两三四五六七八九十]+"
_HOUR_RE = re.compile(rf"(?P<hour>{_NUM})点(?:(?P<min>{_NUM})分)?")
_CLOCK_RE = re.compile(r"(?P<hour>[01]?\d|2[0-3])[:：](?P<min>[0-5]\d)")

# 时间单位：优先中文；裸"分"刻意排除，避免与绝对时间"H点M分"冲突
_CN_UNITS = "分钟|小时|秒"
_LATIN_UNITS = r"(?:min|sec|hr|[msh])"
_UNIT_SECONDS = {
    # 中文
    "秒": 1, "分钟": 60, "小时": 3600,
    # 拉丁缩写
    "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600,
}

# 相对延时：N分钟后/小时/秒(后) 或 Ns/Nm/Nh 或 再过/过N分钟
_DELAY_RE = re.compile(
    rf"(?:再过|过|再)?(?P<num>{_NUM})\s*个?\s*(?P<unit>{_CN_UNITS}|{_LATIN_UNITS})(?:后|之后|以后)?"
)
# 时长：X分钟/小时/秒（跟在设备动作后的自动关）
_DURATION_RE = re.compile(
    rf"(?P<num>{_NUM})\s*个?\s*(?P<unit>{_CN_UNITS}|{_LATIN_UNITS})"
)

# 设备动作动词（多字优先，避免"开"误伤"每天"等）
_VERB_WORDS = ("打开", "开启", "启动", "拉开", "关了", "关闭", "关掉", "关上", "熄灭", "开了", "开", "关")
_VERB_RE = re.compile("|".join(re.escape(w) for w in _VERB_WORDS))

# 时间片段（剥离用；分钟部分可省略；与 _DELAY_RE 一致容忍空格）
_TIME_TOK_RE = re.compile(
    rf"(?:再过|过|再)?{_NUM}\s*个?\s*(?:{_CN_UNITS}|{_LATIN_UNITS})\s*(?:后|之后|以后)?"
    rf"|{'|'.join(re.escape(w) for w in _PERIOD_WORDS)}"
    rf"|{_NUM}点(?:{_NUM}分)?"
    r"|[01]?\d[:：][0-5]\d"
)


def _strip_time(text: str) -> str:
    cleaned = _TIME_TOK_RE.sub("", text)
    return cleaned.replace("，", ",").replace(",", " ").replace("  ", " ").strip()


def _has_action(text: str) -> bool:
    return bool(_VERB_RE.search(text))


def _has_verb_before(pos: int, text: str) -> bool:
    """文本 pos 之前是否已出现动作动词（用于区分 相对/时长）。"""
    return bool(_VERB_RE.search(text[:pos]))


def _period_adjust(period: str, hour: int) -> int:
    """带午别词的 12h 点数转 24h。"""
    slot = FUZZY_PERIODS[period]
    if not slot["afternoon"]:
        return hour
    if hour == 12:
        return 0  # 晚上12点 ≈ 午夜
    if hour < 12:
        return hour + 12
    return hour


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------
@dataclass
class ScheduleSpec:
    kind: str  # relative / absolute / fuzzy / duration
    title: str
    summary: str
    run_at: datetime | None = None   # UTC naive 计划执行时刻（non-duration）
    delay_seconds: int | None = None
    action_text: str = ""            # 剥离时间词后的动作文本
    auto_run_at: datetime | None = None  # duration：自动回切时刻（UTC naive）


def parse_schedule(text: str) -> ScheduleSpec | None:
    if not text:
        return None
    s = text.strip()
    if any(w in s for w in RELATIVE_WORDS) or any(w in s for w in MULTI_WORDS):
        return None

    def action() -> str:
        stripped = _strip_time(s)
        # 剥出后仍保留动作动词才算定时
        return stripped if _has_action(stripped) else ""

    # ---- 相对时间 N分钟后/小时后：时间词在动作之前 ----
    m = _DELAY_RE.search(s)
    if m:
        num = _num_value(m.group("num"))
        unit = m.group("unit").rstrip("后")
        if num is None or num <= 0 or unit not in _UNIT_SECONDS:
            return None
        # 若时间片段前已出现动作动词 → 属「开5分钟」时长语义，交给 duration 分支
        if _has_verb_before(m.start(), s):
            pass  # 落入下方时长分支
        else:
            delay = num * _UNIT_SECONDS[unit]
            a = action()
            if not a:
                return None
            return ScheduleSpec(
                kind="relative",
                title=s,
                summary=f"{num}{unit}后",
                run_at=utcnow() + timedelta(seconds=delay),
                delay_seconds=delay,
                action_text=a,
            )

    # ---- 模糊/绝对：先看是否带时段词（下午3点 = 15:00）----
    pm = _PERIOD_RE.search(s)
    if pm:
        slot = FUZZY_PERIODS[pm.group("period")]
        hour = slot["hour"]
        minute = 0
        hm = _HOUR_RE.search(s)
        if hm and hm.start() >= pm.end():
            h2 = _num_value(hm.group("hour"))
            raw_min = hm["min"]
            if h2 is not None:
                hour = _period_adjust(pm.group("period"), h2)
                minute = _num_value(raw_min) if raw_min else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        a = action()
        if not a:
            return None
        run_at = _next_moment(hour, minute)
        label = slot["label"] + (f"{hour:02d}:{minute:02d}" if hm else "")
        return ScheduleSpec(
            kind="fuzzy", title=s, summary=label, run_at=run_at, action_text=a
        )

    # ---- 绝对时间 H点(H分) / HH:MM ----
    hm = _HOUR_RE.search(s)
    if hm:
        hour = _num_value(hm.group("hour"))
        raw_min = hm["min"]
        minute = _num_value(raw_min) if raw_min else 0
        if hour is None or not (0 <= hour <= 23) or minute is None or not (0 <= minute <= 59):
            return None
        a = action()
        if not a:
            return None
        run_at = _next_moment(hour, minute)
        return ScheduleSpec(
            kind="absolute", title=s, summary=f"{hour:02d}:{minute:02d}",
            run_at=run_at, action_text=a,
        )

    cm = _CLOCK_RE.search(s)
    if cm:
        hour = int(cm.group("hour"))
        minute = int(cm.group("min"))
        a = action()
        if not a:
            return None
        run_at = _next_moment(hour, minute)
        return ScheduleSpec(
            kind="absolute", title=s, summary=f"{hour:02d}:{minute:02d}",
            run_at=run_at, action_text=a,
        )

    # ---- 时长 X分钟/小时：立即执行动作 + 到时自动关 ----
    dm = _DURATION_RE.search(s)
    if dm:
        num = _num_value(dm.group("num"))
        unit = dm.group("unit")
        if num is None or num <= 0 or unit not in _UNIT_SECONDS:
            return None
        a = action()
        if not a:
            return None
        delay = num * _UNIT_SECONDS[unit]
        return ScheduleSpec(
            kind="duration",
            title=s,
            summary=f"{num}{unit}后自动关",
            run_at=utcnow() + timedelta(seconds=delay),
            delay_seconds=delay,
            action_text=a,
            auto_run_at=utcnow() + timedelta(seconds=delay),
        )

    return None