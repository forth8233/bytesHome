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
