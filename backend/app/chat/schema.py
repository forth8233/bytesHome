from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["get_state", "call_service", "create_automation"]
    args: dict[str, Any] = Field(default_factory=dict)


class ChatDecision(BaseModel):
    """对话层的结构化输出；工具调用协议自建，避免绑定某家 function calling。"""

    model_config = ConfigDict(extra="forbid")

    reply: str = ""
    tool_calls: list[ChatToolCall] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _require_content(self) -> "ChatDecision":
        if not self.reply.strip() and not self.tool_calls:
            raise ValueError("reply and tool_calls cannot both be empty")
        return self


class MemoryExtractionItem(BaseModel):
    """从对话中抽取沉淀的长期偏好条目。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["declare", "withdraw"] = "declare"
    key: str = ""
    value: str = ""
    value_type: Literal[
        "text", "number", "number_range", "boolean", "string_enum", "json"
    ] = "text"
    value_json: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    scope: Literal["member", "home", "global"] = "member"


class MemoryExtraction(BaseModel):
    """对话慢路径抽取：本轮是否表达了可沉淀的长期偏好。"""

    model_config = ConfigDict(extra="forbid")

    memory_items: list[MemoryExtractionItem] = Field(default_factory=list, max_length=5)
