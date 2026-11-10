from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import structlog

from app.channels.base import ChannelAdapter, InboundMessage, OutboundMessage

logger = structlog.get_logger(__name__)

# 飞书事件回调必须在 3 秒内应答，因此这里只做解析 + 入队，绝不调用 LLM。
DEDUPE_TTL_SECONDS = 600
CONNECT_WAIT_SECONDS = 30.0
# 按提案号缓存已发出的卡片 JSON（点击按钮后在回调应答里原地更新），上限防撑内存
CARD_CACHE_LIMIT = 200
# 群名解析结果的缓存时长，避免每次刷新对话列表都打飞书 API
CHAT_NAME_TTL_SECONDS = 3600
CHAT_NAME_CACHE_LIMIT = 500

# SDK 1.7.5 的 ws 客户端把卡片回调帧（type=card）直接丢弃，而事件分发只看
# payload 里的 header.event_type，与帧头无关。把 CARD 帧的 type 头改写成 event
# 再交回原实现，卡片回调就能落进 register_p2_card_action_trigger 注册的处理器
# ——这是免公网的按钮回调方案。模块级只打一次，适配器反复热重启不会叠加包装。
_ws_card_patched = False


def _patch_ws_card_frames(ws_client_module) -> None:
    global _ws_card_patched
    if _ws_card_patched:
        return
    original = ws_client_module.Client._handle_data_frame

    async def _handle_data_frame_with_card(client_self, frame):  # noqa: ANN001
        for header in frame.headers:
            if header.key == "type" and header.value == "card":
                header.value = "event"
                break
        await original(client_self, frame)

    ws_client_module.Client._handle_data_frame = _handle_data_frame_with_card
    _ws_card_patched = True


class FeishuAdapter(ChannelAdapter):
    """飞书自建应用长连接适配器（WebSocket，无需公网 IP）。

    lark-oapi 的 ws Client 是同步阻塞的，并且把事件循环在模块导入时就捕获下来，
    所以它必须在独立守护线程里跑自己的循环；主应用的事件循环只通过
    dispatcher.submit() 收消息。
    """

    def __init__(
        self, *, app_id: str, app_secret: str, dispatcher, name: str = "feishu",
        transcriber=None,
    ) -> None:
        # 多应用接入时每个实例有自己的名字（feishu / feishu_2 …），
        # dispatcher 注册表和 channel_identity.channel 都按这个名字路由
        self.name = name
        self._app_id = (app_id or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._dispatcher = dispatcher
        self._transcriber = transcriber
        self._client = None  # lark_oapi.ws.client.Client
        self._api = None  # lark_oapi.Client（发消息用）
        self._thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._connect_error: str | None = None
        self._seen: dict[str, float] = {}
        self._proposal_cards: dict[int, dict[str, Any]] = {}
        self._chat_names: dict[str, tuple[float, str]] = {}
        self._started_at: float | None = None
        self._sent = 0
        self._failed = 0
        self._transcribed = 0
        self._transcribe_failed = 0
        self._transcribe_error: str | None = None

    # ---------- 生命周期 ----------

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._app_secret)

    @property
    def _connected(self) -> bool:
        client = self._client
        return client is not None and getattr(client, "_conn", None) is not None

    async def start(self) -> None:
        if not self.configured or self._thread is not None:
            return
        self._ready.clear()
        self._connect_error = None
        self._started_at = None
        self._thread = threading.Thread(
            target=self._run_client, name=f"{self.name}-ws", daemon=True
        )
        self._thread.start()
        # 建连是同步阻塞的网络请求，放到线程里等待，避免卡住启动
        ok = await asyncio.get_running_loop().run_in_executor(
            None, self._ready.wait, CONNECT_WAIT_SECONDS
        )
        if not ok:
            self._connect_error = self._connect_error or "长连接建立超时"
        if self._connect_error:
            logger.warning("channel.feishu.connect_failed", error=self._connect_error)
        elif self._started_at is not None:
            logger.info("channel.feishu.connected")

    async def stop(self) -> None:
        thread = self._thread
        self._thread = None
        self._client = None
        self._api = None
        self._started_at = None
        if thread is None:
            return
        # 刚启动就被停掉时线程还没建好自己的循环，等一小会儿再收尾
        loop = self._thread_loop
        waited = 0.0
        while loop is None and thread.is_alive() and waited < 5.0:
            await asyncio.sleep(0.1)
            waited += 0.1
            loop = self._thread_loop
        if loop is not None and loop.is_running():
            # Client.start() 卡在 _select() 的无限 sleep 里，只关连接不会返回，
            # 必须把线程自己的循环停掉。
            loop.call_soon_threadsafe(loop.stop)
        await self._await_thread_teardown(thread)

    async def _await_thread_teardown(self, thread: threading.Thread) -> None:
        """收尾屏障：等旧适配器线程（连同它的事件循环）彻底退出。

        热重启（stop→apply 串行锁内）必须把旧线程/旧 loop 的 teardown 完成
        作为新线程建环的前置条件，否则新线程 set_event_loop 时旧循环仍被占用，
        触发 lark-oapi 的 "This event loop is already running" 竞态。
        """
        if thread is None or not thread.is_alive():
            return
        await asyncio.get_running_loop().run_in_executor(None, thread.join, 5.0)

    # ---------- 线程内运行 ----------

    def _run_client(self) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import client as ws_client_module
        except Exception as exc:  # noqa: BLE001 - 依赖缺失要在状态里体现
            self._connect_error = f"lark-oapi 不可用：{exc}"
            self._ready.set()
            return

        # SDK 在 import 时捕获了事件循环；这里显式换成本线程自己的循环，
        # 避免它去操作主应用的 asyncio 循环。SDK 内部按线程取循环
        # （lark_oapi/ws/client.py 的 _current_loop），不再依赖模块级单例。
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_loop = loop
        _patch_ws_card_frames(ws_client_module)

        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        try:
            self._api = (
                lark.Client.builder()
                .app_id(self._app_id)
                .app_secret(self._app_secret)
                .build()
            )
            client = ws_client_module.Client(
                self._app_id, self._app_secret, event_handler=handler
            )
        except Exception as exc:  # noqa: BLE001
            self._connect_error = str(exc)[:300]
            self._ready.set()
            loop.close()
            return
        self._client = client

        # start() 会阻塞到循环被停掉，首连结果只能由旁路线程回报。
        threading.Thread(
            target=self._watch_first_connect, name=f"{self.name}-watchdog", daemon=True
        ).start()
        try:
            client.start()
        except Exception as exc:  # noqa: BLE001 - 停循环时这里是预期异常
            if not self._ready.is_set():
                self._connect_error = str(exc)[:300]
                self._ready.set()
            else:
                logger.debug("channel.feishu.client_exit", error=str(exc)[:200])
        finally:
            self._ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            if not loop.is_running():
                loop.close()

    def _watch_first_connect(self) -> None:
        deadline = time.monotonic() + CONNECT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._thread is None:  # 已被 stop
                self._ready.set()
                return
            if self._connected:
                self._started_at = time.monotonic()
                self._ready.set()
                logger.info("channel.feishu.connected")
                return
            time.sleep(0.5)
        if not self._ready.is_set():
            self._connect_error = (
                "长连接未建立：请确认 App ID / App Secret 正确，"
                "且已在开发者后台开启「长连接接收事件」并发布了版本。"
            )
            self._ready.set()

    # ---------- 事件处理（SDK 线程） ----------

    def _on_message(self, data) -> None:
        """飞书 p2 消息事件：解析后立刻入队，直接返回。"""
        try:
            event = getattr(data, "event", None)
            message = getattr(event, "message", None)
            if message is None:
                return
            chat_id = str(message.chat_id or "")
            message_id = str(message.message_id or "")
            sender = getattr(event, "sender", None)
            sender_type = str(getattr(sender, "sender_type", "") or "")
            if sender_type and sender_type != "user":
                return  # 机器人自己发的消息不处理，避免自激
            sender_id = getattr(sender, "sender_id", None)
            external_id = str(
                getattr(sender_id, "open_id", "")
                or getattr(sender_id, "user_id", "")
                or ""
            )
            if (message.message_type or "") == "audio":
                # 语音：下载 + 转写在适配器自己的线程循环里异步做，
                # 回调本身立刻返回，绝不阻塞 SDK 的接收循环
                if not external_id:
                    return
                self._handle_audio(message, message_id, chat_id, external_id)
                return
            if (message.message_type or "") != "text":
                self._reply_unsupported(chat_id)
                return
            if message_id and not self._is_new(message_id):
                return
            if not external_id:
                return
            text = _extract_text(message.content)
            if not text:
                return
            self._dispatcher.submit(
                InboundMessage(
                    channel=self.name,
                    external_id=external_id,
                    chat_id=chat_id,
                    text=text,
                    message_id=message_id,
                    raw={"chat_type": message.chat_type or ""},
                )
            )
        except Exception:  # noqa: BLE001 - 回调异常不能打断 SDK 接收循环
            logger.exception("channel.feishu.event_failed")

    def _on_card_action(self, data) -> None:
        """卡片按钮点击：翻译成「确认 / 拒绝 #编号」文字指令走既有 dispatcher 链路。

        按钮的 value 是自由 dict，op/id/kind 都是我们自己在 render.py 里写的；
        非本系统卡片的点击（value 对不上）一律静默忽略。同步返回 toast +
        「已处理」新卡片（card.type=raw），按钮消失、原卡片原地更新，
        避免同群的人看到按钮还在又点一遍。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        try:
            event = getattr(data, "event", None)
            operator = getattr(event, "operator", None)
            action = getattr(event, "action", None)
            context = getattr(event, "context", None)
            external_id = str(getattr(operator, "open_id", "") or "")
            value = dict(getattr(action, "value", None) or {})
            chat_id = str(getattr(context, "open_chat_id", "") or "")

            if not external_id or not chat_id:
                return None
            kind = str(value.get("kind") or "")
            op = str(value.get("op") or "")
            try:
                proposal_id = int(value.get("id"))
            except (TypeError, ValueError):
                return None
            if kind != "proposal" or op not in ("confirm", "reject"):
                return None

            # 飞书回调是 at-least-once，同一按钮的重复投递去重掉
            message_id = f"card_{op}_{proposal_id}_{external_id}"
            if not self._is_new(message_id):
                return None

            text = f"{'确认' if op == 'confirm' else '拒绝'} #{proposal_id}"
            self._dispatcher.submit(
                InboundMessage(
                    channel=self.name,
                    external_id=external_id,
                    chat_id=chat_id,
                    text=text,
                    message_id=message_id,
                    raw={"source": "card_action"},
                )
            )
            # 回调应答里直接带回新卡片（card.type=raw），飞书原地替换原消息：
            # 按钮消失，显示「已处理」状态行，其他人不用再重复点击
            response: dict[str, Any] = {
                "toast": {"type": "success", "content": "已提交，处理结果稍后回复。"}
            }
            card = self._proposal_cards.get(proposal_id)
            if card is not None:
                from app.channels.render import render_proposal_processed

                response["card"] = {
                    "type": "raw",
                    "data": render_proposal_processed(card, op),
                }
            return P2CardActionTriggerResponse(response)
        except Exception:  # noqa: BLE001 - 回调异常不能打断 SDK 接收循环
            logger.exception("channel.feishu.card_action_failed")
            return None

    def _reply_unsupported(self, chat_id: str) -> None:
        """非文本消息（图片/文件）统一提示，避免用户以为指令被忽略。"""
        loop = self._thread_loop
        if loop is None or not chat_id:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.send(chat_id, OutboundMessage(text="目前只支持文字和语音指令。")), loop
            )
        except Exception:  # noqa: BLE001
            logger.debug("channel.feishu.unsupported_reply_failed")

    def _handle_audio(self, message, message_id: str, chat_id: str, external_id: str) -> None:
        """语音消息：去重后交给线程循环异步下载 + 转写 + 入队。"""
        if message_id and not self._is_new(message_id):
            return
        loop = self._thread_loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._transcribe_audio(message, message_id, chat_id, external_id), loop
            )
        except Exception:  # noqa: BLE001
            logger.exception("channel.feishu.audio_schedule_failed")

    async def _transcribe_audio(
        self, message, message_id: str, chat_id: str, external_id: str
    ) -> None:
        import json as _json

        from lark_oapi.api.im.v1.model.get_message_resource_request import (
            GetMessageResourceRequest,
        )

        transcriber = self._transcriber
        api = self._api
        try:
            content = _json.loads(message.content or "{}")
        except (TypeError, ValueError):
            content = {}
        file_key = str(content.get("file_key") or "")
        duration_ms = int(content.get("duration") or 0)
        if transcriber is None:
            self._transcribe_error = "未启用语音识别（配置页填 ASR 服务后保存）"
            await self.send(
                chat_id,
                OutboundMessage(text="暂不支持语音，请在配置里启用语音识别，或直接发文字。"),
            )
            return
        if api is None or not file_key:
            self._transcribe_failed += 1
            self._transcribe_error = "语音文件下载参数缺失"
            await self.send(chat_id, OutboundMessage(text="这条语音没能下载下来，请再发一次。"))
            return
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("file")
            .build()
        )
        download_started = time.monotonic()
        try:
            response = await asyncio.get_running_loop().run_in_executor(
                None, api.im.v1.message_resource.get, request
            )
        except Exception as exc:  # noqa: BLE001 - 下载失败要反馈给用户
            self._transcribe_failed += 1
            self._transcribe_error = f"语音下载失败：{str(exc)[:200]}"
            logger.exception("channel.feishu.audio_download_failed")
            await self.send(chat_id, OutboundMessage(text="这条语音没能下载下来，请再发一次。"))
            return
        download_ms = int((time.monotonic() - download_started) * 1000)
        code = getattr(response, "code", 0)
        file_obj = getattr(response, "file", None)
        if code not in (0, None) or file_obj is None:
            self._transcribe_failed += 1
            self._transcribe_error = (
                f"语音下载被拒：code={code} msg={getattr(response, 'msg', '')}"
            )
            await self.send(chat_id, OutboundMessage(text="这条语音没能下载下来，请再发一次。"))
            return
        data = file_obj.read() if hasattr(file_obj, "read") else bytes(file_obj)
        if duration_ms and duration_ms > int(10 * 60 * 1000):
            self._transcribe_failed += 1
            self._transcribe_error = "语音超过 10 分钟上限"
            await self.send(chat_id, OutboundMessage(text="语音太长了，请发 10 分钟以内的语音。"))
            return

        started = time.monotonic()
        try:
            text = await transcriber.transcribe(data, f"{message_id or 'voice'}.ogg")
        except Exception as exc:  # noqa: BLE001 - 转写失败要反馈给用户
            self._transcribe_failed += 1
            self._transcribe_error = str(exc)[:300]
            logger.warning(
                "channel.feishu.transcribe_failed", error=self._transcribe_error
            )
            from app.services.asr import user_hint

            hint = user_hint(self._transcribe_error)
            reply = "语音转文字失败了，请再说一遍或直接打字。"
            if hint:
                reply = f"语音转文字失败：{hint}。请直接打字，管理员可在配置页查看详情。"
            await self.send(chat_id, OutboundMessage(text=reply))
            return
        self._transcribed += 1
        self._transcribe_error = None
        logger.info(
            "channel.feishu.transcribed",
            duration_ms=duration_ms,
            download_ms=download_ms,
            transcribe_ms=int((time.monotonic() - started) * 1000),
            chars=len(text),
        )
        text = text.strip()[:4000]
        if not text:
            self._transcribe_failed += 1
            self._transcribe_error = "转写结果为空"
            await self.send(chat_id, OutboundMessage(text="没听清内容，请靠近一点再说一遍。"))
            return
        self._dispatcher.submit(
            InboundMessage(
                channel=self.name,
                external_id=external_id,
                chat_id=chat_id,
                text=text,
                message_id=message_id,
                raw={"chat_type": message.chat_type or "", "from_audio": True},
            )
        )

    def _cache_proposal_card(self, card: dict[str, Any]) -> None:
        """记下提案卡片原文：按钮点击后要在回调应答里原样重建「已处理」卡片。

        只认带 proposal 按钮（value.kind=proposal 且 id 是整数）的卡片；
        超上限时丢最早的，提案推送频率低，正常用不到淘汰。
        """
        try:
            for element in card.get("elements") or []:
                if not isinstance(element, dict) or element.get("tag") != "action":
                    continue
                for button in element.get("actions") or []:
                    value = button.get("value") if isinstance(button, dict) else None
                    if (
                        isinstance(value, dict)
                        and value.get("kind") == "proposal"
                        and isinstance(value.get("id"), int)
                    ):
                        self._proposal_cards[value["id"]] = card
                        while len(self._proposal_cards) > CARD_CACHE_LIMIT:
                            self._proposal_cards.pop(next(iter(self._proposal_cards)))
                        return
        except Exception:  # noqa: BLE001 - 缓存失败不影响发送
            logger.debug("channel.feishu.card_cache_failed")

    def _is_new(self, message_id: str) -> bool:
        now = time.monotonic()
        expired = [
            key for key, ts in self._seen.items() if now - ts > DEDUPE_TTL_SECONDS
        ]
        for key in expired:
            self._seen.pop(key, None)
        if message_id in self._seen:
            return False
        self._seen[message_id] = now
        return True

    async def chat_name(self, chat_id: str) -> str | None:
        """按 chat_id 取群名（用于对话列表标来源）；带 TTL 缓存，失败返回 None 不抛出。"""
        if not chat_id or self._api is None:
            return None
        now = time.monotonic()
        cached = self._chat_names.get(chat_id)
        if cached and now - cached[0] < CHAT_NAME_TTL_SECONDS:
            return cached[1]
        try:
            from lark_oapi.api.im.v1.model.get_chat_request import GetChatRequest

            request = GetChatRequest.builder().chat_id(chat_id).build()
            response = await asyncio.get_running_loop().run_in_executor(
                None, self._api.im.v1.chat.get, request
            )
        except Exception:  # noqa: BLE001 - 失败只记日志，回退给调用方
            logger.debug("channel.feishu.chat_name_failed", chat_id=chat_id)
            return None
        code = getattr(response, "code", 0)
        data = getattr(response, "data", None)
        name = str(getattr(data, "name", "") or "").strip() if data else ""
        if code not in (0, None) or not name:
            return None
        # 只在成功时写缓存，避免把瞬时失败长期计入
        if len(self._chat_names) >= CHAT_NAME_CACHE_LIMIT:
            expired = [
                k
                for k, v in self._chat_names.items()
                if now - v[0] >= CHAT_NAME_TTL_SECONDS
            ]
            for key in expired:
                self._chat_names.pop(key, None)
            if len(self._chat_names) >= CHAT_NAME_CACHE_LIMIT:
                self._chat_names.pop(next(iter(self._chat_names)), None)
        self._chat_names[chat_id] = (now, name)
        return name

    # ---------- 出站 ----------

    async def send(self, chat_id: str, message: OutboundMessage) -> bool:
        api = self._api
        if api is None or not chat_id:
            self._failed += 1
            return False
        # 卡片走 interactive，纯文本走 text；卡片发送失败时退回纯文本
        if message.card:
            self._cache_proposal_card(message.card)
            if await self._send_one(api, chat_id, "interactive", message.card):
                self._sent += 1
                return True
        ok = await self._send_one(api, chat_id, "text", {"text": message.text[:4000]})
        if ok:
            self._sent += 1
        else:
            self._failed += 1
        return ok

    async def _send_one(self, api, chat_id: str, msg_type: str, content: Any) -> bool:
        from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
        from lark_oapi.api.im.v1.model.create_message_request_body import (
            CreateMessageRequestBody,
        )

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        try:
            response = await asyncio.get_running_loop().run_in_executor(
                None, api.im.v1.message.create, request
            )
        except Exception:  # noqa: BLE001 - 发送失败只记日志
            logger.exception("channel.feishu.send_failed", msg_type=msg_type)
            return False
        code = getattr(response, "code", 0)
        if code not in (0, None):
            logger.warning(
                "channel.feishu.send_rejected",
                msg_type=msg_type,
                code=code,
                msg=getattr(response, "msg", ""),
            )
            return False
        return True

    def status(self) -> dict[str, Any]:
        running = bool(self._thread and self._thread.is_alive())
        return {
            "name": self.name,
            "configured": self.configured,
            "running": running,
            "connected": self._connected,
            "app_id": _mask(self._app_id),
            "sent": self._sent,
            "failed": self._failed,
            "error": self._connect_error,
            "asr_enabled": self._transcriber is not None,
            "transcribed": self._transcribed,
            "transcribe_failed": self._transcribe_failed,
            "asr_error": self._transcribe_error,
            "uptime_seconds": (
                int(time.monotonic() - self._started_at) if self._started_at else 0
            ),
        }


def _extract_text(content: str | None) -> str:
    """飞书文本消息 content 是 JSON 字符串：{"text": "..."}，含 @ 时带 mention。"""
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return str(content)[:4000]
    if not isinstance(payload, dict):
        return ""
    text = str(payload.get("text") or "")
    # 群聊里 @机器人 会留下 "@_user_1" 之类的占位，去掉后再匹配意图
    parts = [part for part in text.split(" ") if not part.startswith("@_user")]
    return " ".join(parts).strip()[:4000]


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-2:]}"
