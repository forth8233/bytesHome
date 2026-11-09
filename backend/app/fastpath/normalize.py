from __future__ import annotations

import re
import unicodedata

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUMBER_RE = re.compile(r"[零〇一二两三四五六七八九十百]+(?=度|档|%)")
_PUNCT_RE = re.compile(r"[\s,，。.!！?？、;；:：\"'“”‘’~～…]+")
_FILLERS = (
    "请帮我",
    "帮我",
    "帮忙",
    "麻烦你",
    "麻烦",
    "给我",
    "我想",
    "我要",
    "可不可以",
    "能不能",
    "可以",
    "请",
    "一下",
    "吧",
    "呢",
    "啊",
    "呀",
    "嘛",
    "谢谢",
    "你好",
    "嗯",
)


def _cn_to_int(text: str) -> int | None:
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
        elif char == "百":
            total += (number or 1) * 100
            number = 0
            seen = True
        else:
            return None
    return total + number if seen else None


def _replace_cn_numbers(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = _cn_to_int(match.group(0))
        return str(value) if value is not None else match.group(0)

    return _CN_NUMBER_RE.sub(repl, text)


def normalize(text: str) -> str:
    """全角转半角、去标点与口语填充词；保留数字与百分号。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _replace_cn_numbers(text)
    text = _PUNCT_RE.sub("", text)
    for filler in _FILLERS:
        text = text.replace(filler, "")
    return text.strip()


_VOICE_FILLERS = tuple(
    filler for filler in _FILLERS if filler not in ("一下", "可以")
)


def normalize_voice(text: str) -> str:
    """语音技能归一化：不转换中文数字，保留“一点/一下/百分之”等语义词。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _PUNCT_RE.sub("", text)
    for filler in _VOICE_FILLERS:
        text = text.replace(filler, "")
    return text.strip()

