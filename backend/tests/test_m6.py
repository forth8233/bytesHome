from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.chat.service import ChatService
from app.db.session import SessionFactory
from app.ha.adapter import HAAdapter
from app.ha.client_rest import HAClientError
from app.ha.client_ws import HAWebSocketClient
from app.llm.client import LLMResult
from app.safety.whitelist import WhitelistRules


class M6Config:
    def global_settings(self) -> dict:
        return {
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
            "feedback_reject_threshold": 2,
            "feedback_cooldown_seconds": 3600,
        }

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=("light.*",),
            service_allow=("light.turn_on", "light.turn_off"),
            param_rules={},
        )


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return []


class DownLLM:
    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        return LLMResult(
            ok=False,
            parsed_ok=False,
            provider="deepseek",
            model="down",
            error="cannot reach provider: timeout",
            duration_ms=1,
        )


class SlowLLM:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        await asyncio.sleep(self.delay)
        return LLMResult(
            ok=True,
            parsed_ok=True,
            provider="stub",
            model="slow",
            content="{}",
            data={"reply": "好的", "tool_calls": []},
            duration_ms=int(self.delay * 1000),
        )


class ReplyLLM:
    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        return LLMResult(
            ok=True,
            parsed_ok=True,
            provider="stub",
            model="reply",
            content="{}",
            data={"reply": "我在", "tool_calls": []},
            duration_ms=1,
        )


class SlowRest:
    async def call_service(self, domain, service, data):
        await asyncio.sleep(1)
        return []


def member() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin", role="admin")


def build_service(client_provider, **kwargs) -> tuple[ChatService, FakeAdapter]:
    adapter = FakeAdapter()
    service = ChatService(
        session_factory=SessionFactory,
        config=M6Config(),
        adapter=adapter,
        guard=None,
        proposals=None,
        client=client_provider,
        **kwargs,
    )
    return service, adapter


async def test_llm_down_slow_path_is_honest(client) -> None:
    service, adapter = build_service(DownLLM())
    result = await service.chat(member(), "把氛围弄舒服一点")

    assert result["route"] == "slow_path"
    assert result["route_status"] == "degraded"
    assert result["degraded"] is True
    assert "大模型暂时不可用" in result["reply"]
    assert "没有执行任何设备动作" in result["reply"]
    assert adapter.calls == []

    messages = await service.list_messages(member().id, result["session_id"])
    assert messages[-1]["route"]["route_status"] == "degraded"


async def test_slow_path_timeout_returns_honest_reply(client) -> None:
    service, adapter = build_service(SlowLLM(0.5), timeout_seconds=0.05)
    result = await service.chat(member(), "帮我看看现在家里适合做什么")

    assert result["route_status"] == "timeout"
    assert result["degraded"] is True
    assert "超时" in result["reply"]
    assert "不要盲目重复操作" in result["reply"]
    assert adapter.calls == []


async def test_slow_path_rate_limit_blocks_extra_requests(client) -> None:
    service, _ = build_service(ReplyLLM(), slow_path_limit_per_minute=1)
    first = await service.chat(member(), "陪我聊一句")
    second = await service.chat(member(), "再聊一句")

    assert first["route_status"] == "replied"
    assert second["route_status"] == "rate_limited"
    assert "频繁" in second["reply"]


async def test_ha_call_timeout_does_not_claim_no_change(client) -> None:
    adapter = HAAdapter(M6Config(), call_timeout_seconds=0.05)
    adapter.rest = SlowRest()

    with pytest.raises(HAClientError) as excinfo:
        await adapter.call_service("light", "turn_on", {"entity_id": "light.a"})

    message = str(excinfo.value)
    assert "did not acknowledge" in message
    assert "may still have changed" in message


async def test_ws_reconnect_backoff_caps_at_30_seconds(monkeypatch) -> None:
    client = HAWebSocketClient("http://ha.local:8123", "token", lambda event: None)

    async def fail() -> None:
        raise RuntimeError("ha is down")

    client._connect_once = fail
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 7:
            client._stop.set()

    monkeypatch.setattr("app.ha.client_ws.asyncio.sleep", fake_sleep)
    await client._run()

    assert sleeps == [1, 2, 4, 8, 16, 30, 30]


async def test_ws_command_rejected_when_disconnected() -> None:
    client = HAWebSocketClient("http://ha.local:8123", "token", lambda event: None)
    with pytest.raises(RuntimeError, match="not connected"):
        await client.command("get_states")
