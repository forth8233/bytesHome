from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.channels.base import InboundMessage, OutboundMessage
from app.channels.dispatcher import ChannelDispatcher
from app.channels.render import render_alert_push, render_proposal_push, render_reply
from app.db.models import ChannelIdentity, Member
from app.db.session import SessionFactory
from app.services.asr import TranscribeError, build_transcriber


class ChannelConfig:
    """dispatcher 只需要 global_settings()，配置项给最小集合即可。"""

    def __init__(self, **settings) -> None:
        self._settings = {"channel_rate_limit_per_minute": 20, **settings}

    def get(self, key, default=None):
        return self._settings.get(key, default)

    def global_settings(self) -> dict:
        return dict(self._settings)


class FakeAdapter:
    name = "feishu"

    def __init__(self) -> None:
        self.sent: list[tuple[str, OutboundMessage]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_id, message) -> bool:
        self.sent.append((chat_id, message))
        return True

    def status(self) -> dict:
        return {"name": self.name}


class FakeChat:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.result = result or {"reply": "已关闭客厅灯。", "session_id": 7}

    async def chat(self, member, text, session_id=None):
        self.calls.append((member.id, text, session_id))
        return dict(self.result)


class FakeProposals:
    def __init__(self) -> None:
        self.approved: list[int] = []
        self.rejected: list[int] = []

    async def get(self, proposal_id: int) -> dict:
        return {"id": proposal_id, "member_id": 1, "status": "pending"}

    async def approve(self, proposal_id: int, member) -> dict:
        self.approved.append(proposal_id)
        return {"id": proposal_id, "status": "executed"}

    async def reject(self, proposal_id: int, member, reason=None) -> dict:
        self.rejected.append(proposal_id)
        return {"id": proposal_id, "status": "rejected"}


async def _admin_member() -> Member:
    async with SessionFactory() as session:
        return await session.scalar(select(Member).where(Member.username == "admin"))


async def _bind(external_id: str, chat_id: str = "oc_test_chat") -> None:
    async with SessionFactory() as session:
        session.add(
            ChannelIdentity(
                channel="feishu",
                external_id=external_id,
                member_id=(await session.scalar(select(Member.id))),
                display_name="测试账号",
                meta_json=json.dumps({"chat_id": chat_id}),
            )
        )
        await session.commit()


def build_dispatcher(chat=None, proposals=None, **settings) -> tuple[ChannelDispatcher, FakeAdapter]:
    adapter = FakeAdapter()
    dispatcher = ChannelDispatcher(
        session_factory=SessionFactory,
        chat_service=chat or FakeChat(),
        proposals=proposals or FakeProposals(),
        config=ChannelConfig(**settings),
    )
    dispatcher.register(adapter)
    return dispatcher, adapter


# ---------- 渲染 ----------


def test_render_reply_includes_state_lines_and_card() -> None:
    message = render_reply(
        {
            "reply": "客厅当前温度 24 度。",
            "route": "fast_path",
            "tool_results": [
                {
                    "tool": "get_state",
                    "result": {
                        "persist": {
                            "ok": True,
                            "entities": [
                                {"entity_id": "sensor.temp", "name": "客厅温度", "state": "24"}
                            ],
                        }
                    },
                }
            ],
        }
    )

    assert "客厅当前温度 24 度。" in message.text
    assert "客厅温度：24" in message.text
    assert message.card is not None
    assert message.card["header"]["title"]["content"] == "设备状态"
    assert message.card["header"]["template"] == "blue"


def test_render_reply_plain_text_when_nothing_structured() -> None:
    message = render_reply({"reply": "好的。"})

    assert message.text == "好的。"
    assert message.card is None


def test_render_reply_high_risk_proposal_uses_red_and_confirm_hint() -> None:
    message = render_reply(
        {
            "reply": "这个操作风险较高，已转待确认。",
            "proposal": {
                "id": 12,
                "risk_level": "high",
                "suggestion": "解锁前门",
                "entity_id": "lock.front_door",
                "actions": [
                    {
                        "entity_id": "lock.front_door",
                        "domain": "lock",
                        "service": "unlock",
                        "data": {},
                    }
                ],
            },
        }
    )

    assert "确认 #12" in message.text
    assert message.card["header"]["template"] == "red"
    assert message.card["header"]["title"]["content"] == "待确认操作 #12"


def test_render_reply_draft_carries_confirm_token() -> None:
    message = render_reply(
        {"reply": "草稿已生成。", "draft": {"confirm_token": "tok_abcdefgh", "yaml": "alias: x"}}
    )

    assert "确认自动化 tok_abcdefgh" in message.text
    assert message.card is not None
    assert "tok_abcdefgh" in json.dumps(message.card, ensure_ascii=False)


def test_render_proposal_push_has_risk_header() -> None:
    message = render_proposal_push(
        {
            "id": 5,
            "risk_level": "medium",
            "suggestion": "夜间开启电热毯",
            "entity_id": "switch.blanket",
            "actions": [
                {"entity_id": "switch.blanket", "domain": "switch", "service": "turn_on", "data": {}}
            ],
        }
    )

    assert "[medium]" in message.text
    assert "确认 #5" in message.text
    assert message.card["header"]["template"] == "orange"


def test_render_alert_push_truncates_long_text() -> None:
    message = render_alert_push(
        entity_id="binary_sensor.smoke", suggestion="烟" * 400, risk_level="high"
    )

    assert message.text.startswith("[high][告警] binary_sensor.smoke：")
    assert len(message.text) < 400
    assert message.card["header"]["template"] == "red"


# ---------- dispatcher ----------


async def test_unbound_account_gets_hint(client) -> None:
    dispatcher, adapter = build_dispatcher()

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_stranger", chat_id="oc_stranger", text="关客厅灯")
    )

    assert dispatcher.counters["unbound"] == 1
    assert "还没有绑定家庭成员" in adapter.sent[-1][1].text


async def test_unbound_account_is_remembered_for_binding(client) -> None:
    dispatcher, _ = build_dispatcher()

    await dispatcher._process(
        InboundMessage(
            channel="feishu",
            external_id="ou_pending",
            chat_id="oc_pending",
            display_name="智杰",
            text="现在有几个灯开着",
        )
    )

    pending = dispatcher.pending()
    assert len(pending) == 1
    assert pending[0]["external_id"] == "ou_pending"
    assert pending[0]["chat_id"] == "oc_pending"
    assert pending[0]["display_name"] == "智杰"
    assert pending[0]["count"] == 1

    # 再发一条只累加次数，不会产生重复条目
    await dispatcher._process(
        InboundMessage(
            channel="feishu", external_id="ou_pending", chat_id="oc_pending", text="在吗"
        )
    )
    assert dispatcher.pending()[0]["count"] == 2

    # 绑定之后这条从待绑定里消失
    dispatcher.forget_pending("feishu", "ou_pending")
    assert dispatcher.pending() == []


async def test_bound_account_is_not_kept_pending(client) -> None:
    await _bind("ou_not_pending")
    dispatcher, _ = build_dispatcher()

    await dispatcher._process(
        InboundMessage(
            channel="feishu", external_id="ou_not_pending", chat_id="oc_test_chat", text="你好"
        )
    )

    assert dispatcher.pending() == []


async def test_bound_account_routes_to_chat_service(client) -> None:
    await _bind("ou_bound")
    chat = FakeChat({"reply": "已关闭客厅灯。", "session_id": 11})
    dispatcher, adapter = build_dispatcher(chat=chat)

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_bound", chat_id="oc_test_chat", text="关客厅灯")
    )

    assert chat.calls[0][1] == "关客厅灯"
    assert chat.calls[0][2] is None  # 首轮没有历史会话
    assert adapter.sent[-1][1].text == "已关闭客厅灯。"
    assert dispatcher.counters["handled"] == 1

    # 第二轮带上一轮 session_id，保证上下文连续
    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_bound", chat_id="oc_test_chat", text="再打开")
    )
    assert chat.calls[1][2] == 11


async def test_bound_account_records_chat_id_for_push(client) -> None:
    await _bind("ou_push", chat_id="")
    dispatcher, _ = build_dispatcher()

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_push", chat_id="oc_new_chat", text="你好")
    )

    async with SessionFactory() as session:
        identity = await session.scalar(
            select(ChannelIdentity).where(ChannelIdentity.external_id == "ou_push")
        )
    assert json.loads(identity.meta_json)["chat_id"] == "oc_new_chat"


async def test_help_command_does_not_reach_llm(client) -> None:
    await _bind("ou_help")
    chat = FakeChat()
    dispatcher, adapter = build_dispatcher(chat=chat)

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_help", chat_id="oc_help", text="帮助")
    )

    assert chat.calls == []
    assert "直接说要做的事" in adapter.sent[-1][1].text


async def test_confirm_command_approves_proposal(client) -> None:
    await _bind("ou_confirm")
    proposals = FakeProposals()
    dispatcher, adapter = build_dispatcher(proposals=proposals)

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_confirm", chat_id="oc_c", text="确认 #42")
    )

    assert proposals.approved == [42]
    assert dispatcher.counters["confirmed"] == 1
    assert "已确认并执行完成" in adapter.sent[-1][1].text


async def test_reject_command_rejects_proposal(client) -> None:
    await _bind("ou_reject")
    proposals = FakeProposals()
    dispatcher, adapter = build_dispatcher(proposals=proposals)

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_reject", chat_id="oc_r", text="拒绝 #43")
    )

    assert proposals.rejected == [43]
    assert "已拒绝提案 #43" in adapter.sent[-1][1].text


async def test_rate_limit_replies_without_calling_chat(client) -> None:
    await _bind("ou_ratelimit")
    chat = FakeChat()
    dispatcher, adapter = build_dispatcher(chat=chat, channel_rate_limit_per_minute=1)
    msg = InboundMessage(channel="feishu", external_id="ou_ratelimit", chat_id="oc_rate", text="开灯")

    await dispatcher._process(msg)
    await dispatcher._process(msg)

    assert len(chat.calls) == 1
    assert "消息太频繁" in adapter.sent[-1][1].text


async def test_chat_failure_is_reported_to_user(client) -> None:
    await _bind("ou_fail")

    class BoomChat:
        async def chat(self, member, text, session_id=None):
            raise RuntimeError("llm exploded")

    dispatcher, adapter = build_dispatcher(chat=BoomChat())

    await dispatcher._process(
        InboundMessage(channel="feishu", external_id="ou_fail", chat_id="oc_f", text="开灯")
    )

    assert dispatcher.counters["failed"] == 1
    assert "这次处理出错了" in adapter.sent[-1][1].text


async def test_submit_drains_through_worker(client) -> None:
    await _bind("ou_worker")
    chat = FakeChat()
    dispatcher, adapter = build_dispatcher(chat=chat)
    await dispatcher.start()
    try:
        assert dispatcher.submit(
            InboundMessage(channel="feishu", external_id="ou_worker", chat_id="oc_w", text="你好")
        )
        for _ in range(100):
            if dispatcher.counters["handled"]:
                break
            await asyncio.sleep(0.02)
    finally:
        await dispatcher.stop()

    assert dispatcher.counters["handled"] == 1
    assert adapter.sent[-1][1].text == "已关闭客厅灯。"


# ---------- 管理接口 ----------


def test_channels_overview_requires_admin(client, auth_headers) -> None:
    assert client.get("/api/channels").status_code == 401
    response = client.get("/api/channels", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "feishu" in data
    assert "dispatcher" in data
    assert "pending" in data
    assert isinstance(data["pending"], list)
    assert isinstance(data["feishu"]["instances"], list)
    for item in data["feishu"]["instances"]:
        assert "app_secret" not in item


def test_identity_create_rejects_app_id(client, auth_headers) -> None:
    """把 App ID（cli_ 开头）当渠道账号填进来，必须当场拦下。"""
    member_id = client.get("/api/members", headers=auth_headers).json()[0]["id"]

    response = client.post(
        "/api/channels/identities",
        headers=auth_headers,
        json={
            "channel": "feishu",
            "external_id": "cli_a924a185eff9dcb0",
            "member_id": member_id,
        },
    )

    assert response.status_code == 422
    assert "open_id" in response.json()["message"]


def test_feishu_config_save_rejects_empty_app_id(client, auth_headers) -> None:
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={
            "instances": [
                {"enabled": True, "app_id": "  ", "push_enabled": True},
            ]
        },
    )
    assert response.status_code == 422


def test_feishu_config_save_rejects_duplicate_app_id(client, auth_headers) -> None:
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={
            "instances": [
                {"enabled": True, "app_id": "cli_dup", "push_enabled": True},
                {"enabled": True, "app_id": " cli_dup ", "push_enabled": True},
            ]
        },
    )
    assert response.status_code == 422
    assert "重复" in response.json()["message"]


def test_feishu_config_save_masks_secret(client, auth_headers) -> None:
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={
            "instances": [
                {
                    "enabled": False,
                    "app_id": "cli_test_app",
                    "app_secret": "super-secret-value",
                    "push_enabled": False,
                }
            ]
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["feishu"]["instances"][0]["app_id"] == "cli_test_app"
    assert data["feishu"]["instances"][0]["app_secret_set"] is True
    assert "super-secret-value" not in response.text

    # 再次保存时省略 app_secret，应沿用旧密钥
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={"instances": [{"enabled": False, "app_id": "cli_test_app", "push_enabled": True}]},
    )
    assert response.json()["feishu"]["instances"][0]["app_secret_set"] is True
    assert response.json()["feishu"]["instances"][0]["push_enabled"] is True


def test_feishu_multi_instance_save_and_apply(client, auth_headers) -> None:
    """第二个应用叫 feishu_2，运行中的适配器按实例名出现。"""
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={
            "instances": [
                {"enabled": False, "app_id": "cli_test_app", "push_enabled": True},
                {
                    "enabled": True,
                    "app_id": "cli_second_app",
                    "app_secret": "super-secret-value",
                    "push_enabled": True,
                    "remark": "备用飞书",
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    instances = data["feishu"]["instances"]
    assert [item["name"] for item in instances] == ["feishu", "feishu_2"]
    assert instances[1]["enabled"] is True
    assert instances[1]["remark"] == "备用飞书"
    assert instances[1]["app_secret_set"] is True

    # 第二个实例已注册到 dispatcher（长连接地址是假的，连不上但适配器存在）
    names = [item["name"] for item in data["channels"]]
    assert "feishu_2" in names

    # 删掉第一行后，原第二行仍叫 feishu_2，已绑定的渠道名不会错位
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={"instances": [{"name": "feishu_2", "enabled": False, "app_id": "cli_second_app"}]},
    )
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["feishu"]["instances"]] == ["feishu_2"]

    # 恢复单实例（显式写回 feishu，避免旧行名字 feishu_2 被继承），避免影响其它用例
    response = client.put(
        "/api/channels/feishu",
        headers=auth_headers,
        json={
            "instances": [
                {"name": "feishu", "enabled": False, "app_id": "cli_test_app", "push_enabled": True}
            ]
        },
    )
    assert response.status_code == 200


def test_identity_crud(client, auth_headers) -> None:
    members = client.get("/api/members", headers=auth_headers).json()
    member_id = members[0]["id"]

    created = client.post(
        "/api/channels/identities",
        headers=auth_headers,
        json={
            "channel": "feishu",
            "external_id": "ou_api_test",
            "member_id": member_id,
            "display_name": "接口测试",
            "chat_id": "oc_api_chat",
        },
    )
    assert created.status_code == 201, created.text
    identity = created.json()
    assert identity["channel"] == "feishu"
    assert identity["chat_id"] == "oc_api_chat"

    duplicated = client.post(
        "/api/channels/identities",
        headers=auth_headers,
        json={"channel": "feishu", "external_id": "ou_api_test", "member_id": member_id},
    )
    assert duplicated.status_code == 409

    unsupported = client.post(
        "/api/channels/identities",
        headers=auth_headers,
        json={"channel": "qq", "external_id": "u1", "member_id": member_id},
    )
    assert unsupported.status_code == 422

    # 绑定到一个不存在的飞书实例名也要拦下
    unknown_instance = client.post(
        "/api/channels/identities",
        headers=auth_headers,
        json={"channel": "feishu_9", "external_id": "ou_x", "member_id": member_id},
    )
    assert unknown_instance.status_code == 422
    assert "未配置" in unknown_instance.json()["message"]

    patched = client.patch(
        f"/api/channels/identities/{identity['id']}",
        headers=auth_headers,
        json={"enabled": False, "chat_id": "oc_api_chat2"},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["chat_id"] == "oc_api_chat2"

    listed = client.get("/api/channels/identities", headers=auth_headers).json()
    assert any(item["external_id"] == "ou_api_test" for item in listed)

    deleted = client.delete(
        f"/api/channels/identities/{identity['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204
    assert client.delete(
        f"/api/channels/identities/{identity['id']}", headers=auth_headers
    ).status_code == 404


# ---------- 语音识别 ----------


class FakeMessage:
    """模拟飞书 SDK 的 EventMessage（audio 类型）。"""

    message_id = "om_audio_1"
    chat_id = "oc_audio_chat"
    chat_type = "p2p"
    message_type = "audio"
    content = json.dumps({"file_key": "file_key_1", "duration": 2000})


class FakeTranscriber:
    """可编排结果的转写器替身。"""

    def __init__(self, text: str = "打开客厅灯", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[bytes] = []

    async def transcribe(self, data: bytes, filename: str) -> str:
        self.calls.append(data)
        if self.error is not None:
            raise self.error
        return self.text


class FakeResourceResponse:
    def __init__(self, payload: bytes = b"fake-ogg", code: int = 0) -> None:
        import io

        self.code = code
        self.file = io.BytesIO(payload)
        self.msg = "ok"


class AudioApiStub:
    """只实现 message_resource.get 的 API 替身（SDK 的这个方法是同步的）。"""

    def __init__(self, response: FakeResourceResponse) -> None:
        calls = self

        class ResourceStub:
            def get(self, request):  # noqa: ANN001 - SDK 形状太啰嗦
                calls.request = request
                return response

        class V1Stub:
            def __init__(self) -> None:
                self.message_resource = ResourceStub()

        self.im = type("IM", (), {"v1": None})()
        self.im.v1 = V1Stub()


def build_audio_adapter(transcriber, api=None):
    """构造一个不走 WebSocket 的适配器：直接注入线程循环与 API 替身。"""
    from app.channels.feishu import FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(
        app_id="cli_x", app_secret="s", dispatcher=dispatcher, transcriber=transcriber
    )
    adapter._api = api
    adapter._thread_loop = asyncio.new_event_loop()
    return adapter, dispatcher


async def test_audio_flow_transcribes_and_submits() -> None:
    transcriber = FakeTranscriber("打开客厅灯")
    api = AudioApiStub(FakeResourceResponse(b"ogg-bytes"))
    adapter, dispatcher = build_audio_adapter(transcriber, api)
    sent: list[OutboundMessage] = []

    async def fake_send(chat_id, message):
        sent.append(message)
        return True

    adapter.send = fake_send  # type: ignore[method-assign]

    loop = adapter._thread_loop
    try:
        await adapter._transcribe_audio(
            FakeMessage(), "om_audio_1", "oc_audio_chat", "ou_voice"
        )
    finally:
        loop.close()

    assert transcriber.calls == [b"ogg-bytes"]
    assert api.request.file_key == "file_key_1"
    assert api.request.message_id == "om_audio_1"
    assert dispatcher.counters["received"] == 1


async def test_audio_flow_reports_transcribe_failure(client) -> None:
    transcriber = FakeTranscriber(error=TranscribeError("HTTP 401: bad key"))
    api = AudioApiStub(FakeResourceResponse(b"ogg-bytes"))
    adapter, dispatcher = build_audio_adapter(transcriber, api)
    sent: list[OutboundMessage] = []

    async def fake_send(chat_id, message):
        sent.append(message)
        return True

    adapter.send = fake_send  # type: ignore[method-assign]

    loop = adapter._thread_loop
    try:
        await adapter._transcribe_audio(FakeMessage(), "om_a2", "oc_audio_chat", "ou_voice")
    finally:
        loop.close()

    assert transcriber.calls == [b"ogg-bytes"]
    assert "转文字失败" in sent[-1].text
    assert adapter.status()["transcribe_failed"] == 1
    assert adapter.status()["asr_error"] == "HTTP 401: bad key"
    assert dispatcher.counters["received"] == 0


async def test_audio_flow_hint_includes_cause(client) -> None:
    """余额不足这类可翻译的错误，飞书提示要带上原因。"""
    transcriber = FakeTranscriber(error=TranscribeError('HTTP 402: {"code":30001}'))
    api = AudioApiStub(FakeResourceResponse(b"ogg-bytes"))
    adapter, dispatcher = build_audio_adapter(transcriber, api)
    sent: list[OutboundMessage] = []

    async def fake_send(chat_id, message):
        sent.append(message)
        return True

    adapter.send = fake_send  # type: ignore[method-assign]

    loop = adapter._thread_loop
    try:
        await adapter._transcribe_audio(FakeMessage(), "om_a3", "oc_audio_chat", "ou_voice")
    finally:
        loop.close()

    assert "余额不足" in sent[-1].text
    assert "转文字失败" in sent[-1].text


def test_asr_status_masks_api_key(client, auth_headers) -> None:
    data = client.get("/api/channels", headers=auth_headers).json()
    assert "asr" in data
    assert "api_key" not in json.dumps(data["asr"]["config"])


def test_asr_save_roundtrip(client, auth_headers) -> None:
    # 未启用时不需要 api_base
    response = client.put("/api/channels/asr", headers=auth_headers, json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["asr"]["enabled"] is False

    # 启用必须带合法 api_base
    bad = client.put(
        "/api/channels/asr", headers=auth_headers, json={"enabled": True, "api_base": "不是地址"}
    )
    assert bad.status_code == 422

    good = client.put(
        "/api/channels/asr",
        headers=auth_headers,
        json={
            "enabled": True,
            "api_base": "https://asr.example.com/v1",
            "api_key": "super-secret-value",
            "model": "SenseVoiceSmall",
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["asr"]["enabled"] is True
    assert "real-key-value" not in good.text  # 密钥不回显

    # 再保存时省略 api_key，应沿用旧密钥
    again = client.put(
        "/api/channels/asr",
        headers=auth_headers,
        json={"enabled": True, "api_base": "https://asr.example.com/v1"},
    )
    assert again.status_code == 200
    assert again.json()["asr"]["config"]["model"] == "SenseVoiceSmall"


def test_build_transcriber_requires_config() -> None:
    assert build_transcriber(None) is None
    assert build_transcriber({"enabled": False}) is None
    assert build_transcriber({"enabled": True, "api_base": "api.example.com"}) is None
    built = build_transcriber({"enabled": True, "api_base": "https://a.example.com/v1"})
    assert built is not None


# ---------- 提案卡片按钮 ----------


def test_render_proposal_push_includes_confirm_reject_buttons() -> None:
    message = render_proposal_push(
        {
            "id": 5,
            "risk_level": "medium",
            "suggestion": "夜间开启电热毯",
            "entity_id": "switch.blanket",
            "actions": [],
        }
    )

    card = json.dumps(message.card, ensure_ascii=False)
    assert '"kind": "proposal"' in card or '"kind":"proposal"' in card
    assert '"op": "confirm"' in card or '"op":"confirm"' in card
    assert '"op": "reject"' in card or '"op":"reject"' in card
    # 按钮挂在卡片元素里，tag 为 action
    elements = message.card["elements"]
    action_elements = [e for e in elements if e.get("tag") == "action"]
    assert len(action_elements) == 1
    actions = action_elements[0]["actions"]
    assert [b["type"] for b in actions] == ["primary", "danger"]
    assert [b["value"]["op"] for b in actions] == ["confirm", "reject"]


def test_render_reply_proposal_card_also_has_buttons() -> None:
    message = render_reply(
        {
            "reply": "已生成提案。",
            "proposal": {"id": 9, "risk_level": "low", "suggestion": "关客厅灯"},
        }
    )

    elements = message.card["elements"]
    action_elements = [e for e in elements if e.get("tag") == "action"]
    assert len(action_elements) == 1
    assert action_elements[0]["actions"][0]["value"]["id"] == 9


def _make_card_trigger(op: str, proposal_id: int, open_id: str, chat_id: str):
    """构造飞书 SDK 的 P2CardActionTrigger 回调对象（模拟按钮点击）。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    payload = {
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "action": {"value": {"kind": "proposal", "id": proposal_id, "op": op}},
            "context": {"open_chat_id": chat_id},
        },
    }
    return P2CardActionTrigger(payload)


def test_card_action_submits_confirm_command(client) -> None:
    from app.channels.feishu import FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(
        app_id="cli_x", app_secret="s", dispatcher=dispatcher
    )
    response = adapter._on_card_action(
        _make_card_trigger("confirm", 5, "ou_clicker", "oc_chat")
    )

    assert dispatcher.counters["received"] == 1
    assert response is not None and response.toast.content == "已提交，处理结果稍后回复。"


def test_card_action_reject_and_dedupe(client) -> None:
    from app.channels.feishu import FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(
        app_id="cli_x", app_secret="s", dispatcher=dispatcher
    )
    first = adapter._on_card_action(
        _make_card_trigger("reject", 3, "ou_clicker", "oc_chat")
    )
    # 同一按钮的重复投递（飞书 at-least-once）应被去重
    second = adapter._on_card_action(
        _make_card_trigger("reject", 3, "ou_clicker", "oc_chat")
    )

    assert dispatcher.counters["received"] == 1
    assert first is not None
    assert second is None  # 去重后不再回应答


def test_card_action_ignores_foreign_values(client) -> None:
    """非本系统卡片的点击（value 对不上 / 缺字段）一律静默忽略。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    from app.channels.feishu import FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(
        app_id="cli_x", app_secret="s", dispatcher=dispatcher
    )

    # 未知 kind
    foreign = P2CardActionTrigger(
        {
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_x"},
                "action": {"value": {"kind": "other", "id": 1, "op": "confirm"}},
                "context": {"open_chat_id": "oc_x"},
            },
        }
    )
    assert adapter._on_card_action(foreign) is None

    # id 不是数字
    bad_id = P2CardActionTrigger(
        {
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_x"},
                "action": {"value": {"kind": "proposal", "id": "abc", "op": "confirm"}},
                "context": {"open_chat_id": "oc_x"},
            },
        }
    )
    assert adapter._on_card_action(bad_id) is None

    # 缺 operator
    no_operator = P2CardActionTrigger(
        {
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "action": {"value": {"kind": "proposal", "id": 1, "op": "confirm"}},
                "context": {"open_chat_id": "oc_x"},
            },
        }
    )
    assert adapter._on_card_action(no_operator) is None
    assert dispatcher.counters["received"] == 0


# ---------- 点击后卡片原地更新 ----------


def test_render_proposal_processed_removes_buttons_and_adds_status() -> None:
    from app.channels.render import render_proposal_processed

    message = render_proposal_push(
        {"id": 7, "risk_level": "medium", "suggestion": "开电热毯", "actions": []}
    )
    processed = render_proposal_processed(message.card, "confirm")

    # 按钮消失
    assert all(e.get("tag") != "action" for e in processed["elements"])
    # 状态行在最末
    assert "已确认" in processed["elements"][-1]["text"]["content"]
    # 标题与模板色已切换；原卡片对象不被修改
    assert processed["header"]["template"] == "green"
    assert "已确认" in processed["header"]["title"]["content"]
    assert message.card["header"]["template"] != "green"
    assert any(e.get("tag") == "action" for e in message.card["elements"])


def test_render_proposal_processed_reject_grey_and_idempotent_title() -> None:
    from app.channels.render import render_proposal_processed

    message = render_proposal_push(
        {"id": 8, "risk_level": "low", "suggestion": "关客厅灯", "actions": []}
    )
    processed = render_proposal_processed(message.card, "reject")

    assert processed["header"]["template"] == "grey"
    assert "已拒绝" in processed["elements"][-1]["text"]["content"]
    # 重复调用（理论上不会发生，但标题不应叠两层标记）
    again = render_proposal_processed(processed, "reject")
    assert again["header"]["title"]["content"].count("已拒绝") == 1


def test_render_proposal_processed_unknown_op_returns_original() -> None:
    from app.channels.render import render_proposal_processed

    message = render_proposal_push(
        {"id": 9, "risk_level": "low", "suggestion": "关灯", "actions": []}
    )
    assert render_proposal_processed(message.card, "other") is message.card


def test_card_action_response_carries_processed_card(client) -> None:
    from app.channels.feishu import FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(app_id="cli_x", app_secret="s", dispatcher=dispatcher)
    message = render_proposal_push(
        {"id": 5, "risk_level": "medium", "suggestion": "开电热毯", "actions": []}
    )
    adapter._cache_proposal_card(message.card)

    response = adapter._on_card_action(
        _make_card_trigger("confirm", 5, "ou_clicker", "oc_chat")
    )

    assert dispatcher.counters["received"] == 1
    assert response is not None
    assert response.card.type == "raw"
    data = response.card.data
    assert all(e.get("tag") != "action" for e in data["elements"])
    assert "已确认" in data["elements"][-1]["text"]["content"]
    # 无缓存的提案号（如重启后）只回 toast，不报错
    response2 = adapter._on_card_action(
        _make_card_trigger("confirm", 999, "ou_clicker", "oc_chat")
    )
    assert response2 is not None and response2.card is None
    assert response2.toast.content == "已提交，处理结果稍后回复。"


def test_cache_proposal_card_ignores_non_proposal_cards() -> None:
    from app.channels.feishu import CARD_CACHE_LIMIT, FeishuAdapter

    dispatcher, _ = build_dispatcher()
    adapter = FeishuAdapter(app_id="cli_x", app_secret="s", dispatcher=dispatcher)

    # 普通卡片（无 proposal 按钮）不进缓存
    adapter._cache_proposal_card({"elements": [{"tag": "div"}]})
    assert adapter._proposal_cards == {}

    # 上限淘汰最早的
    for proposal_id in range(CARD_CACHE_LIMIT + 10):
        adapter._cache_proposal_card(
            {
                "elements": [
                    {
                        "tag": "action",
                        "actions": [
                            {"value": {"kind": "proposal", "id": proposal_id}}
                        ],
                    }
                ]
            }
        )
    assert len(adapter._proposal_cards) == CARD_CACHE_LIMIT
    assert 0 not in adapter._proposal_cards
    assert CARD_CACHE_LIMIT + 9 in adapter._proposal_cards


def test_patch_ws_card_frames_rewrites_header() -> None:
    """CARD 帧的 type 头被改写成 event，其余头不动；补丁幂等不叠加。"""
    import asyncio as _asyncio

    from lark_oapi.ws import client as ws_client_module
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    import app.channels.feishu as feishu_module

    captured: dict = {}
    saved = ws_client_module.Client._handle_data_frame

    async def stub(client_self, frame):
        captured["types"] = [(h.key, h.value) for h in frame.headers]

    # 先放一个替身当「原实现」，再打补丁（包装层捕获的是打补丁时的方法）
    ws_client_module.Client._handle_data_frame = stub
    try:
        feishu_module._ws_card_patched = False
        feishu_module._patch_ws_card_frames(ws_client_module)
        wrapper = ws_client_module.Client._handle_data_frame
        assert wrapper is not stub  # 确实包了一层
        # 再打一次不应叠加包装
        feishu_module._patch_ws_card_frames(ws_client_module)
        assert ws_client_module.Client._handle_data_frame is wrapper

        frame = Frame()
        h1 = frame.headers.add()
        h1.key, h1.value = "type", "card"
        h2 = frame.headers.add()
        h2.key, h2.value = "message_id", "m1"

        _asyncio.new_event_loop().run_until_complete(wrapper(None, frame))
        assert captured["types"] == [("type", "event"), ("message_id", "m1")]
    finally:
        ws_client_module.Client._handle_data_frame = saved
        feishu_module._ws_card_patched = False
