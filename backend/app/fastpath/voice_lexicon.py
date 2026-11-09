from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LEXICONS = (
    "floorName",
    "areaName",
    "deviceName",
    "sceneName",
    "function",
    "enum",
    "value",
    "max",
    "min",
    "increase",
    "decrease",
    "query",
    "little",
    "unit",
    "deviceType",
)

LEXICON_BY_NAME = {
    "楼层名称": "floorName",
    "区域名称": "areaName",
    "设备名称": "deviceName",
    "场景名称": "sceneName",
    "功能名称": "function",
    "枚举值": "enum",
    "属性数值": "value",
    "最大值": "max",
    "最小值": "min",
    "相对调高": "increase",
    "相对调低": "decrease",
    "查询": "query",
    "一点": "little",
    "单位": "unit",
    "设备类型": "deviceType",
}

_ALIAS_SPLIT = re.compile(r"[/、]")
_FUNCTION_SPLIT = re.compile(r"[,，、]")
_NUMBER_RE = re.compile(r"^(?:负|-)?[0-9]+(?:\.[0-9]+)?%?$")

MAX_ERRORS = 200


@dataclass(frozen=True)
class LexiconEntry:
    lexicon: str
    term: str
    canonical: str
    alias: str
    functions: tuple[str, ...] = ()
    unit: str = ""
    numeric_value: float | None = None
    entry_type: int = 1
    source: str = "xlsx"


@dataclass
class ParsedWorkbook:
    entries: list[LexiconEntry] = field(default_factory=list)
    source_rows: dict[str, int] = field(default_factory=dict)
    alias_rows: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    sha256: str = ""

    @property
    def error_count(self) -> int:
        return len(self.errors)


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_number(value: Any) -> float | None:
    text = cell_text(value)
    if not text or not _NUMBER_RE.match(text):
        return None
    negative = text.startswith(("负", "-"))
    body = text[1:] if negative else text
    try:
        number = float(body.rstrip("%"))
    except ValueError:
        return None
    return -number if negative else number


def split_aliases(value: Any) -> list[str]:
    text = cell_text(value)
    if not text:
        return []
    result: list[str] = []
    for part in _ALIAS_SPLIT.split(text):
        alias = part.strip()
        if alias and alias not in result:
            result.append(alias)
    return result


def split_functions(value: Any) -> tuple[str, ...]:
    text = cell_text(value)
    if not text:
        return ()
    return tuple(item.strip() for item in _FUNCTION_SPLIT.split(text) if item.strip())


def _canonical(
    lexicon: str, term: str, identifier: str, numeric: float | None
) -> str:
    if lexicon == "floorName":
        return identifier or term
    if lexicon in ("areaName", "sceneName", "deviceName"):
        return term
    if lexicon in ("function", "enum", "deviceType"):
        return identifier or term
    if lexicon == "unit":
        return term if identifier in ("", "model") else identifier
    if lexicon == "max":
        return "max"
    if lexicon == "min":
        return "min"
    if lexicon in ("increase", "decrease"):
        return identifier if numeric is not None else lexicon
    if lexicon == "value":
        return identifier or term
    if lexicon == "little":
        return "little"
    if lexicon == "query":
        return "query"
    return term


def parse_row(row: Iterable[Any]) -> list[LexiconEntry]:
    values = list(row) + [None] * 6
    lexicon_name = cell_text(values[0])
    lexicon = LEXICON_BY_NAME.get(lexicon_name)
    if lexicon is None:
        raise ValueError(f"unknown lexicon: {lexicon_name or '<empty>'}")
    term = cell_text(values[1])
    if not term:
        raise ValueError("empty term")
    identifier = cell_text(values[3])
    numeric = parse_number(values[3])
    functions = split_functions(values[2])
    try:
        entry_type = int(float(cell_text(values[5]) or 1))
    except ValueError:
        entry_type = 1
    if entry_type not in (1, 2):
        entry_type = 1
    canonical = _canonical(lexicon, term, identifier, numeric)
    unit = canonical if lexicon == "unit" else ""

    aliases = [term]
    for alias in split_aliases(values[4]):
        if alias not in aliases:
            aliases.append(alias)

    return [
        LexiconEntry(
            lexicon=lexicon,
            term=term,
            canonical=canonical,
            alias=alias,
            functions=functions,
            unit=unit,
            numeric_value=numeric,
            entry_type=entry_type,
            source="xlsx",
        )
        for alias in aliases
    ]


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    from openpyxl import load_workbook

    path = Path(path)
    raw = path.read_bytes()
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    next(rows, None)

    result = ParsedWorkbook(sha256=hashlib.sha256(raw).hexdigest())
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for index, row in enumerate(rows, start=2):
        if not row or all(value in (None, "") for value in row):
            continue
        result.total_rows += 1
        try:
            entries = parse_row(row)
        except ValueError as exc:
            if len(result.errors) < MAX_ERRORS:
                result.errors.append({"row": index, "error": str(exc)})
            continue
        lexicon = entries[0].lexicon
        result.source_rows[lexicon] = result.source_rows.get(lexicon, 0) + 1
        for entry in entries:
            key = (entry.lexicon, entry.alias)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            result.entries.append(entry)
            result.alias_rows[lexicon] = result.alias_rows.get(lexicon, 0) + 1
    if duplicates:
        result.errors.append({"row": 0, "error": f"duplicate aliases skipped: {duplicates}"})
    return result
