import asyncio
import dataclasses
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete

from app.agent.context import ContextBuilder
from app.agent.proposals import ProposalError, ProposalService
from app.agent.schema import AgentProposal
from app.db.models import Proposal
from app.events.models import ProcessedEvent
from app.ha.registry import RegistryCache
from app.llm.client import LLMClient
from app.llm.json_parse import extract_json_object
from app.notify.sse import SSEHub
from app.safety.guard import ActionGuard, action_signature, max_risk
from app.safety.whitelist import WhitelistRules


def test_extract_json_object_handles_fences_and_nesting() -> None:
    text = '说明如下：\n```json\n{"a": {"b": 1}, "c": "x}y"}\n```\n结束'
    assert extract_json_object(text) == {"a": {"b": 1}, "c": "x}y"}
    assert extract_json_object("没有对象") is None
    assert extract_json_object('{bad} then {"ok": true}') == {"ok": True}


def test_agent_proposal_requires_action_when_proposing() -> None:
    with pytest.raises(ValueError):
        AgentProposal(should_propose=True, actions=[])
    ok = AgentProposal(
        should_propose=True,
        actions=[{"domain": "light", "service": "turn_on", "entity_id": "light.a"}],
    )
    assert ok.actions[0].full_service == "light.turn_on"


class _Response:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class _FakeHttpClient:
    calls: list[dict] = []
    responses: list[_Response] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002
        _FakeHttpClient.calls.append({"url": url, "headers": headers, "payload": json})
        return _FakeHttpClient.responses.pop(0)


class StubLLMConfig:
    def __init__(self, primary: dict, fallback: dict | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or {}
        self.store: dict = {}

    def llm_settings(self, which: str = "primary") -> dict:
        return dict(self.primary if which == "primary" else self.fallback)

    def get(self, key, default=None):
        return self.store.get(key, default)

    async def set(self, key, value, encrypted: bool = False) -> None:
        self.store[key] = value


DEEPSEEK = {
    "provider": "deepseek",
    "model_name": "deepseek-flash",
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "test-key",
    "temperature": 0.1,
    "timeout": 60,
    "max_tokens": 1024,
}


def _body(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
    }


async def test_llm_client_uses_json_object_tier(monkeypatch) -> None:
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses = [_Response(_body('{"should_propose": false}'))]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpClient)

    client = LLMClient(StubLLMConfig(DEEPSEEK))
    result = await client.structured(
        [{"role": "user", "content": "观察家庭状态"}], AgentProposal
    )

    assert result.parsed_ok is True
    payload = _FakeHttpClient.calls[0]["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["n"] == 1
    assert any("json" in message["content"].lower() for message in payload["messages"])
    assert result.prompt_tokens == 12


async def test_llm_client_retries_once_with_validation_error(monkeypatch) -> None:
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses = [
        _Response(_body("我认为不需要操作")),
        _Response(_body('{"should_propose": false, "thought": "ok"}')),
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpClient)

    client = LLMClient(StubLLMConfig(DEEPSEEK))
    result = await client.structured_with_fallback(
        [{"role": "user", "content": "json 观察"}], AgentProposal
    )

    assert result.parsed_ok is True
    assert result.attempts == 2
    assert len(_FakeHttpClient.calls) == 2
    retry_messages = _FakeHttpClient.calls[1]["payload"]["messages"]
    assert "校验" in retry_messages[-1]["content"]


class GuardConfig:
    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=("light.*", "climate.*", "lock.*", "shell_command.*"),
            service_allow=("light.turn_on", "climate.set_temperature", "lock.unlock", "shell_command.*"),
            param_rules={
                "climate.set_temperature": {"temperature": [16, 30]},
                "light.turn_on": {"brightness_pct": [1, 100]},
            },
        )


def _guard_config() -> GuardConfig:
    return GuardConfig()


async def test_action_guard_passes_low_risk_action() -> None:
    result = await ActionGuard(_guard_config()).check(
        [{"domain": "light", "service": "turn_on", "entity_id": "light.a", "data": {"brightness_pct": 50}}]
    )
    assert result.passed is True
    assert result.risk_level == "low"
    assert result.requires_confirm is False


async def test_action_guard_rejects_out_of_range_param() -> None:
    result = await ActionGuard(_guard_config()).check(
        [{"domain": "climate", "service": "set_temperature", "entity_id": "climate.a", "data": {"temperature": 35}}]
    )
    assert result.passed is False
    assert "between 16" in result.reasons[0]


async def test_action_guard_hard_denies_shell_command() -> None:
    result = await ActionGuard(_guard_config()).check(
        [{"domain": "shell_command", "service": "reboot", "entity_id": "shell_command.reboot", "data": {}}]
    )
    assert result.passed is False
    assert "hard-denied" in result.reasons[0]


async def test_action_guard_raises_risk_but_never_lowers() -> None:
    result = await ActionGuard(_guard_config()).check(
        [{"domain": "lock", "service": "unlock", "entity_id": "lock.a", "data": {}}],
        llm_risk="low",
        event_pre_risk="medium",
    )
    assert result.passed is True
    assert result.risk_level == "high"
    assert result.requires_confirm is True
    assert max_risk("low", "high", "medium") == "high"


def test_action_signature_ignores_unrelated_fields() -> None:
    a = action_signature("light.turn_on", "light.a", {"brightness_pct": 50, "note": "x"})
    b = action_signature("light.turn_on", "light.a", {"brightness_pct": 50, "timestamp": "1"})
    c = action_signature("light.turn_on", "light.a", {"brightness_pct": 60})
    assert a == b
    assert a != c


class StubCache:
    def __init__(self, states: dict) -> None:
        self.states = states

    async def snapshot(self) -> dict:
        return dict(self.states)


def _event(entity_id: str = "light.living", domain: str = "light") -> ProcessedEvent:
    return ProcessedEvent(
        event_id="e1",
        entity_id=entity_id,
        entity_name="客厅灯",
        domain=domain,
        event_type="state_changed",
        old_state="off",
        new_state="on",
        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        risk_pre="low",
    )


async def test_context_builder_wraps_untrusted_and_filters_attributes() -> None:
    registry = RegistryCache()
    registry.entities = {
        "light.living": {"entity_id": "light.living", "name": "客厅灯", "area_id": "living", "device_id": "d1"},
        "light.other": {"entity_id": "light.other", "name": "书房灯", "area_id": "study", "device_id": "d2"},
    }
    cache = StubCache(
        {
            "light.living": {
                "entity_id": "light.living",
                "state": "on",
                "attributes": {"brightness": 200, "friendly_name": "客厅灯", "last_changed": "x"},
            },
            "light.other": {"entity_id": "light.other", "state": "off", "attributes": {"brightness": 10}},
        }
    )
    builder = ContextBuilder(session_factory=None, cache=cache, registry=registry)
    bundle = await builder.build(_event())

    assert bundle.event["name"] == "<untrusted>客厅灯</untrusted>"
    assert bundle.event["new_state"] == "<untrusted>on</untrusted>"
    assert bundle.entities[0]["entity_id"] == "light.living"
    assert "brightness" in bundle.entities[0]["attributes"]
    assert "last_changed" not in bundle.entities[0].get("attributes", {})
    assert bundle.messages[0]["role"] == "system"
    assert "<untrusted>" in bundle.messages[1]["content"]


async def test_context_builder_drops_entities_over_budget() -> None:
    registry = RegistryCache()
    states = {}
    for index in range(60):
        entity_id = f"light.l{index}"
        registry.entities[entity_id] = {
            "entity_id": entity_id,
            "name": "很长很长" * 30 + str(index),
            "area_id": "living",
            "device_id": "d1",
        }
        states[entity_id] = {"entity_id": entity_id, "state": "on", "attributes": {}}
    builder = ContextBuilder(session_factory=None, cache=StubCache(states), registry=registry)
    bundle = await builder.build(_event(entity_id="light.l0"))
    assert bundle.dropped > 0
    assert len(bundle.entities) < 60


def test_sse_hub_ticket_is_single_use() -> None:
    hub = SSEHub()
    ticket = hub.issue_ticket(7)
    assert hub.consume_ticket(ticket) == 7
    assert hub.consume_ticket(ticket) is None
    assert hub.consume_ticket("nope") is None


async def test_sse_hub_replays_from_last_event_id() -> None:
    hub = SSEHub()
    await hub.publish("proposal.created", {"proposal_id": 1})
    await hub.publish("proposal.updated", {"proposal_id": 1, "status": "executed"})
    assert hub.since(0) == []
    replay = hub.since(1)
    assert len(replay) == 1
    assert replay[0][1] == "proposal.updated"


class StubProposalConfig:
    def global_settings(self) -> dict:
        return {
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
            "feedback_reject_threshold": 2,
            "feedback_cooldown_seconds": 3600,
        }


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return []


class StubMember:
    id = 1
    username = "admin"
    role = "admin"


async def test_proposal_state_machine_executes_and_blocks_double_approve(client) -> None:
    from app.db.session import SessionFactory

    async with SessionFactory() as session:
        await session.execute(delete(Proposal).where(Proposal.entity_id == "sensor.m3test"))
        await session.commit()

    adapter = FakeAdapter()
    service = ProposalService(
        session_factory=SessionFactory,
        config=StubProposalConfig(),
        adapter=adapter,
        guard=ActionGuard(_guard_config()),
    )
    actions = [{"domain": "light", "service": "turn_on", "entity_id": "light.m3", "data": {}}]

    record, outcome = await service.create(
        entity_id="sensor.m3test",
        event_type="state_changed",
        actions=actions,
        thought="t",
        suggestion="s",
        risk_level="low",
        risk_source="code",
        trace_id="trace-m3",
        member_id=None,
    )
    assert outcome == "created"
    assert record["status"] == "pending"

    duplicate, duplicate_outcome = await service.create(
        entity_id="sensor.m3test",
        event_type="state_changed",
        actions=actions,
        thought="t",
        suggestion="s",
        risk_level="low",
        risk_source="code",
        trace_id="trace-m3",
        member_id=None,
    )
    assert duplicate is None
    assert duplicate_outcome == "duplicate"

    executed = await service.approve(record["id"], StubMember())
    assert executed["status"] == "executed"
    assert adapter.calls[0][:2] == ("light", "turn_on")

    with pytest.raises(ProposalError) as excinfo:
        await service.approve(record["id"], StubMember())
    assert excinfo.value.status_code == 409


async def test_proposal_sweeper_times_out_expired(client) -> None:
    from datetime import timedelta

    from app.db.session import SessionFactory
    from app.db.models import Proposal

    service = ProposalService(
        session_factory=SessionFactory,
        config=StubProposalConfig(),
        adapter=FakeAdapter(),
        guard=ActionGuard(_guard_config()),
    )
    record, _ = await service.create(
        entity_id="sensor.m3expire",
        event_type="state_changed",
        actions=[{"domain": "light", "service": "turn_on", "entity_id": "light.m3", "data": {}}],
        thought="",
        suggestion="",
        risk_level="low",
        risk_source="code",
        trace_id="trace-x",
        member_id=None,
    )
    async with SessionFactory() as session:
        row = await session.get(Proposal, record["id"])
        row.expire_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
        await session.commit()

    assert await service.sweep_expired() >= 1
    assert (await service.get(record["id"]))["status"] == "timeout"


def test_proposal_endpoints_are_wired(client, auth_headers) -> None:
    response = client.get("/api/proposal?page=1&page_size=5", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert "items" in response.json()

    stats = client.get("/api/stats", headers=auth_headers)
    assert stats.status_code == 200, stats.text
    assert "proposal" in stats.json()

    events = client.get("/api/events?page=1&page_size=5", headers=auth_headers)
    assert events.status_code == 200, events.text


def test_sse_ticket_endpoint(client, auth_headers) -> None:
    response = client.post("/api/sse/ticket", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == 60


async def test_reasoner_raises_alert_for_high_risk_without_action(client) -> None:
    from types import SimpleNamespace

    from app.agent.reasoner import Reasoner
    from app.llm.client import LLMResult

    class ReasonerConfig:
        def global_settings(self):
            return {"active_reasoning_enabled": True, "llm_rate_limit_per_minute": 30}

    class BuilderStub:
        async def build(self, event, member_id=None):
            return SimpleNamespace(messages=[], input_summary="{}")

    class ClientStub:
        def __init__(self, data):
            self.data = data

        async def structured_with_fallback(self, messages, model_cls, schema_name=""):
            return LLMResult(parsed_ok=True, data=self.data)

    class NotifierStub:
        def __init__(self):
            self.alerts = []

        async def notify_alert(self, member, *, entity_id, suggestion, risk_level="high"):
            self.alerts.append((entity_id, suggestion))
            return True

    class SSEStub:
        def __init__(self):
            self.events = []

        async def publish(self, event_type, payload):
            self.events.append((event_type, payload))

    async def _fake_trace(result, bundle, event):
        return "trace-alert"

    async def _load_member(_member_id):
        return SimpleNamespace(id=1, notify_service="notify.mobile_app_iphone")

    async def _target_member():
        return 1

    async def run(data, risk_pre):
        notifier = NotifierStub()
        sse = SSEStub()
        reasoner = Reasoner(
            config=ReasonerConfig(),
            queue=asyncio.Queue(),
            builder=BuilderStub(),
            client=ClientStub(data),
            guard=ActionGuard(_guard_config()),
            proposals=None,
            session_factory=None,
            sse=sse,
            notifier=notifier,
        )
        reasoner._write_trace = _fake_trace
        reasoner._load_member = _load_member
        reasoner._target_member = _target_member
        event = _event(entity_id="binary_sensor.leak", domain="binary_sensor")
        event = dataclasses.replace(event, risk_pre=risk_pre)
        await reasoner._handle(event)
        return reasoner, sse, notifier

    reasoner, sse, notifier = await run(
        {"should_propose": False, "suggestion": "请立即关闭进水总阀并检查漏水点"}, "high"
    )
    assert reasoner.counters["alerted"] == 1
    assert sse.events[0][0] == "alert"
    assert notifier.alerts == [("binary_sensor.leak", "请立即关闭进水总阀并检查漏水点")]

    reasoner, sse, notifier = await run({"should_propose": False, "suggestion": ""}, "low")
    assert reasoner.counters["alerted"] == 0
    assert sse.events == []
    assert notifier.alerts == []

    reasoner, sse, notifier = await run({"should_propose": False, "suggestion": ""}, "high")
    assert reasoner.counters["alerted"] == 1
    assert sse.events[0][1]["suggestion"].startswith("检测到高风险事件")
    assert notifier.alerts[0][0] == "binary_sensor.leak"
