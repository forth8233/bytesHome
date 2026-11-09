from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.agent.proposals import ProposalService
from app.chat.automation import AutomationValidationError, parse_automation
from app.chat.schema import ChatDecision
from app.chat.service import ChatService
from app.db.models import AuditLog, AutomationDraft, Proposal, ReasoningTrace
from app.db.session import SessionFactory
from app.llm.client import LLMResult
from app.llm.dsml import parse_dsml_decision
from app.safety.guard import ActionGuard
from app.safety.whitelist import WhitelistRules


class ChatConfig:
    def global_settings(self) -> dict:
        return {
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
            "feedback_reject_threshold": 2,
            "feedback_cooldown_seconds": 3600,
        }

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=("light.*", "lock.*"),
            service_allow=("light.turn_on", "light.turn_off", "lock.unlock"),
            param_rules={"light.turn_on": {"brightness_pct": [1, 100]}},
        )


class FakeCache:
    def __init__(self) -> None:
        self.states = {
            "light.a": {
                "entity_id": "light.a",
                "state": "off",
                "attributes": {"friendly_name": "客厅灯"},
            }
        }

    async def get(self, entity_id):
        return self.states.get(entity_id)

    async def snapshot(self):
        return dict(self.states)


class FakeRegistry:
    def area_of(self, entity_id):
        return "living"


class FakeRest:
    def __init__(self) -> None:
        self.written = []

    async def write_automation(self, automation_id, config):
        self.written.append((automation_id, config))
        return {"result": "ok"}


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = []
        self.cache = FakeCache()
        self.registry = FakeRegistry()
        self.rest = FakeRest()

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return []


class FakeLLM:
    def __init__(self, decisions: list[dict]) -> None:
        self.decisions = decisions

    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        data = self.decisions.pop(0) if self.decisions else {"reply": "完成", "tool_calls": []}
        return LLMResult(
            ok=True,
            parsed_ok=True,
            provider="stub",
            model="stub",
            content="{}",
            data=data,
            duration_ms=1,
        )


class RawLLM:
    """按 (data, content) 返回，模拟模型给出非法 JSON 或 DSML 标记。"""

    def __init__(self, results: list[tuple[dict | None, str]]) -> None:
        self.results = results

    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        data, content = self.results.pop(0)
        return LLMResult(
            ok=True,
            parsed_ok=data is not None,
            provider="stub",
            model="stub",
            content=content,
            data=data,
            duration_ms=1,
        )


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin", role="admin")


def _build_service(
    adapter: FakeAdapter | None = None,
    decisions: list[dict] | None = None,
    *,
    client=None,
) -> tuple[ChatService, FakeAdapter, ProposalService]:
    config = ChatConfig()
    adapter = adapter or FakeAdapter()
    guard = ActionGuard(config)
    proposals = ProposalService(
        session_factory=SessionFactory, config=config, adapter=adapter, guard=guard
    )
    service = ChatService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposals,
        client=client or FakeLLM(decisions or []),
    )
    return service, adapter, proposals


async def _cleanup() -> None:
    async with SessionFactory() as session:
        await session.execute(
            delete(Proposal).where(Proposal.entity_id.in_(["lock.m4test", "light.m4test"]))
        )
        await session.commit()


async def test_chat_high_risk_action_becomes_pending_proposal(client) -> None:
    await _cleanup()
    service, adapter, _ = _build_service(
        decisions=[
            {
                "reply": "正在开锁。",
                "tool_calls": [
                    {
                        "tool": "call_service",
                        "args": {
                            "domain": "lock",
                            "service": "unlock",
                            "entity_id": "lock.m4test",
                            "data": {},
                        },
                    }
                ],
            }
        ]
    )
    result = await service.chat(_member(), "帮我把门锁打开")

    assert adapter.calls == []
    assert result["proposal"] is not None
    assert result["proposal"]["status"] == "pending"
    assert result["proposal"]["risk_level"] == "high"
    assert result["proposal"]["source"] == "chat"
    assert "待确认提案" in result["reply"]


async def test_chat_denied_action_is_blocked(client) -> None:
    await _cleanup()
    service, adapter, _ = _build_service(
        decisions=[
            {
                "reply": "马上执行。",
                "tool_calls": [
                    {
                        "tool": "call_service",
                        "args": {
                            "domain": "shell_command",
                            "service": "reboot",
                            "entity_id": "shell_command.reboot",
                            "data": {},
                        },
                    }
                ],
            }
        ]
    )
    result = await service.chat(_member(), "重启系统")
    assert adapter.calls == []
    assert "安全策略拦截" in result["reply"]
    assert result["tool_results"][0]["result"]["denied"] is True


async def test_chat_low_risk_action_executes_and_loop_collects_state(client) -> None:
    await _cleanup()
    service, adapter, _ = _build_service(
        decisions=[
            {
                "reply": "",
                "tool_calls": [
                    {"tool": "get_state", "args": {"entity_id": "light.a"}},
                    {
                        "tool": "call_service",
                        "args": {
                            "domain": "light",
                            "service": "turn_on",
                            "entity_id": "light.m4test",
                            "data": {"brightness_pct": 40},
                        },
                    },
                ],
            },
            {"reply": "已打开客厅灯。", "tool_calls": []},
        ]
    )
    result = await service.chat(_member(), "看看客厅灯并打开")

    assert result["reply"] == "已打开客厅灯。"
    assert adapter.calls[0][:2] == ("light", "turn_on")
    assert result["tool_results"][0]["result"]["entities"][0]["entity_id"] == "light.a"
    assert result["proposal"] is None


def test_chat_decision_rejects_unrelated_json_and_empty_decisions() -> None:
    with pytest.raises(ValidationError):
        ChatDecision.model_validate(
            {"domain": "light", "service": "turn_off", "entity_id": "light.a"}
        )
    with pytest.raises(ValidationError):
        ChatDecision.model_validate({"reply": "", "tool_calls": []})


DSML_TURN_OFF = (
    "\n<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="call_service">\n'
    '<｜｜DSML｜｜ parameter name="args" string="false">'
    '{"domain": "light", "service": "turn_off", "entity_id": "light.m4test"}'
    "</｜｜DSML｜｜ parameter>\n"
    '<｜｜DSML｜｜ parameter name="tool" string="true">call_service</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


def test_dsml_parser_extracts_tool_calls() -> None:
    decision = parse_dsml_decision(DSML_TURN_OFF)
    assert decision is not None
    assert decision.reply == ""
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].tool == "call_service"
    assert decision.tool_calls[0].args["service"] == "turn_off"
    assert decision.tool_calls[0].args["entity_id"] == "light.m4test"
    assert parse_dsml_decision('{"reply": "你好", "tool_calls": []}') is None


async def test_chat_recovers_deepseek_dsml_tool_markup(client) -> None:
    await _cleanup()
    llm = RawLLM(
        [
            (
                {"domain": "light", "service": "turn_off", "entity_id": "light.m4test"},
                DSML_TURN_OFF,
            ),
            ({"reply": "已关闭客厅灯。", "tool_calls": []}, "{}"),
        ]
    )
    service, adapter, _ = _build_service(client=llm)

    result = await service.chat(_member(), "关灯")

    assert adapter.calls == [("light", "turn_off", {"entity_id": "light.m4test"})]
    assert result["reply"] == "已关闭客厅灯。"
    assert result["tool_results"][0]["result"]["ok"] is True


async def test_chat_never_claims_completion_without_action(client) -> None:
    service, adapter, _ = _build_service(
        decisions=[{"reply": "已完成。", "tool_calls": []}]
    )
    result = await service.chat(_member(), "关灯")

    assert adapter.calls == []
    assert "没有实际执行" in result["reply"]
    assert "已完成" not in result["reply"]


def test_automation_validation_accepts_safe_config() -> None:
    raw = """
alias: 晚间灯光
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: light.turn_on
    target:
      entity_id: light.a
    data:
      brightness_pct: 50
"""
    config = parse_automation(raw, ChatConfig().whitelist())
    assert config["alias"] == "晚间灯光"
    assert config["trigger"][0]["platform"] == "time"


@pytest.mark.parametrize(
    "raw",
    [
        """
alias: 危险设备
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: light.turn_on
    target:
      device_id: abc
""",
        """
alias: 模板注入
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: light.turn_on
    target:
      entity_id: "{{ states('input_text.x') }}"
""",
        """
alias: 未授权服务
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: shell_command.reboot
    entity_id: shell_command.reboot
""",
        """
alias: 未授权实体
trigger:
  - platform: webhook
    webhook_id: test
action:
  - service: light.turn_on
    entity_id: light.a
""",
    ],
)
def test_automation_validation_rejects_unsafe_config(raw: str) -> None:
    with pytest.raises(AutomationValidationError):
        parse_automation(raw, ChatConfig().whitelist())


async def test_automation_draft_requires_confirmation_and_writes_once(client) -> None:
    async with SessionFactory() as session:
        draft = AutomationDraft(
            member_id=1,
            confirm_token="m4-confirm-token-123456",
            yaml_content="""
alias: 安全测试
trigger:
  - platform: time
    at: "23:00:00"
action:
  - service: light.turn_on
    entity_id: light.a
""",
            status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        )
        session.add(draft)
        await session.commit()
        draft_id = draft.id

    service, adapter, _ = _build_service()
    result = await service.confirm_automation(_member(), "m4-confirm-token-123456")
    assert result["confirmed"] is True
    assert len(adapter.rest.written) == 1
    assert adapter.rest.written[0][1]["alias"] == "安全测试"

    with pytest.raises(HTTPException) as excinfo:
        await service.confirm_automation(_member(), "m4-confirm-token-123456")
    assert excinfo.value.status_code == 409

    async with SessionFactory() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "automation.confirmed").order_by(AuditLog.id.desc())
        )
        assert audit is not None
        await session.delete(await session.get(AutomationDraft, draft_id))
        await session.commit()


async def test_two_rejections_trigger_feedback_cooldown(client) -> None:
    from app.db.models import UserFeedback

    _, _, proposals = _build_service()
    signature = "m4cooldownsignature"
    async with SessionFactory() as session:
        await session.execute(
            delete(UserFeedback).where(UserFeedback.action_signature == signature)
        )
        await session.commit()

    await proposals.record_feedback(1, [signature], "proposal", "rejected")
    assert await proposals.in_cooldown(1, [signature]) is False
    await proposals.record_feedback(1, [signature], "proposal", "rejected")
    assert await proposals.in_cooldown(1, [signature]) is True

    await proposals.record_feedback(1, [signature], "proposal", "approved")
    assert await proposals.in_cooldown(1, [signature]) is False

    async with SessionFactory() as session:
        await session.execute(
            delete(UserFeedback).where(UserFeedback.action_signature == signature)
        )
        await session.commit()


def test_memory_preference_crud_and_purge_audit(client, auth_headers) -> None:
    created = client.post(
        "/api/memories/preference",
        json={"key": "climate.夏季温度", "value": "26"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    pref_id = created.json()["id"]

    listed = client.get("/api/memories/preference", headers=auth_headers)
    assert listed.status_code == 200
    assert any(item["id"] == pref_id for item in listed.json())

    updated = client.put(
        f"/api/memories/preference/{pref_id}",
        json={"value": "25"},
        headers=auth_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["value"] == "25"

    rejected = client.post(
        "/api/memories/purge", json={"confirm": False}, headers=auth_headers
    )
    assert rejected.status_code == 422

    purged = client.post("/api/memories/purge", json={"confirm": True}, headers=auth_headers)
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged_preferences"] >= 1

    audit = client.get("/api/audit/log", headers=auth_headers)
    assert audit.status_code == 200
    assert any(item["action"] == "memory.purge" for item in audit.json()["items"])

    missing = client.delete(f"/api/memories/preference/{pref_id}", headers=auth_headers)
    assert missing.status_code == 404


def test_chat_endpoints_are_wired(client, auth_headers) -> None:
    created = client.post("/api/chat/sessions", json={"title": "M4 测试"}, headers=auth_headers)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    sessions = client.get("/api/chat/sessions", headers=auth_headers)
    assert sessions.status_code == 200
    assert any(item["id"] == session_id for item in sessions.json())

    messages = client.get(
        f"/api/chat/sessions/{session_id}/messages", headers=auth_headers
    )
    assert messages.status_code == 200
    assert messages.json() == []

    deleted = client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)
    assert deleted.status_code == 204
