"""解析 DeepSeek 原生 DSML 工具调用标记。

部分 DeepSeek 版本即使要求 JSON 输出，仍会返回形如
``<｜｜DSML｜｜invoke name="call_service">`` 的工具调用块。这里只做结构提取，
安全校验仍由 ActionGuard 与白名单完成。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.chat.schema import ChatDecision, ChatToolCall

_D = r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}"
_INVOKE_END = re.compile(r"</\s*" + _D + r"\s*invoke\s*>", re.S)
_TOOL_PARAM = re.compile(
    r"<\s*" + _D + r"\s*parameter\s+name=[\"']tool[\"'][^>]*>\s*([A-Za-z_]+)\s*<",
    re.S,
)
_ARGS_PARAM = re.compile(
    r"<\s*" + _D + r"\s*parameter\s+name=[\"']args[\"'][^>]*>(.*?)<",
    re.S,
)
_ALLOWED_TOOLS = {"get_state", "call_service", "create_automation"}


def _first_json_object(text: str) -> dict[str, Any] | None:
    from app.llm.json_parse import extract_json_object

    return extract_json_object(text)


def parse_dsml_tool_calls(text: str) -> list[dict[str, Any]] | None:
    if not text or "DSML" not in text:
        return None

    calls: list[dict[str, Any]] = []
    for block in _INVOKE_END.split(text):
        if not block.strip():
            continue
        tool_match = _TOOL_PARAM.search(block)
        if tool_match is None:
            if "{" not in block and "parameter" not in block:
                continue
            return None
        tool = tool_match.group(1).strip()
        if tool not in _ALLOWED_TOOLS:
            return None

        args_match = _ARGS_PARAM.search(block)
        args: dict[str, Any] | None = None
        if args_match is not None:
            try:
                candidate = json.loads(args_match.group(1).strip())
            except (ValueError, TypeError):
                candidate = None
            if isinstance(candidate, dict):
                args = candidate
        if args is None:
            args = _first_json_object(block)
        if args is None:
            return None
        calls.append({"tool": tool, "args": args})

    return calls or None


def parse_dsml_decision(text: str) -> ChatDecision | None:
    calls = parse_dsml_tool_calls(text)
    if not calls:
        return None
    try:
        return ChatDecision(
            reply="",
            tool_calls=[ChatToolCall(**call) for call in calls[:3]],
        )
    except ValueError:
        return None
