"""IM 渠道接入层：适配器负责协议收发，dispatcher 负责接入现有对话链路。"""

from app.channels.base import ChannelAdapter, ChannelError, InboundMessage, OutboundMessage
from app.channels.dispatcher import ChannelDispatcher

__all__ = [
    "ChannelAdapter",
    "ChannelDispatcher",
    "ChannelError",
    "InboundMessage",
    "OutboundMessage",
]
