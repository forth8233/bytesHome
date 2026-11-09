from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InboundMessage:
    """渠道适配器归一化后的入站消息。"""

    channel: str
    external_id: str  # 发送者在渠道内的唯一标识（飞书 open_id / 微信 wxid）
    chat_id: str  # 会话标识（飞书 chat_id / 微信 bot 会话）
    text: str
    display_name: str = ""
    message_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """要发回渠道的消息：纯文本，可附带结构化卡片。"""

    text: str
    card: dict[str, Any] | None = None


class ChannelAdapter(ABC):
    """渠道适配器接口：负责协议收发，业务逻辑全部交给 dispatcher。"""

    name: str = ""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id: str, message: OutboundMessage) -> bool:
        """发送消息；失败返回 False，不抛异常（避免打断主链路）。"""

    @abstractmethod
    def status(self) -> dict[str, Any]: ...


class ChannelError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
