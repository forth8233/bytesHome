from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.proposals import ProposalError
from app.channels.base import ChannelAdapter, InboundMessage, OutboundMessage
from app.channels.render import render_reply
from app.db.models import ChannelIdentity, Member

logger = structlog.get_logger(__name__)

_CONFIRM_RE = re.compile(r"^(?:确认|同意|approve)\s*[#＃]?\s*(\d+)$", re.IGNORECASE)
_REJECT_RE = re.compile(r"^(?:拒绝|取消|reject)\s*[#＃]?\s*(\d+)$", re.IGNORECASE)
_CONFIRM_DRAFT_RE = re.compile(
    r"^(?:确认自动化|写入自动化|confirm)\s*[：: ]?\s*(\S{8,128})$", re.IGNORECASE
)
_HELP_RE = re.compile(r"^(?:帮助|help|/?help|/?说明)$", re.IGNORECASE)

_HELP_TEXT = (
    "直接说要做的事即可，例如：\n"
    "- 关客厅灯\n"
    "- 空调调到 26 度\n"
    "- 客厅温度多少\n\n"
    "高风险动作会先转为待确认提案，回复「确认 #编号」执行，「拒绝 #编号」放弃。"
)
_UNBOUND_TEXT = "这个渠道账号还没有绑定家庭成员，请联系管理员在「消息渠道」里完成绑定。"
_DISABLED_TEXT = "该成员账号已停用，请联系管理员。"
_BUSY_TEXT = "上一条还在处理中，请稍等几秒再发。"

MAX_QUEUE = 200
MAX_TEXT = 4000
# 未绑定账号只留最近这么多条，避免有人乱发消息把内存撑大
MAX_PENDING = 50


class ChannelDispatcher:
    """把渠道消息接进现有对话链路：身份解析 → ChatService → 渲染回发。

    适配器只负责把消息丢进队列（飞书要求 3 秒内应答），真正的处理在
    独立 worker 里串行执行，避免同一成员的并发对话互相踩会话上下文。
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        chat_service,
        proposals,
        config,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._chat = chat_service
        self._proposals = proposals
        self._config = config
        self._loop = loop
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._adapters: dict[str, ChannelAdapter] = {}
        self._sessions: dict[tuple[str, str], int] = {}
        self._busy: set[tuple[str, str]] = set()
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._recent: dict[str, deque[float]] = {}
        # 未绑定账号的最近来信：(channel, external_id) -> 摘要，供管理页一键绑定
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.counters: dict[str, int] = {
            "received": 0,
            "handled": 0,
            "dropped": 0,
            "unbound": 0,
            "failed": 0,
            "confirmed": 0,
            "rejected": 0,
        }

    # ---------- 生命周期 ----------

    def register(self, adapter: ChannelAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def adapters(self) -> dict[str, ChannelAdapter]:
        return dict(self._adapters)

    def unregister_all(self) -> None:
        """热重建时清空注册表，避免残留已停的适配器。"""
        self._adapters.clear()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._worker = asyncio.create_task(self._run(), name="channel-dispatcher")

    async def stop(self) -> None:
        self._stop.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条失败不能拖垮 worker
                self.counters["failed"] += 1
                logger.exception("channel.dispatch_failed", channel=item.channel)

    # ---------- 入站入口（可在其它线程调用） ----------

    def submit(self, message: InboundMessage) -> bool:
        """把消息放进队列；调用方必须立刻返回（飞书 3 秒应答要求）。"""
        self.counters["received"] += 1
        loop = self._loop
        if loop is None:
            self.counters["dropped"] += 1
            return False

        def _put() -> None:
            if self._queue.full():
                # 队列满时丢最旧的一条，保住最新的指令
                try:
                    self._queue.get_nowait()
                    self.counters["dropped"] += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                self.counters["dropped"] += 1

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:  # 事件循环已关闭
            self.counters["dropped"] += 1
            return False
        return True

    # ---------- 处理 ----------

    async def _process(self, msg: InboundMessage) -> None:
        text = (msg.text or "").strip()[:MAX_TEXT]
        if not text:
            return
        key = (msg.channel, msg.chat_id)
        if key in self._busy:
            await self._send(msg.channel, msg.chat_id, OutboundMessage(text=_BUSY_TEXT))
            return
        if not self._allow_rate(msg):
            await self._send(
                msg.channel, msg.chat_id, OutboundMessage(text="消息太频繁，请稍后再发。")
            )
            return

        member = await self._resolve_member(msg)
        if member is None:
            self.counters["unbound"] += 1
            self._remember_pending(msg)
            await self._send(
                msg.channel, msg.chat_id, OutboundMessage(text=_UNBOUND_TEXT)
            )
            return
        self._pending.pop((msg.channel, msg.external_id), None)
        if not member.enabled:
            await self._send(
                msg.channel, msg.chat_id, OutboundMessage(text=_DISABLED_TEXT)
            )
            return

        if _HELP_RE.match(text):
            await self._send(msg.channel, msg.chat_id, OutboundMessage(text=_HELP_TEXT))
            return

        confirm = _CONFIRM_RE.match(text)
        reject = _REJECT_RE.match(text)
        draft_confirm = _CONFIRM_DRAFT_RE.match(text)
        if confirm or reject:
            await self._handle_proposal_command(
                msg, member, int((confirm or reject).group(1)), approve=bool(confirm)
            )
            return
        if draft_confirm:
            await self._handle_draft_command(msg, member, draft_confirm.group(1))
            return

        self._busy.add(key)
        try:
            result = await self._chat.chat(member, text, self._sessions.get(key))
        except Exception as exc:  # noqa: BLE001 - 如实告知，不吞异常
            self.counters["failed"] += 1
            logger.exception("channel.chat_failed", channel=msg.channel, member_id=member.id)
            await self._send(
                msg.channel,
                msg.chat_id,
                OutboundMessage(text=f"这次处理出错了：{str(exc)[:200]}"),
            )
            return
        finally:
            self._busy.discard(key)

        session_id = result.get("session_id")
        if isinstance(session_id, int):
            self._sessions[key] = session_id
        self.counters["handled"] += 1
        await self._send(msg.channel, msg.chat_id, render_reply(result))

    async def _handle_proposal_command(
        self, msg: InboundMessage, member: Member, proposal_id: int, *, approve: bool
    ) -> None:
        try:
            proposal = await self._proposals.get(proposal_id)
        except ProposalError as exc:
            await self._send(
                msg.channel, msg.chat_id, OutboundMessage(text=f"提案处理失败：{exc.message}")
            )
            return
        if (
            proposal.get("member_id")
            and member.role != "admin"
            and proposal["member_id"] != member.id
        ):
            await self._send(
                msg.channel,
                msg.chat_id,
                OutboundMessage(text=f"提案 #{proposal_id} 不属于你，不能操作。"),
            )
            return
        try:
            if approve:
                updated = await self._proposals.approve(proposal_id, member)
                self.counters["confirmed"] += 1
                text = _approve_text(updated)
            else:
                updated = await self._proposals.reject(proposal_id, member, "渠道内拒绝")
                self.counters["rejected"] += 1
                text = f"已拒绝提案 #{proposal_id}。"
        except ProposalError as exc:
            await self._send(
                msg.channel, msg.chat_id, OutboundMessage(text=f"提案处理失败：{exc.message}")
            )
            return
        await self._send(msg.channel, msg.chat_id, OutboundMessage(text=text))

    async def _handle_draft_command(
        self, msg: InboundMessage, member: Member, token: str
    ) -> None:
        try:
            result = await self._chat.confirm_automation(
                member, token, ip=f"channel:{msg.channel}"
            )
        except HTTPException as exc:
            await self._send(
                msg.channel,
                msg.chat_id,
                OutboundMessage(text=f"自动化确认失败：{str(exc.detail)[:200]}"),
            )
            return
        except Exception as exc:  # noqa: BLE001 - 令牌错误要原样反馈
            await self._send(
                msg.channel,
                msg.chat_id,
                OutboundMessage(text=f"自动化确认失败：{str(exc)[:200]}"),
            )
            return
        alias = result.get("alias") or result.get("automation_id") or ""
        reply = f"自动化「{alias}」已写入 Home Assistant。"
        await self._send(msg.channel, msg.chat_id, OutboundMessage(text=reply))

    # ---------- 待绑定账号 ----------

    def _remember_pending(self, msg: InboundMessage) -> None:
        """记住未绑定账号的来信，管理员才能在配置页看到 open_id 并一键绑定。"""
        key = (msg.channel, msg.external_id)
        now = time.time()
        entry = self._pending.get(key)
        if entry is None:
            if len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending, key=lambda k: self._pending[k]["last_seen"])
                self._pending.pop(oldest, None)
            entry = {
                "channel": msg.channel,
                "external_id": msg.external_id,
                "chat_id": msg.chat_id,
                "display_name": msg.display_name,
                "first_seen": now,
                "count": 0,
            }
            self._pending[key] = entry
        entry["last_seen"] = now
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["chat_id"] = msg.chat_id or entry.get("chat_id") or ""
        if msg.display_name:
            entry["display_name"] = msg.display_name[:64]
        entry["last_text"] = (msg.text or "")[:100]

    def pending(self) -> list[dict[str, Any]]:
        """按最近来信排序的待绑定账号列表。"""
        items = sorted(
            self._pending.values(), key=lambda item: item["last_seen"], reverse=True
        )
        return [dict(item) for item in items]

    def forget_pending(self, channel: str, external_id: str) -> None:
        self._pending.pop((channel, external_id), None)

    # ---------- 身份与限流 ----------

    async def _resolve_member(self, msg: InboundMessage) -> Member | None:
        async with self._session_factory() as session:
            identity = await session.scalar(
                select(ChannelIdentity).where(
                    ChannelIdentity.channel == msg.channel,
                    ChannelIdentity.external_id == msg.external_id,
                    ChannelIdentity.enabled.is_(True),
                )
            )
            if identity is None:
                return None
            member = await session.get(Member, identity.member_id)
            if member is None:
                return None
            dirty = False
            if msg.display_name and identity.display_name != msg.display_name[:64]:
                identity.display_name = msg.display_name[:64]
                dirty = True
            # 记住最近一次会话，主动推送（提案/告警）才能找到这个人
            if msg.chat_id:
                try:
                    meta = json.loads(identity.meta_json or "{}")
                except (TypeError, ValueError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                if meta.get("chat_id") != msg.chat_id:
                    meta["chat_id"] = msg.chat_id
                    identity.meta_json = json.dumps(meta, ensure_ascii=False)
                    dirty = True
            if dirty:
                await session.commit()
            return member

    def _allow_rate(self, msg: InboundMessage) -> bool:
        limit = int(self._config.global_settings().get("channel_rate_limit_per_minute", 20))
        if limit <= 0:
            return True
        now = time.monotonic()
        bucket = self._recent.setdefault(msg.external_id, deque())
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    # ---------- 出站 ----------

    async def _send(self, channel: str, chat_id: str, message: OutboundMessage) -> bool:
        adapter = self._adapters.get(channel)
        if adapter is None:
            logger.warning("channel.send_no_adapter", channel=channel)
            return False
        try:
            return await adapter.send(chat_id, message)
        except Exception:  # noqa: BLE001 - 出站失败只记日志
            logger.exception("channel.send_failed", channel=channel)
            return False

    async def push(self, channel: str, external_id: str, message: OutboundMessage) -> bool:
        """主动推送：按渠道身份找到对应会话并发送。"""
        adapter = self._adapters.get(channel)
        if adapter is None:
            return False
        async with self._session_factory() as session:
            identity = await session.scalar(
                select(ChannelIdentity).where(
                    ChannelIdentity.channel == channel,
                    ChannelIdentity.external_id == external_id,
                    ChannelIdentity.enabled.is_(True),
                )
            )
        if identity is None:
            return False
        try:
            meta = json.loads(identity.meta_json or "{}")
        except (TypeError, ValueError):
            meta = {}
        chat_id = str(meta.get("chat_id") or external_id)
        return await self._send(channel, chat_id, message)

    def stats(self) -> dict[str, Any]:
        return {
            "queue_size": self._queue.qsize(),
            "sessions": len(self._sessions),
            "adapters": {
                name: adapter.status() for name, adapter in self._adapters.items()
            },
            **self.counters,
        }


def _approve_text(updated: dict[str, Any]) -> str:
    status = updated.get("status")
    if status == "executed":
        return f"提案 #{updated.get('id')} 已确认并执行完成。"
    if status == "failed":
        return (
            f"提案 #{updated.get('id')} 已确认，但执行失败："
            f"{str(updated.get('execution_result'))[:200]}"
        )
    return f"提案 #{updated.get('id')} 当前状态：{status}。"
